#include "blocking_data_engine.h"

#include <fcntl.h>
#include <linux/stat.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <utility>

namespace uring_slab {
namespace {

std::runtime_error SystemError(const char* operation, int error) {
  return std::runtime_error(std::string(operation) + " 失败：" +
                            std::strerror(error) + " (errno=" +
                            std::to_string(error) + ")");
}

std::uint64_t MonotonicNowNs() noexcept {
  return static_cast<std::uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(
          std::chrono::steady_clock::now().time_since_epoch())
          .count());
}

std::size_t CheckedOffset(std::uint64_t slot,
                          std::size_t block_size,
                          const char* description) {
  if (slot > std::numeric_limits<std::size_t>::max() ||
      static_cast<std::size_t>(slot) >
          std::numeric_limits<std::size_t>::max() / block_size) {
    throw std::out_of_range(std::string(description) + " 超出size_t范围");
  }
  return static_cast<std::size_t>(slot) * block_size;
}

}  // namespace

BlockingDataEngine::BlockingDataEngine(void* primary_base,
                                       std::size_t primary_bytes,
                                       std::size_t block_size_bytes,
                                       std::string slab_path,
                                       std::size_t slab_bytes,
                                       std::size_t workers,
                                       std::size_t pending_capacity)
    : primary_base_(static_cast<std::byte*>(primary_base)),
      primary_bytes_(primary_bytes),
      block_size_bytes_(block_size_bytes),
      slab_path_(std::move(slab_path)),
      slab_bytes_(slab_bytes),
      workers_count_(workers),
      store_limit_(workers >= 4
                       ? workers - std::max<std::size_t>(1, workers / 4)
                       : 0),
      pending_capacity_(pending_capacity) {
  if (primary_base_ == nullptr || block_size_bytes_ == 0 || slab_path_.empty()) {
    throw std::invalid_argument("BlockingDataEngine几何参数非法");
  }
  if (slab_bytes_ == 0 || slab_bytes_ % block_size_bytes_ != 0) {
    throw std::invalid_argument("slab_bytes必须是block size的正整数倍");
  }
  if (workers_count_ < 4) {
    throw std::invalid_argument("workers必须至少为4");
  }
  if (pending_capacity_ < workers_count_) {
    throw std::invalid_argument("pending_capacity不能小于workers");
  }
  if (slab_bytes_ >
      static_cast<std::size_t>(std::numeric_limits<off_t>::max())) {
    throw std::invalid_argument("slab_bytes超出off_t范围");
  }

  InitializeSlab();
  try {
    workers_.reserve(workers_count_);
    for (std::size_t i = 0; i < workers_count_; ++i) {
      workers_.emplace_back(&BlockingDataEngine::WorkerLoop, this);
    }
  } catch (...) {
    {
      std::lock_guard<std::mutex> lock(mutex_);
      stopping_ = true;
    }
    work_cv_.notify_all();
    for (std::thread& worker : workers_) {
      worker.join();
    }
    DestroySlab();
    throw;
  }
}

BlockingDataEngine::~BlockingDataEngine() {
  Shutdown();
}

void BlockingDataEngine::InitializeSlab() {
  slab_fd_ =
      open(slab_path_.c_str(), O_CREAT | O_RDWR | O_DIRECT | O_CLOEXEC, 0644);
  if (slab_fd_ < 0) {
    throw SystemError("打开blocking slab", errno);
  }
  if (flock(slab_fd_, LOCK_EX | LOCK_NB) != 0) {
    const int error = errno;
    DestroySlab();
    throw SystemError("独占锁定blocking slab", error);
  }
  if (ftruncate(slab_fd_, 0) != 0) {
    const int error = errno;
    DestroySlab();
    throw SystemError("截断blocking slab", error);
  }
  const int allocation_error =
      posix_fallocate(slab_fd_, 0, static_cast<off_t>(slab_bytes_));
  if (allocation_error != 0) {
    DestroySlab();
    throw SystemError("预分配blocking slab", allocation_error);
  }
  try {
    ValidateGeometry();
  } catch (...) {
    DestroySlab();
    throw;
  }
}

void BlockingDataEngine::DestroySlab() noexcept {
  if (slab_fd_ >= 0) {
    close(slab_fd_);
    slab_fd_ = -1;
  }
}

void BlockingDataEngine::ValidateGeometry() const {
  struct statx info {};
  if (statx(slab_fd_, "", AT_EMPTY_PATH,
            STATX_SIZE | STATX_DIOALIGN, &info) != 0) {
    throw SystemError("statx(STATX_DIOALIGN)", errno);
  }
  if ((info.stx_mask & STATX_DIOALIGN) != STATX_DIOALIGN ||
      info.stx_dio_mem_align == 0 || info.stx_dio_offset_align == 0) {
    throw std::runtime_error("文件系统未返回direct I/O对齐信息");
  }
  if (reinterpret_cast<std::uintptr_t>(primary_base_) %
          info.stx_dio_mem_align != 0 ||
      block_size_bytes_ % info.stx_dio_mem_align != 0 ||
      block_size_bytes_ % info.stx_dio_offset_align != 0) {
    throw std::invalid_argument("primary/slab不满足direct I/O对齐要求");
  }
  if (info.stx_size != static_cast<__u64>(slab_bytes_)) {
    throw std::runtime_error("blocking slab实际大小不符");
  }
}

void BlockingDataEngine::ValidateTask(const BlockIo& task) const {
  const std::size_t primary_offset =
      CheckedOffset(task.primary_slot, block_size_bytes_, "primary offset");
  const std::size_t secondary_offset =
      CheckedOffset(task.secondary_slot, block_size_bytes_, "secondary offset");
  if (primary_offset > primary_bytes_ ||
      block_size_bytes_ > primary_bytes_ - primary_offset) {
    throw std::out_of_range("primary slot越界");
  }
  if (secondary_offset > slab_bytes_ ||
      block_size_bytes_ > slab_bytes_ - secondary_offset) {
    throw std::out_of_range("secondary slot越界");
  }
}

void BlockingDataEngine::Submit(BlockIo task) {
  ValidateTask(task);
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (stopping_) {
      throw std::runtime_error("BlockingDataEngine正在shutdown");
    }
    if (load_pending_.size() + store_pending_.size() >= pending_capacity_) {
      completions_.push_back(
          BlockCompletion{task.job_id, std::move(task.key), false, EAGAIN});
      return;
    }
    task.enqueue_ns = MonotonicNowNs();
    if (task.direction == IoDirection::kLoad) {
      load_pending_.push_back(std::move(task));
    } else {
      store_pending_.push_back(std::move(task));
    }
    ++accepted_not_completed_;
  }
  work_cv_.notify_one();
}

bool BlockingDataEngine::CanDispatchLocked() const {
  if (!load_pending_.empty()) {
    return true;
  }
  return !store_pending_.empty() && store_in_flight_ < store_limit_;
}

void BlockingDataEngine::WorkerLoop() {
  for (;;) {
    BlockIo task;
    std::uint64_t dispatch_ns = 0;
    {
      std::unique_lock<std::mutex> lock(mutex_);
      work_cv_.wait(lock, [this] {
        return (stopping_ && accepted_not_completed_ == 0) ||
               CanDispatchLocked();
      });
      if (stopping_ && accepted_not_completed_ == 0) {
        return;
      }
      if (!load_pending_.empty()) {
        task = std::move(load_pending_.front());
        load_pending_.pop_front();
        ++load_in_flight_;
      } else {
        task = std::move(store_pending_.front());
        store_pending_.pop_front();
        ++store_in_flight_;
      }
      dispatch_ns = MonotonicNowNs();
    }

    const std::size_t primary_offset =
        static_cast<std::size_t>(task.primary_slot) * block_size_bytes_;
    const off_t slab_offset =
        static_cast<off_t>(task.secondary_slot * block_size_bytes_);
    void* const address = primary_base_ + primary_offset;
    const ssize_t result =
        task.direction == IoDirection::kLoad
            ? pread(slab_fd_, address, block_size_bytes_, slab_offset)
            : pwrite(slab_fd_, address, block_size_bytes_, slab_offset);
    const int saved_errno = result < 0 ? errno : 0;
    const std::uint64_t completion_ns = MonotonicNowNs();
    const bool success = result == static_cast<ssize_t>(block_size_bytes_);
    const int error_code = success ? 0 : (result < 0 ? saved_errno : EIO);

    {
      std::lock_guard<std::mutex> lock(mutex_);
      DirectionIoStats& direction_stats =
          task.direction == IoDirection::kLoad ? stats_.load : stats_.store;
      const std::uint64_t queue_ns = dispatch_ns - task.enqueue_ns;
      const std::uint64_t execution_ns = completion_ns - dispatch_ns;
      ++direction_stats.count;
      direction_stats.queue_ns_sum += queue_ns;
      direction_stats.queue_ns_max =
          std::max(direction_stats.queue_ns_max, queue_ns);
      direction_stats.dispatch_to_cqe_ns_sum += execution_ns;
      direction_stats.dispatch_to_cqe_ns_max =
          std::max(direction_stats.dispatch_to_cqe_ns_max, execution_ns);
      completions_.push_back(BlockCompletion{
          task.job_id, std::move(task.key), success, error_code});
      if (task.direction == IoDirection::kLoad) {
        --load_in_flight_;
      } else {
        --store_in_flight_;
      }
      --accepted_not_completed_;
      if (accepted_not_completed_ == 0) {
        idle_cv_.notify_all();
      }
    }
    work_cv_.notify_all();
  }
}

std::vector<BlockCompletion> BlockingDataEngine::TakeCompletionsLocked() {
  std::vector<BlockCompletion> result;
  result.reserve(completions_.size());
  while (!completions_.empty()) {
    result.push_back(std::move(completions_.front()));
    completions_.pop_front();
  }
  return result;
}

std::vector<BlockCompletion> BlockingDataEngine::PollCompletions() {
  std::lock_guard<std::mutex> lock(mutex_);
  return TakeCompletionsLocked();
}

std::vector<BlockCompletion> BlockingDataEngine::Drain() {
  std::unique_lock<std::mutex> lock(mutex_);
  idle_cv_.wait(lock, [this] { return accepted_not_completed_ == 0; });
  return TakeCompletionsLocked();
}

bool BlockingDataEngine::HasPendingWork() const noexcept {
  std::lock_guard<std::mutex> lock(mutex_);
  return accepted_not_completed_ != 0 || !completions_.empty();
}

EngineStats BlockingDataEngine::StatsSnapshot() const noexcept {
  std::lock_guard<std::mutex> lock(mutex_);
  return stats_;
}

void BlockingDataEngine::ResetStats() {
  std::lock_guard<std::mutex> lock(mutex_);
  if (accepted_not_completed_ != 0 || !completions_.empty()) {
    throw std::runtime_error("BlockingDataEngine有pending work，不能清零统计");
  }
  stats_ = EngineStats{};
}

void BlockingDataEngine::Shutdown() {
  {
    std::lock_guard<std::mutex> lock(mutex_);
    stopping_ = true;
  }
  work_cv_.notify_all();
  for (std::thread& worker : workers_) {
    if (worker.joinable()) {
      worker.join();
    }
  }
  workers_.clear();
  DestroySlab();
}

}  // namespace uring_slab
