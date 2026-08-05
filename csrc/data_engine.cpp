#include "data_engine.h"

#include <liburing.h>

#include <fcntl.h>
#include <linux/stat.h>
#include <poll.h>
#include <sys/eventfd.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <bit>
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

std::size_t CheckedMultiply(std::size_t lhs,
                            std::size_t rhs,
                            const char* description) {
  if (rhs != 0 && lhs > std::numeric_limits<std::size_t>::max() / rhs) {
    throw std::overflow_error(std::string(description) + " 超出 size_t 范围");
  }
  return lhs * rhs;
}

unsigned RingEntries(std::size_t requested, const char* description) {
  const std::size_t rounded = std::bit_ceil(requested);
  if (rounded > std::numeric_limits<unsigned>::max()) {
    throw std::invalid_argument(std::string(description) + " 过大");
  }
  return static_cast<unsigned>(rounded);
}

std::uint64_t MonotonicNowNs() noexcept {
  return static_cast<std::uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(
          std::chrono::steady_clock::now().time_since_epoch())
          .count());
}

}  // namespace

DataEngine::DataEngine(void* primary_base,
                       std::size_t primary_bytes,
                       std::size_t block_size_bytes,
                       std::string slab_path,
                       std::size_t slab_bytes,
                       EngineOptions options)
    : primary_base_(static_cast<std::byte*>(primary_base)),
      primary_bytes_(primary_bytes),
      block_size_bytes_(block_size_bytes),
      slab_path_(std::move(slab_path)),
      slab_bytes_(slab_bytes),
      total_qd_(options.total_qd),
      load_reserve_(std::max<std::size_t>(1, options.total_qd / 4)),
      store_qd_(options.total_qd - load_reserve_),
      pending_capacity_(options.pending_capacity) {
  if (primary_base_ == nullptr) {
    throw std::invalid_argument("primary_base 不能为空");
  }
  if (block_size_bytes_ == 0) {
    throw std::invalid_argument("block_size_bytes 必须大于 0");
  }
  if (slab_path_.empty()) {
    throw std::invalid_argument("slab_path 不能为空");
  }
  if (slab_bytes_ == 0 || slab_bytes_ % block_size_bytes_ != 0) {
    throw std::invalid_argument(
        "slab_bytes 必须是 block_size_bytes 的正整数倍");
  }
  if (total_qd_ < 4) {
    throw std::invalid_argument("total_qd 必须至少为 4");
  }
  if (total_qd_ > std::numeric_limits<std::size_t>::max() / 2) {
    throw std::invalid_argument("total_qd 过大");
  }
  if (slab_bytes_ >
      static_cast<std::size_t>(std::numeric_limits<off_t>::max())) {
    throw std::invalid_argument("slab_bytes 超出 off_t 范围");
  }
  if (pending_capacity_ < total_qd_) {
    throw std::invalid_argument(
        "pending_capacity 不能小于 total_qd");
  }

  contexts_.reserve(total_qd_);
  free_contexts_.reserve(total_qd_);
  for (std::size_t i = 0; i < total_qd_; ++i) {
    contexts_.push_back(std::make_unique<RequestContext>());
    free_contexts_.push_back(contexts_.back().get());
  }

  owner_ = std::thread(&DataEngine::OwnerLoop, this);

  // owner 负责创建 slab 和 ring；构造线程等待其初始化完成，避免把一个
  // 半初始化的 Engine 暴露给 Python。
  std::unique_lock<std::mutex> lock(mutex_);
  startup_cv_.wait(lock, [this] { return startup_complete_; });
  if (startup_error_ != nullptr) {
    std::exception_ptr error = startup_error_;
    lock.unlock();
    owner_.join();
    std::rethrow_exception(error);
  }
}

DataEngine::~DataEngine() {
  Shutdown();
}

void DataEngine::InitializeSlabAndRing() {
  slab_fd_ =
      open(slab_path_.c_str(), O_CREAT | O_RDWR | O_DIRECT | O_CLOEXEC, 0644);
  if (slab_fd_ < 0) {
    throw SystemError("打开 slab", errno);
  }

  // V1 不允许两个 Engine 同时使用同一路径。锁必须在截断前取得；进程退出
  // 或 fd 关闭时内核自动释放锁。
  if (flock(slab_fd_, LOCK_EX | LOCK_NB) != 0) {
    const int error = errno;
    close(slab_fd_);
    slab_fd_ = -1;
    throw SystemError("独占锁定 slab", error);
  }

  // V1 每次构造都创建全新 slab。先截断以清除旧 extent/数据映射，
  // 再由 posix_fallocate 预留完整空间；未写区域读取为零。
  if (ftruncate(slab_fd_, 0) != 0) {
    const int error = errno;
    close(slab_fd_);
    slab_fd_ = -1;
    throw SystemError("截断 slab", error);
  }
  const int allocation_error =
      posix_fallocate(slab_fd_, 0, static_cast<off_t>(slab_bytes_));
  if (allocation_error != 0) {
    close(slab_fd_);
    slab_fd_ = -1;
    throw SystemError("为 slab 预分配空间", allocation_error);
  }

  try {
    ValidateGeometry();
  } catch (...) {
    close(slab_fd_);
    slab_fd_ = -1;
    throw;
  }

  wake_fd_ = eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK);
  if (wake_fd_ < 0) {
    const int error = errno;
    close(slab_fd_);
    slab_fd_ = -1;
    throw SystemError("eventfd", error);
  }

  ring_ = new io_uring{};
  io_uring_params params{};
  // ring 在 owner 线程创建，后续也只有 owner 提交，因此可以安全声明
  // SINGLE_ISSUER。
  params.flags = IORING_SETUP_SINGLE_ISSUER |
                 IORING_SETUP_SUBMIT_ALL | IORING_SETUP_CQSIZE;
  params.cq_entries = RingEntries(total_qd_ * 2, "CQ 容量");
  const unsigned sq_entries = RingEntries(total_qd_, "SQ 容量");
  const int ring_error = io_uring_queue_init_params(sq_entries, ring_, &params);
  if (ring_error < 0) {
    delete ring_;
    ring_ = nullptr;
    close(wake_fd_);
    wake_fd_ = -1;
    close(slab_fd_);
    slab_fd_ = -1;
    throw SystemError("io_uring_queue_init_params", -ring_error);
  }

  const int register_error = io_uring_register_files(ring_, &slab_fd_, 1);
  if (register_error < 0) {
    io_uring_queue_exit(ring_);
    delete ring_;
    ring_ = nullptr;
    close(wake_fd_);
    wake_fd_ = -1;
    close(slab_fd_);
    slab_fd_ = -1;
    throw SystemError("io_uring_register_files", -register_error);
  }
  fixed_file_registered_ = true;
}

void DataEngine::DestroySlabAndRing() noexcept {
  // 本函数只由 owner 调用，与创建资源的线程保持一致。
  if (fixed_file_registered_) {
    io_uring_unregister_files(ring_);
    fixed_file_registered_ = false;
  }
  if (ring_ != nullptr) {
    io_uring_queue_exit(ring_);
    delete ring_;
    ring_ = nullptr;
  }
  if (wake_fd_ >= 0) {
    close(wake_fd_);
    wake_fd_ = -1;
  }
  if (slab_fd_ >= 0) {
    close(slab_fd_);  // 同时释放 flock 独占锁。
    slab_fd_ = -1;
  }
}

void DataEngine::ValidateGeometry() const {
  struct statx info {};
  if (statx(slab_fd_, "", AT_EMPTY_PATH,
            STATX_SIZE | STATX_DIOALIGN, &info) != 0) {
    throw SystemError("statx(STATX_DIOALIGN)", errno);
  }
  if ((info.stx_mask & STATX_DIOALIGN) != STATX_DIOALIGN ||
      info.stx_dio_mem_align == 0 || info.stx_dio_offset_align == 0) {
    throw std::runtime_error(
        "文件系统未返回 direct I/O 对齐信息");
  }
  if (reinterpret_cast<std::uintptr_t>(primary_base_) %
          info.stx_dio_mem_align !=
      0) {
    throw std::invalid_argument(
        "primary 基址不满足 direct I/O 内存对齐要求");
  }
  if (block_size_bytes_ % info.stx_dio_mem_align != 0) {
    throw std::invalid_argument(
        "primary block stride 不满足 direct I/O 内存对齐要求");
  }
  if (block_size_bytes_ % info.stx_dio_offset_align != 0) {
    throw std::invalid_argument(
        "block size 不满足 direct I/O 文件偏移对齐要求");
  }
  if (info.stx_size != static_cast<__u64>(slab_bytes_)) {
    throw std::runtime_error("实际 slab 大小与 slab_bytes 不一致");
  }
}

void DataEngine::ValidateTask(const BlockIo& task) const {
  if (task.primary_slot > std::numeric_limits<std::size_t>::max() ||
      task.secondary_slot > std::numeric_limits<std::size_t>::max()) {
    throw std::out_of_range("slot id 超出 size_t 范围");
  }
  const std::size_t primary_offset =
      CheckedMultiply(static_cast<std::size_t>(task.primary_slot),
                      block_size_bytes_, "primary slot 字节偏移");
  const std::size_t secondary_offset =
      CheckedMultiply(static_cast<std::size_t>(task.secondary_slot),
                      block_size_bytes_, "secondary slot 字节偏移");
  if (primary_offset > primary_bytes_ ||
      block_size_bytes_ > primary_bytes_ - primary_offset) {
    throw std::out_of_range("primary slot 超出 primary buffer 范围");
  }
  if (secondary_offset > slab_bytes_ ||
      block_size_bytes_ > slab_bytes_ - secondary_offset) {
    throw std::out_of_range("secondary slot 超出 slab 范围");
  }
}

void DataEngine::Submit(BlockIo task) {
  ValidateTask(task);
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (stopping_) {
      throw std::runtime_error("DataEngine 正在 shutdown，不能继续提交");
    }
    if (load_pending_.size() + store_pending_.size() >= pending_capacity_) {
      completions_.push_back(BlockCompletion{
          task.job_id,
          std::move(task.key),
          false,
          EAGAIN,
      });
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
  WakeOwner();
}

std::vector<BlockCompletion> DataEngine::TakeCompletionsLocked() {
  std::vector<BlockCompletion> result;
  result.reserve(completions_.size());
  while (!completions_.empty()) {
    result.push_back(std::move(completions_.front()));
    completions_.pop_front();
  }
  return result;
}

std::vector<BlockCompletion> DataEngine::PollCompletions() {
  std::lock_guard<std::mutex> lock(mutex_);
  return TakeCompletionsLocked();
}

std::vector<BlockCompletion> DataEngine::Drain() {
  std::unique_lock<std::mutex> lock(mutex_);
  idle_cv_.wait(lock, [this] { return accepted_not_completed_ == 0; });
  return TakeCompletionsLocked();
}

void DataEngine::Shutdown() {
  {
    std::lock_guard<std::mutex> lock(mutex_);
    stopping_ = true;
  }
  WakeOwner();
  if (owner_.joinable()) {
    owner_.join();
  }
}

bool DataEngine::HasPendingWork() const noexcept {
  std::lock_guard<std::mutex> lock(mutex_);
  return accepted_not_completed_ != 0 || !completions_.empty();
}

EngineStats DataEngine::StatsSnapshot() const noexcept {
  std::lock_guard<std::mutex> lock(mutex_);
  return stats_;
}

void DataEngine::WakeOwner() noexcept {
  if (wake_fd_ < 0) {
    return;
  }
  const std::uint64_t one = 1;
  const ssize_t result = write(wake_fd_, &one, sizeof(one));
  (void)result;  // EAGAIN 表示已有尚未消费的唤醒信号。
}

void DataEngine::DrainWakeFd() noexcept {
  std::uint64_t value;
  while (read(wake_fd_, &value, sizeof(value)) == sizeof(value)) {
  }
}

bool DataEngine::CanDispatchLocked() const {
  const std::size_t total_in_flight =
      load_in_flight_ + store_in_flight_;
  if (total_in_flight >= total_qd_ || free_contexts_.empty()) {
    return false;
  }
  if (!load_pending_.empty()) {
    return true;
  }
  return !store_pending_.empty() && store_in_flight_ < store_qd_;
}

DataEngine::RequestContext* DataEngine::AcquireContextLocked() {
  RequestContext* context = free_contexts_.back();
  free_contexts_.pop_back();
  context->occupied = true;
  return context;
}

void DataEngine::OwnerLoop() {
  try {
    InitializeSlabAndRing();
  } catch (...) {
    DestroySlabAndRing();
    {
      std::lock_guard<std::mutex> lock(mutex_);
      startup_error_ = std::current_exception();
      startup_complete_ = true;
    }
    startup_cv_.notify_one();
    return;
  }

  {
    std::lock_guard<std::mutex> lock(mutex_);
    startup_complete_ = true;
  }
  startup_cv_.notify_one();

  for (;;) {
    ReapAvailable();
    DispatchAvailable();

    {
      std::lock_guard<std::mutex> lock(mutex_);
      if (stopping_ && accepted_not_completed_ == 0) {
        break;
      }
    }

    pollfd fds[2] = {
        {.fd = ring_->ring_fd, .events = POLLIN, .revents = 0},
        {.fd = wake_fd_, .events = POLLIN, .revents = 0},
    };
    int result;
    do {
      result = poll(fds, 2, -1);
    } while (result < 0 && errno == EINTR);
    if (result < 0) {
      std::terminate();
    }
    if ((fds[1].revents & POLLIN) != 0) {
      DrainWakeFd();
    }
  }

  DestroySlabAndRing();
}

void DataEngine::DispatchAvailable() {
  unsigned prepared = 0;
  while (true) {
    RequestContext* context = nullptr;
    {
      std::lock_guard<std::mutex> lock(mutex_);
      if (!CanDispatchLocked()) {
        break;
      }
      context = AcquireContextLocked();
      if (!load_pending_.empty()) {
        context->task = std::move(load_pending_.front());
        load_pending_.pop_front();
        ++load_in_flight_;
      } else {
        context->task = std::move(store_pending_.front());
        store_pending_.pop_front();
        ++store_in_flight_;
      }
      context->dispatch_ns = MonotonicNowNs();
    }

    io_uring_sqe* sqe = io_uring_get_sqe(ring_);
    if (sqe == nullptr) {
      std::terminate();  // 逻辑 QD 按约定必须完全容纳于 SQ。
    }

    const auto primary_offset =
        static_cast<std::size_t>(context->task.primary_slot) *
        block_size_bytes_;
    const auto slab_offset =
        static_cast<off_t>(context->task.secondary_slot * block_size_bytes_);
    void* const primary_address = primary_base_ + primary_offset;

    if (context->task.direction == IoDirection::kStore) {
      io_uring_prep_write(sqe, 0, primary_address, block_size_bytes_,
                          slab_offset);
    } else {
      io_uring_prep_read(sqe, 0, primary_address, block_size_bytes_,
                         slab_offset);
    }
    io_uring_sqe_set_flags(sqe, IOSQE_FIXED_FILE);
    io_uring_sqe_set_data(sqe, context);
    ++prepared;
  }
  SubmitPrepared(prepared);
}

void DataEngine::SubmitPrepared(unsigned prepared) {
  unsigned submitted = 0;
  while (submitted < prepared) {
    const int result = io_uring_submit(ring_);
    if (result <= 0) {
      std::terminate();
    }
    submitted += static_cast<unsigned>(result);
  }
}

void DataEngine::ReapAvailable() {
  io_uring_cqe* cqe = nullptr;
  while (io_uring_peek_cqe(ring_, &cqe) == 0) {
    auto* context =
        static_cast<RequestContext*>(io_uring_cqe_get_data(cqe));
    Finish(context, cqe->res);
    io_uring_cqe_seen(ring_, cqe);
  }
}

void DataEngine::Finish(RequestContext* context, int result) {
  const std::uint64_t cqe_ns = MonotonicNowNs();
  std::lock_guard<std::mutex> lock(mutex_);
  if (context == nullptr || !context->occupied) {
    std::terminate();
  }
  const bool success = result == static_cast<int>(block_size_bytes_);
  const int error_code = success ? 0 : (result < 0 ? -result : EIO);
  DirectionIoStats& direction_stats =
      context->task.direction == IoDirection::kLoad ? stats_.load
                                                     : stats_.store;
  const std::uint64_t queue_ns =
      context->dispatch_ns - context->task.enqueue_ns;
  const std::uint64_t dispatch_to_cqe_ns = cqe_ns - context->dispatch_ns;
  ++direction_stats.count;
  direction_stats.queue_ns_sum += queue_ns;
  direction_stats.queue_ns_max =
      std::max(direction_stats.queue_ns_max, queue_ns);
  direction_stats.dispatch_to_cqe_ns_sum += dispatch_to_cqe_ns;
  direction_stats.dispatch_to_cqe_ns_max = std::max(
      direction_stats.dispatch_to_cqe_ns_max, dispatch_to_cqe_ns);
  completions_.push_back(BlockCompletion{
      context->task.job_id,
      std::move(context->task.key),
      success,
      error_code,
  });
  if (context->task.direction == IoDirection::kLoad) {
    --load_in_flight_;
  } else {
    --store_in_flight_;
  }
  context->occupied = false;
  free_contexts_.push_back(context);
  --accepted_not_completed_;
  if (accepted_not_completed_ == 0) {
    idle_cv_.notify_all();
  }
}

}  // namespace uring_slab
