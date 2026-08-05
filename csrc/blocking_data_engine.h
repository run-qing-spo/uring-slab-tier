#pragma once

#include "data_engine.h"

#include <condition_variable>
#include <cstddef>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace uring_slab {

// M2 消融：单 slab + C++ blocking pread/pwrite worker pool。
class BlockingDataEngine {
 public:
  BlockingDataEngine(void* primary_base,
                     std::size_t primary_bytes,
                     std::size_t block_size_bytes,
                     std::string slab_path,
                     std::size_t slab_bytes,
                     std::size_t workers,
                     std::size_t pending_capacity);
  ~BlockingDataEngine();

  BlockingDataEngine(const BlockingDataEngine&) = delete;
  BlockingDataEngine& operator=(const BlockingDataEngine&) = delete;

  void Submit(BlockIo task);
  std::vector<BlockCompletion> PollCompletions();
  std::vector<BlockCompletion> Drain();
  void Shutdown();
  bool HasPendingWork() const noexcept;
  EngineStats StatsSnapshot() const noexcept;
  void ResetStats();

  std::size_t block_size_bytes() const noexcept { return block_size_bytes_; }

 private:
  void InitializeSlab();
  void DestroySlab() noexcept;
  void ValidateGeometry() const;
  void ValidateTask(const BlockIo& task) const;
  void WorkerLoop();
  bool CanDispatchLocked() const;
  std::vector<BlockCompletion> TakeCompletionsLocked();

  std::byte* const primary_base_;
  const std::size_t primary_bytes_;
  const std::size_t block_size_bytes_;
  const std::string slab_path_;
  const std::size_t slab_bytes_;
  const std::size_t workers_count_;
  const std::size_t store_limit_;
  const std::size_t pending_capacity_;

  int slab_fd_ = -1;
  mutable std::mutex mutex_;
  std::condition_variable work_cv_;
  std::condition_variable idle_cv_;
  std::deque<BlockIo> load_pending_;
  std::deque<BlockIo> store_pending_;
  std::deque<BlockCompletion> completions_;
  std::vector<std::thread> workers_;
  std::size_t load_in_flight_ = 0;
  std::size_t store_in_flight_ = 0;
  std::size_t accepted_not_completed_ = 0;
  EngineStats stats_;
  bool stopping_ = false;
};

}  // namespace uring_slab
