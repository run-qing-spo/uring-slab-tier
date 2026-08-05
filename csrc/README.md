# C++ DataEngine V1

## 所有权

- pybind 包装层持有 `primary_kv_view` 的强引用。
- owner 线程负责创建、锁定、截断、预分配、打开、注册和关闭 slab 文件，
  同时负责创建与销毁 io_uring。
- `shutdown()` 会等待 ring owner 线程退出；返回后不再有请求访问 primary
  内存。

V1 每次构造都会创建一个读取结果为零的新 slab：

```text
open(O_DIRECT) -> ftruncate(0) -> posix_fallocate(slab_bytes)
```

## 接收与调度

- `pending_capacity` 限制 load/store 两个用户态待处理队列的总任务数。
- 待处理队列满时，立即产生一个 `EAGAIN` completion。
- `total_qd` 限制提交给内核但尚未完成的请求总数。
- `load_reserve = max(1, total_qd / 4)`。
- `store_qd = total_qd - load_reserve`，store 不允许借用 load reserve。
- 只要存在待处理 load，调度器就优先选择 load。

SQ 和 CQ 不按 load/store 静态分区。SQ 容量覆盖 `total_qd`，CQ 容量覆盖
`2 * total_qd`。

## Completion 语义

每个已接受的 `BlockIo` 恰好产生一个 `BlockCompletion`，包括队列满、
短读写和 I/O 错误。`poll_completions()` 非阻塞地消费当前全部可见结果；
`drain()` 等待所有已接受任务结束，并消费此前尚未被 poll 的 completion。

## 最小 I/O 计时

`stats_snapshot()` 返回进程生命周期内的累计快照，load/store 分开统计：

- `count`：已收割 CQE 的 block 数；
- `queue_ns_sum/max`：任务被接受到 owner 取出任务的等待时间；
- `dispatch_to_cqe_ns_sum/max`：owner 取出任务到收割 CQE 的时间。

正式实验窗口在开始和结束时各取一次快照并相减。后一段包含 SQE 准备、
提交、内核及设备处理和 CQE 可见时间，不应单独解释成裸设备延迟。

`submit_batch_size=0` 保持默认的尽可能批量提交；M3 消融使用值 1。值 1 时
owner 仍会连续提交直到填满可用 QD，只把每次 `io_uring_submit()` 限制为一个
SQE，不会把有效 QD 一并降为 1。快照中的 `submit_calls`、
`submitted_blocks` 和 `submit_batch_size_max` 用于验证消融是否生效。

## Linux 特性

- 单个 owner 线程独占 ring 的提交和收割。
- 使用 `IORING_SETUP_SINGLE_ISSUER` 和 `IORING_SETUP_SUBMIT_ALL`；ring
  与所有 SQE 都由同一个 owner 线程创建和提交。
- 不使用 `IORING_SETUP_SQPOLL`。
- slab fd 注册为 fixed file 的第 0 项。
- primary 内存不注册为 fixed buffer。
- 使用 `statx(STATX_DIOALIGN)` 获取并校验 direct I/O 对齐要求。
