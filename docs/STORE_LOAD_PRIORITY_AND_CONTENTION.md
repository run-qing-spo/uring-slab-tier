# vLLM 0.24.0 store/load 优先级与资源竞争

状态：**SOURCE-VERIFIED**

源码版本：
`ee0da84ab9e04ac7610e28580af62c365e898389`（vLLM `v0.24.0`）

本文只回答：

1. store 与 load 在各层分别有什么优先级；
2. 两者竞争哪些资源；
3. “满了以后还能不能 store/load”在源码中分别意味着什么；
4. 哪些结论可由源码直接确定，哪些仍需要实验。

术语：

- GPU→CPU 称为 GPU store；
- CPU→secondary 称为 cascade，也称 secondary store；
- secondary→CPU 称为 promotion，也称 secondary load；
- CPU→GPU 称为 GPU load。

## 1. 结论

vLLM 0.24.0 没有一个贯穿全路径的“load 严格优先于 store”调度器。实际是三层
局部策略的组合：

1. **CPU slot 分配：本 step 的新 promotion 先于新 GPU store。**
   waiting request 在 lookup 时立即为 promotion 预留 CPU slot；到
   `build_connector_meta()` 时，manager 先 flush 这些 promotions，随后才构造
   新的 GPU→CPU store job。
2. **GPU↔CPU：load 在当前 step 提交，store 延迟到下一 step。**
   当前 metadata 中的 CPU→GPU loads 会在 `start_kv_transfers()` 提交；本 step
   新产生的 GPU→CPU stores 先缓存，到下一 engine step 开头才提交，以免影响
   token sampling。
3. **CPU↔FS：读写各有一组偏好线程，但不是全局抢占优先级。**
   load 进入 load queue，store 进入 store queue。read-priority threads 先取
   load，write-priority threads 先取 store；自己的队列为空时，两组线程都会
   帮另一方向。

所以准确表述应当是：

> vLLM 对关键路径上的 load 有局部优待，但 FS backend 仍允许 store 与 load
> 并行占用 CPU primary、线程、文件系统和同一存储设备；已经开始的 store
> 不会被新 load 抢占。

高 store pressure 可以通过两条独立路径伤害 load：

- **空间路径**：cascade 在排队和 I/O 期间 pin 住 CPU primary slot，使
  promotion 无处落盘；
- **设备路径**：O_DIRECT store 和 load 仍在同一文件系统/设备上竞争带宽、
  device queue、元数据和 CPU。

## 2. 本 step 中的 CPU slot 优先级

### 2.1 promotion 在 lookup 时立即占 slot

secondary lookup 命中后，`_initiate_promotion()` 立即调用
`primary.prepare_write([key])`。新 block 初始 `ref_cnt=-1`，表示目标 slot
已分配但数据尚不可读。实际 `submit_load()` 被延迟到
`on_schedule_end()` 批量提交。

源码：

- [`TieringOffloadingManager.lookup()`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L227-L269)
- [`_initiate_promotion()`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L271-L318)
- [`BlockStatus`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/cpu/policies/base.py#L10-L33)

promotion 按 block 单独预留，不是整个 prefix 一次性原子预留。因此长 prefix
可能只成功预留前一部分，后续 block 因 primary pressure 暂时失败。

### 2.2 promotion flush 先于新 store 构造

`build_connector_meta()` 的顺序是：

```text
更新本 step block 状态
→ manager.on_schedule_end()
    → 收割 secondary completions
    → flush pending promotions 到 secondary submit_load()
→ 处理必须 flush 的旧 GPU jobs
→ _build_store_jobs()
    → primary.prepare_store()
```

源码：

- [`build_connector_meta()`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py#L1014-L1051)
- [`TieringOffloadingManager.on_schedule_end()`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L566-L578)
- [`_flush_pending_promotions()`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L320-L342)
- [`_build_store_jobs()` 调用 prepare_store](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py#L907-L922)

这能证明：**对本 step 尚未分配的 CPU slot，promotion 先到。**

但它不能证明全局 load priority，因为以下旧工作可能早已 pin 住 slot：

- 上一 step 的 GPU→CPU store；
- 已提交但未完成的 CPU→FS cascade；
- 已提交但未完成的 FS→CPU promotion；
- 已提交但未完成的 CPU→GPU load；
- 已完成 I/O、但 completion 尚未被 scheduler poll 到的 job。

新 promotion 不会取消、抢占或迁移这些旧工作。

## 3. CPU primary 的真实竞争规则

promotion 和新 store 最终使用同一个 `CPUOffloadingManager` block pool。
可供新写入使用的容量是：

```text
尚未分配的 slot
+ free list
+ ref_cnt == 0 且不在 protected keys 中的可逐出 slot
```

以下 slot 都不可逐出：

- `ref_cnt=-1`：GPU store 或 promotion 正在写；
- `ref_cnt>0`：正在作为 cascade 或 CPU→GPU load 的源；
- 当前 `prepare_store()` 输入中的 protected keys。

源码：

- [`prepare_load()` 增加 ref_cnt](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/cpu/manager.py#L131-L165)
- [`prepare_store()` 容量与 eviction](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/cpu/manager.py#L167-L236)
- [`complete_store()` 使写入 slot 可读或回滚](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/cpu/manager.py#L238-L269)
- [LRU 只逐出 `ref_cnt==0` block](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/cpu/policies/lru.py#L54-L77)

### 3.1 cascade 为什么会压住 promotion

GPU→CPU store 完成后，`TieringOffloadingManager.complete_store()` 对每个
secondary tier 调用一次 `primary.prepare_read()`，增加对应 block 的
`ref_cnt`，然后调用 `tier.submit_store()`。只有 secondary store completion
被 manager 收割后，才调用 `primary.complete_read()` 解 pin。

源码：

- [cascade 提交和 pin](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L481-L537)
- [completion 解 pin](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L192-L225)

因此 cascade 占用 CPU slot 的时间不是纯写盘时间，而是：

```text
FS queue wait
+ 文件/设备 I/O
+ FS completion queue wait
+ scheduler 下一次 poll 的观测延迟
```

FS 越慢或 store backlog 越长，CPU primary 中不可逐出的 slot 越多。即使磁盘
读带宽尚未饱和，promotion 也可能先因 staging slot 不足失败。

### 3.2 promotion 自己也会形成 staging pressure

promotion 在 lookup 时就分配 `ref_cnt=-1` 的目标 slot，直到 FS load 完成并
被 manager 收割后才由 `complete_write()` 标为 ready。并发 revisit wave
越大，promotion 同时占据的 staging slots 越多。

这说明 load pressure 有自限效应：

```text
更多并发 promotion
→ 更多 reserved primary slots
→ 后续 promotion/store 更难分配
```

### 3.3 primary 满时，store 与 load 的结果不同

“primary 满”还要区分是否存在可逐出 idle block：

- 有足够 `ref_cnt==0` block：可以 eviction 后继续；
- 没有足够可逐出 block：
  - promotion 的 `prepare_write([key])` 返回 `None`，该次 lookup 把此 block
    当作不可用；
  - 新 GPU store 的 `prepare_store()` 返回 `None`，store cursor 不前进，
    请求后续被调度时仍有机会重试。

源码：

- [promotion primary-full 分支](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L294-L303)
- [store 失败时不推进 cursor](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py#L907-L920)

这里存在不对称：store 可以在后续 step 重试；promotion 没有独立的永久等待
队列。多 block lookup 中，如果前面已有 block 返回 `None`，整个 request
可能因为 defer 而在下一 step 再查；但不能把这种偶然重试写成可靠的
promotion backpressure。

## 4. GPU↔CPU 路径的优先级

本 step 新生成的 GPU→CPU store 不会立即提交。worker 在本 step
`get_finished()` 中把它放进 `_unsubmitted_store_jobs`，下一 engine step 的
`start_kv_transfers()` 才提交。

同一个 `start_kv_transfers()` 中，源码先提交上一 step 延迟的 stores，再提交
当前 metadata 的 loads；但两个方向使用独立的
`SingleDirectionOffloadingHandler` 和独立 CUDA streams，所以这个 Python
调用顺序不构成 load 等待全部 store 完成的串行屏障。

更关键的优待来自：

- GPU→CPU handler 在 transfer stream 上等待当前 compute stream；
- CPU→GPU load 没有这个 wait；
- 每个方向内部严格按提交顺序串行链接，各方向之间没有同样的 FIFO 链。

源码：

- [store 延迟到下一 step](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading_connector.py#L111-L120)
- [`start_kv_transfers()` 的提交顺序](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/worker.py#L280-L296)
- [两个独立方向 handler](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/cpu/gpu_worker.py#L477-L536)
- [方向内 FIFO 和 GPU→CPU compute barrier](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/cpu/gpu_worker.py#L169-L176)
- [实际 stream/event ordering](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/cpu/gpu_worker.py#L371-L423)

因此：

- load 是 request-critical；
- store 被刻意移出当前 sampling 尾部；
- 但两个方向仍可能竞争 PCIe、copy engine、pinned host memory bandwidth 和
  CPU descriptor construction。

这些资源上的实际干扰大小不能由 Python 调用顺序推出，必须测量。

### 4.1 同一 request 与跨 request 的规则不同

connector 的 `req_status.transfer_jobs` 只跟踪 GPU↔CPU jobs，不包含
CPU↔secondary 的 cascade/promotion：

- 同一个 request 只要还有 connector job，新的 lookup/load 会被延迟；
- 创建 CPU→GPU load 时，断言该 request 没有其他 connector job；
- 创建 GPU→CPU store 时，如果已有 job，只要求已有 job 也是 store，所以
  同一 request 可以同时有多个 store；
- 不同 request 的 load/store 可以重叠；
- secondary cascade 可以与其他 request 的 promotion/load 重叠。

源码：

- [pending transfer 时延迟 request](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py#L658-L681)
- [load 要求 request 无其他 connector job](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py#L762-L775)
- [store 允许与同 request 的其他 stores 共存](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py#L977-L998)

因此“同一 request 内不会同时有 connector load/store”不能外推成“系统没有
mixed read/write”。跨 request 以及 secondary 层仍然可以形成真实的混合压力。

## 5. FS 双队列的准确语义

### 5.1 任务粒度

`FileSystemTierManager.submit_store/load()` 把一个 job 拆成“每个 KV block
一个 callable”，整批分别追加到 store/load deque。job 只有在最后一个 block
task 完成后才产生一个 completion。

源码：

- [FS submit_store/load](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/fs/manager.py#L143-L179)
- [`JobState` 聚合 block completion](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/fs/thread_pool.py#L21-L47)

结果是：

- 同一方向按 block task FIFO，而不是 request 或 job 公平调度；
- 一个大 job 的 tasks 连续入队，后来的同方向小 job 排在其后；
- job sojourn 由该 job 最慢的 block 决定；
- 一个 block 失败不会取消同 job 其余 tasks。

`tasks` 是 lazy generator。`enqueue_*()` 持有共享 condition lock 时完整迭代
generator，因此路径计算、`partial` 构造和所有 block 的 deque append 都发生
在 scheduler 调用线程中，完成后才 `notify(n_tasks)`。所以
`submit_store/load()` 不执行磁盘 I/O，但同步成本是 O(blocks/job)，并非 O(1)；
大 job 入队期间 worker 也不能持同一把锁领取任务。

### 5.2 “read-priority/write-priority”不是一个全局排序

默认有：

- 16 个 read-priority threads；
- 16 个 write-priority threads。

每个 worker 在领取下一项任务时：

```text
read worker:  load_q 非空 ? load : store
write worker: store_q 非空 ? store : load
```

源码：

- [线程组和两个 deque](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/fs/thread_pool.py#L50-L91)
- [worker 选队列](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/fs/thread_pool.py#L153-L180)
- [默认线程数](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/fs/manager.py#L83-L100)

在默认两组线程都大于 0 时：

- 两个队列持续非空：大致保留 16 个 worker 给 load、16 个给 store；
- 只有 load：最多 32 个 worker 都可服务 load；
- 只有 store：最多 32 个 worker 都可服务 store。

这是**work-conserving 的软分区**，不是 read 抢占 write。

### 5.3 非抢占导致的短期 priority inversion

worker 只在一个 block I/O 完成后才重新检查队列。如果 load queue 原本为空，
read-priority threads 可能已经 fallback 去执行 store。新 load 到达时：

- 已经开始的 blocking store syscall 不会被抢占；
- store queue 仍非空时，write-priority threads 继续取 store；
- load 至少要等一个 read-priority worker 完成当前 store 后才获得其保留份额。

所以 read priority 限制了持续竞争下的线程份额，却不给 load 提供确定的
排队延迟上界。

### 5.4 队列无界，也没有 backpressure

两个队列都是普通 `deque`；`enqueue_load/store()` 直接 append 后返回。源码中
没有：

- max queue jobs/blocks；
- queue full 返回值；
- admission control；
- 按字节计费的 outstanding 上限；
- device queue depth 参数。

因此不存在“FS queue 满了不能 store/load”。它会继续接受任务，代价是：

- queue delay 增长；
- `JobState`、callables 和 keys 占用更多内存；
- 被 cascade pin 住的 CPU primary slots 增多；
- completion 更晚被上层看见。

`n_read_threads=0` 或 `n_write_threads=0` 也没有构造参数校验。如果把一组设为
0，另一方向持续有任务时，低优先队列可能饥饿；两组都为 0 时 job 永不完成。
正式实验不得使用这种退化配置。

## 6. 文件系统和设备竞争

FS load/store 都是 O_DIRECT 数据路径，但 O_DIRECT 不代表两者互不干扰。

每个 store block：

```text
exists
→ mkdir -p
→ open(temp, O_DIRECT)
→ write
→ close
→ replace(temp, final)
```

每个 load block：

```text
open(final, O_DIRECT)
→ readv 直接写 primary memoryview
→ close
```

源码：

- [`store_block()`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/fs/io.py#L32-L72)
- [`load_block()`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/fs/io.py#L75-L101)

两者仍共同竞争：

- block device bandwidth、IOPS 和 device queue；
- 文件系统 inode/dentry/extent 操作；
- store 的目录创建、临时文件和 rename；
- Python worker、系统调用和 context switch；
- primary memory bandwidth。

已经下发到内核/设备的 store 不会因为随后出现 load 而被 FS pool 撤销或
重新排序。设备内部是否以及如何重排请求不由 vLLM 控制。

## 7. lookup 与 completion 的额外竞争

FS existence lookup 不使用上述 I/O thread pool，而是单独一个 background
thread。每个 scheduler step 累积一批 key，`on_schedule_end()` 后发送到无界
`SimpleQueue`；结果在后续 lookup 时 drain。

源码：

- [AsyncLookupManager 的线程和队列](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/async_lookup.py#L71-L105)
- [lookup/flush/drain](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/async_lookup.py#L125-L173)
- [FS batch `exists`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/fs/manager.py#L45-L59)

lookup 不与数据 I/O 共用用户态线程，但会共享文件系统 metadata path、CPU 和
调度时机。大量 background stores 创建文件时，lookup 可能受到 metadata
竞争；影响大小需要实验。

FS lookup 还存在一个可由源码确定的 store/lookup 可见性边界：

- store 只有在 `os.replace(temp, final)` 后才发布 final path；
- lookup 只做 final path 的 `exists`；
- 如果 lookup 恰好发生在 replace 前，会得到 `False`；
- 这个 negative result 会留在该 request 的 `LookupState` 中，直到 request
  cleanup；store completion 不会主动 invalidation 它。

因此 FS 并不保证“同 key store-in-flight lookup 返回 `None`”。正常上层的
primary in-flight state 会挡住很多同进程竞争，但跨进程共享目录或特殊时序
仍可能看到 cached miss。这个行为不应被误写成读写优先级。

secondary completion 最多在 manager 的 step poll 点被上层承认。job 实际
I/O 已完成到 scheduler 观察之间，CPU pin 仍未释放。

源码：

- [每 step completion gate](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L179-L225)
- [`has_pending_push_work()` 保持 engine 继续 step](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py#L1053-L1059)

## 8. “满了以后能不能 store/load”汇总

| “满”的位置 | store | load |
|---|---|---|
| CPU primary 仅容量用满，但有 idle evictable slot | eviction 后可继续 | eviction 后可继续 |
| CPU primary 全部被 pin/protect | `prepare_store()` 返回 `None`，cursor 暂不推进 | promotion 预留失败；该 lookup 不能把该 block 当 hit |
| FS 用户态任务队列 | 无上限，继续接收并积压 | 无上限，继续接收并积压 |
| 磁盘空间 | store block 抛错，job 最终失败；无容量回收 | 已存在且可读的文件仍可 load；失败文件会被删除 |
| 存储设备队列/带宽饱和 | 继续排队，延迟上升 | 有 read workers，但不能抢占已发出的 writes，延迟上升 |
| GPU↔CPU 同方向 transfer backlog | store 方向 FIFO | load 方向 FIFO |

原生 FS 没有磁盘容量参数、FS LRU、水位或后台回收。主 A/B 中 uring-slab
必须配置为不会发生 secondary-capacity eviction，否则 candidate 的有限容量
与 FS 的无界保留会改变 hit 集合，比较的就不再只是 engine。

失败后的重试也不对称：

- CPU store admission 失败时 cursor 不推进，后续 step 可重试；
- promotion admission 失败通常按 miss/recompute 处理，没有专门等待队列；
- secondary cascade 返回失败时，上层只解 pin，不自动重试；
- secondary promotion 返回失败时，目标 primary slot 被回滚；
- `submit_store/load()` 若直接抛异常，上层已经登记的 job 和 pin/slot 没有
  try/rollback 路径，因此 candidate 的可恢复失败必须通过最终
  `JobResult(success=False)` 表达，不能从 submit 抛出。

## 9. 对 uring-slab 设计与实验的直接约束

### 9.1 不应强制“内部 QD 相同”

FS 使用 16+16 blocking threads，uring-slab 使用 ring/QD；内部提交模型正是
treatment 的一部分。公平条件应是两侧接收完全相同的：

- job/key/block/byte stream；
- submit 时间和方向；
- blocks per job；
- offered outstanding jobs/bytes；
- CPU primary 容量与 poll 时机。

不能要求 `n_threads == queue_depth`。

### 9.2 candidate 的读写仲裁属于 engine 方法

上层接口只要求 `submit_load/store()` 轻量非阻塞，没有规定内部必须复刻
FS 的线程分区。uring-slab 可以实现 load-aware submission，但如果它对结果
贡献明显，应在微基准增加诊断性消融：

```text
统一 FIFO submission
vs
load-aware submission
```

这仍是 engine 内部设计，不属于 scheduler 协同优化。

### 9.3 必须观测两类竞争

仅记录磁盘 read/write throughput 不足以解释结果。至少需要：

1. secondary queue：
   - direction；
   - submit→engine-dispatch；
   - dispatch→completion；
   - outstanding jobs/blocks/bytes；
2. CPU primary：
   - promotion attempted/accepted/rejected-primary-full；
   - slots free/evictable；
   - pinned-by-cascade；
   - reserved-by-promotion；
   - pinned-by-CPU→GPU；
   - completion→scheduler-observed delay。

否则无法区分：

```text
load 慢是因为设备争用
vs
load 根本没有被接受
vs
load 已完成但 slot 尚未解 pin
```

## 10. 源码已经证明与仍需实验的边界

源码已经证明：

- 本 step promotion slot reservation 先于新 store allocation；
- GPU store 被延迟一 step，GPU load 不采用同样延迟；
- FS 是两个无界队列和两组 preference workers；
- preference 是非抢占、work-conserving 的软分区；
- store/load 共享 CPU primary 和同一文件系统/设备；
- cascade queue time 会延长 CPU slot pin；
- FS 没有磁盘容量管理和任务队列 backpressure。

源码不能证明：

- 默认 16+16 下实际读写带宽如何分配；
- 多大 store pressure 会显著推高 load p95/p99；
- 主要瓶颈是 primary pin、metadata、device 还是 scheduler observation；
- io_uring/slab 的优势在哪个 load/store 压力下出现；
- engine micro 优势能否转化为 TTFT 或 SLO goodput。

因此下一步实验不是为了“发现有没有优先级”，而是量化源码已经揭示的两条
竞争路径：

```text
store pressure → cascade pin duration → promotion acceptance
store pressure → device/metadata contention → accepted load latency
```
