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

## Linux 特性

- 单个 owner 线程独占 ring 的提交和收割。
- 使用 `IORING_SETUP_SINGLE_ISSUER` 和 `IORING_SETUP_SUBMIT_ALL`；ring
  与所有 SQE 都由同一个 owner 线程创建和提交。
- 不使用 `IORING_SETUP_SQPOLL`。
- slab fd 注册为 fixed file 的第 0 项。
- primary 内存不注册为 fixed buffer。
- 使用 `statx(STATX_DIOALIGN)` 获取并校验 direct I/O 对齐要求。
