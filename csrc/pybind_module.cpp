#include "data_engine.h"

#include <pybind11/pybind11.h>

#include <cstddef>
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace py = pybind11;

namespace uring_slab {
namespace {

py::list ToPython(std::vector<BlockCompletion> completions) {
  py::list result(completions.size());
  for (std::size_t i = 0; i < completions.size(); ++i) {
    auto& completion = completions[i];
    result[i] = py::make_tuple(
        completion.job_id,
        py::bytes(completion.key),
        completion.success,
        completion.error_code);
  }
  return result;
}

class PyDataEngine {
 public:
  PyDataEngine(py::buffer primary_kv_view,
               std::string slab_path,
               std::size_t slab_bytes,
               std::size_t total_qd,
               std::size_t pending_capacity)
      : primary_owner_(std::move(primary_kv_view)) {
    py::buffer_info info = primary_owner_.request();
    if (info.readonly) {
      throw std::invalid_argument(
          "primary_kv_view 必须可写，否则无法执行 load");
    }
    if (info.strides.empty() || info.strides[0] <= 0) {
      throw std::invalid_argument(
          "primary_kv_view 第一维 stride 必须为正数");
    }
    const auto primary_bytes =
        static_cast<std::size_t>(info.size) *
        static_cast<std::size_t>(info.itemsize);
    const auto block_size_bytes =
        static_cast<std::size_t>(info.strides[0]);

    engine_ = std::make_unique<DataEngine>(
        info.ptr,
        primary_bytes,
        block_size_bytes,
        std::move(slab_path),
        slab_bytes,
        EngineOptions{
            .total_qd = total_qd,
            .pending_capacity = pending_capacity,
        });
  }

  void SubmitStore(JobId job_id,
                   py::bytes key,
                   std::uint64_t primary_slot,
                   SlotId secondary_slot) {
    Submit(job_id, std::move(key), primary_slot, secondary_slot,
           IoDirection::kStore);
  }

  void SubmitLoad(JobId job_id,
                  py::bytes key,
                  std::uint64_t primary_slot,
                  SlotId secondary_slot) {
    Submit(job_id, std::move(key), primary_slot, secondary_slot,
           IoDirection::kLoad);
  }

  py::list PollCompletions() {
    return ToPython(engine_->PollCompletions());
  }

  py::list Drain() {
    std::vector<BlockCompletion> completions;
    {
      py::gil_scoped_release release;
      completions = engine_->Drain();
    }
    return ToPython(std::move(completions));
  }

  void Shutdown() {
    py::gil_scoped_release release;
    engine_->Shutdown();
  }

  bool HasPendingWork() const { return engine_->HasPendingWork(); }
  py::dict StatsSnapshot() const {
    const EngineStats stats = engine_->StatsSnapshot();
    py::dict result;
    AddDirectionStats(result, "load", stats.load);
    AddDirectionStats(result, "store", stats.store);
    return result;
  }
  std::size_t BlockSizeBytes() const {
    return engine_->block_size_bytes();
  }

 private:
  static void AddDirectionStats(py::dict& result,
                                const char* prefix,
                                const DirectionIoStats& stats) {
    const std::string name(prefix);
    result[py::str(name + "_count")] = stats.count;
    result[py::str(name + "_queue_ns_sum")] = stats.queue_ns_sum;
    result[py::str(name + "_queue_ns_max")] = stats.queue_ns_max;
    result[py::str(name + "_dispatch_to_cqe_ns_sum")] =
        stats.dispatch_to_cqe_ns_sum;
    result[py::str(name + "_dispatch_to_cqe_ns_max")] =
        stats.dispatch_to_cqe_ns_max;
  }

  void Submit(JobId job_id,
              py::bytes key,
              std::uint64_t primary_slot,
              SlotId secondary_slot,
              IoDirection direction) {
    engine_->Submit(BlockIo{
        job_id,
        key.cast<std::string>(),
        primary_slot,
        secondary_slot,
        direction,
    });
  }

  // 持有 buffer exporter 的强引用，直到 C++ engine 已 join owner 且所有
  // 内核 I/O 都结束，防止 primary 内存被提前释放。
  py::buffer primary_owner_;
  std::unique_ptr<DataEngine> engine_;
};

}  // namespace
}  // namespace uring_slab

PYBIND11_MODULE(_uring_slab_engine, m) {
  using uring_slab::PyDataEngine;

  m.doc() = "基于 C++ io_uring 的 slab 数据引擎";

  py::class_<PyDataEngine>(m, "DataEngine")
      .def(py::init<py::buffer,
                    std::string,
                    std::size_t,
                    std::size_t,
                    std::size_t>(),
           py::arg("primary_kv_view"),
           py::arg("slab_path"),
           py::arg("slab_bytes"),
           py::arg("total_qd") = 128,
           py::arg("pending_capacity") = 4096)
      .def("submit_store",
           &PyDataEngine::SubmitStore,
           py::arg("job_id"),
           py::arg("key"),
           py::arg("primary_slot"),
           py::arg("secondary_slot"))
      .def("submit_load",
           &PyDataEngine::SubmitLoad,
           py::arg("job_id"),
           py::arg("key"),
           py::arg("primary_slot"),
           py::arg("secondary_slot"))
      .def("poll_completions", &PyDataEngine::PollCompletions)
      .def("drain", &PyDataEngine::Drain)
      .def("shutdown", &PyDataEngine::Shutdown)
      .def("has_pending_work", &PyDataEngine::HasPendingWork)
      .def("stats_snapshot", &PyDataEngine::StatsSnapshot)
      .def_property_readonly(
          "block_size_bytes", [](const PyDataEngine& self) {
            return self.BlockSizeBytes();
          });
}
