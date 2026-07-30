#pragma once

#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

struct io_uring;

namespace uring_slab {

using JobId = std::uint64_t;
using SlotId = std::uint64_t;

enum class IoDirection : std::uint8_t {
  kStore,
  kLoad,
};

struct BlockIo {
  JobId job_id;
  std::string key;
  std::uint64_t primary_slot;
  SlotId secondary_slot;
  IoDirection direction;
};

struct BlockCompletion {
  JobId job_id;
  std::string key;
  bool success;
  int error_code;  // 成功为 0；失败为 EAGAIN/EIO 等正 errno 值。
};

struct EngineOptions {
  std::size_t total_qd;
  std::size_t pending_capacity;
};

// 单个 owner 线程独占 io_uring，负责提交 SQE 和收割 CQE。
// Python 调用线程只向两个待处理队列追加任务，并消费完成结果。
class DataEngine {
 public:
  DataEngine(void* primary_base,
             std::size_t primary_bytes,
             std::size_t block_size_bytes,
             std::string slab_path,
             std::size_t slab_bytes,
             EngineOptions options);
  ~DataEngine();

  DataEngine(const DataEngine&) = delete;
  DataEngine& operator=(const DataEngine&) = delete;

  // 复制任务元数据后立即返回。队列满仍视为已接受，但立即产生 EAGAIN
  // completion；生命周期或调用契约错误则抛异常。
  void Submit(BlockIo task);

  // 消费式非阻塞收割：每个 completion 只交付一次。
  std::vector<BlockCompletion> PollCompletions();

  // 等待所有已接受任务结束，并消费式返回尚未被 poll 的 completion。
  // 调用方不得在 Drain() 执行期间并发提交。
  std::vector<BlockCompletion> Drain();

  // 停止接收新任务，完成已接受任务并 join owner。返回后，不再有任何线程
  // 或内核请求访问 primary 内存。
  void Shutdown();

  bool HasPendingWork() const noexcept;

  std::size_t block_size_bytes() const noexcept { return block_size_bytes_; }
  std::size_t total_qd() const noexcept { return total_qd_; }
  std::size_t store_qd() const noexcept { return store_qd_; }

 private:
  struct RequestContext {
    BlockIo task;
    bool occupied = false;
  };

  void InitializeSlabAndRing();
  void ValidateGeometry() const;
  void ValidateTask(const BlockIo& task) const;
  void WakeOwner() noexcept;
  void DrainWakeFd() noexcept;

  void OwnerLoop();
  void DispatchAvailable();
  void SubmitPrepared(unsigned prepared);
  void ReapAvailable();
  void Finish(RequestContext* context, int result);
  bool CanDispatchLocked() const;
  RequestContext* AcquireContextLocked();
  std::vector<BlockCompletion> TakeCompletionsLocked();

  std::byte* const primary_base_;
  const std::size_t primary_bytes_;
  const std::size_t block_size_bytes_;
  const std::string slab_path_;
  const std::size_t slab_bytes_;
  const std::size_t total_qd_;
  const std::size_t load_reserve_;
  const std::size_t store_qd_;
  const std::size_t pending_capacity_;

  int slab_fd_ = -1;
  int wake_fd_ = -1;
  io_uring* ring_ = nullptr;
  bool fixed_file_registered_ = false;

  mutable std::mutex mutex_;
  std::condition_variable idle_cv_;
  std::deque<BlockIo> load_pending_;
  std::deque<BlockIo> store_pending_;
  std::deque<BlockCompletion> completions_;

  std::vector<std::unique_ptr<RequestContext>> contexts_;
  std::vector<RequestContext*> free_contexts_;
  std::size_t load_in_flight_ = 0;
  std::size_t store_in_flight_ = 0;
  std::size_t accepted_not_completed_ = 0;

  bool stopping_ = false;
  std::thread owner_;
};

}  // namespace uring_slab
