# vLLM 0.24.0 scheduler / CPU primary / secondary 时序

状态：**SOURCE-DERIVED / 尚未用运行时 trace 验证**

源码版本：vLLM `v0.24.0`  
commit：`ee0da84ab9e04ac7610e28580af62c365e898389`

本文只回答一件事：在不修改 vLLM 上层语义的前提下，一份 KV 是怎样在
GPU、CPU primary 和 secondary 之间流动的，scheduler 在什么时候创建任务、
提交任务、观察完成，以及哪些等待会进入端到端指标。

本文中的结论来自上述固定 commit，不用旧实验结果作证。源码链接均固定到该
commit。

## 1. 先统一术语：系统中不是一种 job，而是两种

```text
                    scheduler process
             ┌────────────────────────────┐
             │ OffloadingConnectorScheduler│
             │ TieringOffloadingManager    │
             │ CPU primary metadata/index  │
             │ secondary manager           │
             └──────────────┬─────────────┘
                            │ shared CPU memoryview
                            ▼
GPU KV  ← connector job →  CPU primary  ← tier job →  secondary
```

| job | 数据方向 | 谁创建并跟踪 | 谁执行 |
|---|---|---|---|
| connector load | CPU primary → GPU | `OffloadingConnectorScheduler._jobs` | worker 的 CPU/GPU transfer worker |
| connector store | GPU → CPU primary | `OffloadingConnectorScheduler._jobs` | worker 的 CPU/GPU transfer worker |
| tier promotion | secondary → CPU primary | `TieringOffloadingManager._transfer_jobs` | secondary backend |
| tier cascade | CPU primary → secondary | `TieringOffloadingManager._transfer_jobs` | secondary backend |

两类 job 使用不同的 job ID、完成队列和收账路径。不能把
`secondary.get_finished_jobs()` 和 worker 的 GPU↔CPU completion 当成同一个
完成事件。

CPU primary 的 `read/write` 只是同一套 CPU cache 操作在 secondary 视角下的
别名：

```text
prepare_read  = prepare_load   # CPU 是 source，pin
complete_read = complete_load  # CPU source 使用完，unpin
prepare_write = prepare_store  # CPU 是 destination，reserve
complete_write= complete_store # CPU destination ready / rollback
```

源码：
[tiering/manager.py#L62-L102](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L62-L102)。

## 2. 不要用“N 轮”代替三种不同的时钟

本文区分：

1. **scheduler pass**：一次 `scheduler.schedule()`；
2. **worker invocation**：一次 `execute_model()`，其中包含 pre-forward、
   forward/no-forward、post-forward；
3. **completion observation**：异步 I/O 可能已经完成，但必须到规定的 poll
   点才对 scheduler 可见。

同步 engine 路径的外层顺序是：

```text
scheduler.schedule()
    → model_executor.execute_model()
    → scheduler.update_from_output()
```

源码：
[engine/core.py#L479-L508](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/engine/core.py#L479-L508)。

但 async scheduling 的 batch queue 可以在前一份 worker output 尚未执行
`update_from_output()` 时继续 schedule 新 batch。因此下文的 L0/L1/L2 是
**逻辑依赖阶段**，不是严格的墙钟“第 N、N+1、N+2 轮”。

源码：
[engine/core.py#L519-L607](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/engine/core.py#L519-L607)。

## 3. 一次 scheduler pass 中与 offloading 相关的总顺序

```text
schedule waiting requests
│
├─ 1. connector prefix lookup
│     └─ manager 首次触及时 poll secondary completion
│
├─ 2. 若 external hit ready：分配 GPU blocks
│
├─ 3. connector.update_state_after_alloc()
│     ├─ CPU primary prepare_load / pin
│     └─ 创建本 pass 的 CPU→GPU connector load job
│
└─ 4. connector.build_connector_meta()
      ├─ 更新 request/block 状态
      ├─ manager.on_schedule_end()
      │    ├─ flush secondary→CPU promotions
      │    └─ flush FS async existence lookups
      ├─ 建立必要的 GPU store flush fence
      ├─ _build_store_jobs()
      │    ├─ CPU primary prepare_store / reserve
      │    └─ 创建本 pass 的 GPU→CPU connector store job
      └─ 打包已有 load/store/flush metadata

worker invocation
│
├─ pre-forward
│    ├─ 提交上一个 invocation 延迟的 GPU→CPU stores
│    ├─ 等待本 pass 要求的 jobs_to_flush
│    └─ 提交本 pass 的 CPU→GPU loads
│
├─ model forward，或 zero-token 时走 no_forward
│
└─ post-forward
     ├─ 把本 pass 新建的 stores 放进“下次提交”队列
     └─ 非阻塞收取已完成的 GPU↔CPU jobs

scheduler.update_from_output()
└─ 消费 worker completion：
     ├─ CPU→GPU 完成：CPU unpin；request 记为 recv finished
     └─ GPU→CPU 完成：CPU ready；立即发起 CPU→secondary cascade
```

关键源码：

- waiting lookup 与 `None` 延迟：
  [core/sched/scheduler.py#L672-L749](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/core/sched/scheduler.py#L672-L749)；
- GPU block allocation 和 `update_state_after_alloc()`：
  [core/sched/scheduler.py#L866-L938](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/core/sched/scheduler.py#L866-L938)；
- connector metadata 的真实构建顺序：
  [offloading/scheduler.py#L1014-L1051](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py#L1014-L1051)；
- worker pre/post：
  [offloading/worker.py#L271-L340](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/worker.py#L271-L340)。

两个容易误读的点：

- `prepare_load()` 和 CPU→GPU job 的创建发生在
  `update_state_after_alloc()`，早于 `build_connector_meta()`；后者只打包该
  job。源码：
  [offloading/scheduler.py#L683-L779](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py#L683-L779)。
- 即使本 batch 的 `total_num_scheduled_tokens == 0`，`no_forward()` 仍调用
  pre-forward 和 post-forward，所以纯 transfer pass 仍能推进。
  源码：
  [worker/gpu/kv_connector.py#L98-L105](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/worker/gpu/kv_connector.py#L98-L105)。

## 4. 完整 read path：新 request 的原生 FS hit

这里假设 GPU local cache 和 CPU primary 都 miss，而需要的连续 prefix 确实
存在于原生 FS secondary。`None` 的含义是“现在无法给出可调度的 hit 长度，
稍后再问”，不是 miss。

### L0：先做异步 existence lookup

新 request 只有在 `num_computed_tokens == 0` 的 waiting 路径查询 connector。
manager 先查 CPU primary；primary miss 后才查 secondary。

原生 FS 对该 request/key 的第一次 lookup 通常：

1. 建立 lookup state；
2. 返回 `None`；
3. scheduler 跳过该 request；
4. `on_schedule_end()` 才把本 pass 累积的 `path.exists` 查询批量交给后台线程。

如果后台查询在下一 pass 前尚未完成，request 可以继续得到 `None`，所以 L0
并不保证只占一个 pass。

源码：

- scheduler 处理 `None`：
  [core/sched/scheduler.py#L722-L738](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/core/sched/scheduler.py#L722-L738)；
- primary → secondary lookup 顺序：
  [tiering/manager.py#L228-L269](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L228-L269)；
- FS lookup state 与 flush：
  [async_lookup.py#L125-L158](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/async_lookup.py#L125-L158)。

`on_request_finished()` 会清理该 request 的 FS lookup state。因此“相同 key
以前查过”不等于一个新 request 一定免掉 L0；是否能免掉，取决于是否仍有共享
的活跃 lookup state，而非 secondary data 本身是否 resident。

### L1：FS hit 后，先 promotion 到 CPU primary

当 FS lookup 返回 `True`：

1. manager 立即调用 CPU primary `prepare_write()`；
2. 预留 CPU slot，状态是 `ref_cnt=-1`；
3. 把 key 和 CPU block ID 累积到 `(tier, request)` 的 pending promotion；
4. 对 scheduler 仍返回 `None`，所以本 pass 不分配 GPU blocks；
5. `manager.on_schedule_end()` 才创建一个 batched tier job 并调用
   `secondary.submit_load()`。

源码：

- 预留 slot：
  [tiering/manager.py#L271-L318](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L271-L318)；
- pass 末提交 promotion：
  [tiering/manager.py#L320-L342](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L320-L342)。

若 CPU primary 没有足够空闲或可逐出 slot，promotion 不会启动，manager 返回
`False`。上层按连续 prefix 的 miss 边界截断 external hit，后面的 token
重算；这不是“整个 request 失败”。

### L2：promotion 被观察后，再创建 CPU→GPU load

secondary I/O 完成不等于 CPU block 已对 scheduler ready。必须等 manager 的
completion poll：

1. `secondary.get_finished_jobs()` 返回 promotion completion；
2. `primary.complete_write(..., success=True)` 把 `ref_cnt=-1` 改成 `0`；
3. 同次或后续 lookup 才返回 `True`；
4. scheduler 得到 external hit，分配目标 GPU blocks；
5. `update_state_after_alloc()` 调 `manager.prepare_load()`，将 CPU block pin；
6. 创建 connector CPU→GPU load job；
7. request 进入 `WAITING_FOR_REMOTE_KVS`，自己的本轮 compute token 为 0。

源码：

- promotion completion 收账：
  [tiering/manager.py#L192-L225](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L192-L225)；
- external load 分配但不计算：
  [core/sched/scheduler.py#L782-L906](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/core/sched/scheduler.py#L782-L906)；
- 转入等待状态：
  [core/sched/scheduler.py#L917-L938](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/core/sched/scheduler.py#L917-L938)。

### L3：worker pre-forward 提交 DMA；完成后下一 pass 才恢复 request

CPU→GPU load 在 worker **pre-forward** 提交，可以与同一 batch 中其他 request
的 model forward 重叠；被 load 的 request 自己不能在这时计算。

worker post-forward 非阻塞 poll：

- DMA 足够快时，可以在提交它的同一个 worker invocation 中被观察；
- 未完成时，在后续 invocation 继续 poll；
- 所有 workers 都报告 job 完成后，scheduler 才调用
  `manager.complete_load()` 解 CPU pin，并记录 `finished_recving`；
- 再下一次 schedule 才把 request 从 `WAITING_FOR_REMOTE_KVS` 恢复为 WAITING，
  cache 已加载的 GPU blocks，然后真正继续计算。

源码：

- pre-forward 启动 load：
  [worker/gpu/kv_connector.py#L61-L75](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/worker/gpu/kv_connector.py#L61-L75)；
- worker completion：
  [offloading/worker.py#L298-L340](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/worker.py#L298-L340)；
- scheduler 解 pin 与接收完成：
  [offloading/scheduler.py#L1106-L1147](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py#L1106-L1147)；
- request 恢复：
  [core/sched/scheduler.py#L2355-L2404](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/core/sched/scheduler.py#L2355-L2404)。

因此，原生 FS 上一个“新 request 的 secondary hit”最短也包含四个逻辑
scheduler 阶段：

```text
L0  existence lookup
 → L1  secondary→CPU promotion submit
 → L2  CPU→GPU load submit
 → L3  completion 已被观察后的 request resume
```

实际可能因后台 lookup、I/O、completion poll、GPU DMA 或 async batch queue
增加更多 pass。不能把固定的“N+2”写进实验假设。

## 5. 完整 write path：GPU → CPU primary → secondary

### S0：forward 前先为“本轮将生成的完整块”预约 CPU slot

`_build_store_jobs()` 在 worker 执行本轮 forward **之前**运行。它用：

```text
request.num_computed_tokens + 本轮 num_scheduled_tokens
```

推算 forward 后会成为完整、可 offload 的 block，只处理 store cursor 之后的
新完整块。随后：

1. `manager.prepare_store()` 在 CPU primary 预约 `ref_cnt=-1` 的 destination；
2. 创建 GPU→CPU connector store job；
3. 把 job 放进本轮 connector metadata。

源码：
[offloading/scheduler.py#L831-L920](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py#L831-L920)。

若 primary 容量不足，`prepare_store()` 返回 `None`，本次不推进对应 store
cursor，后续 pass 可以重试。CPU primary 的 capacity/pin 状态因此会直接改变
实际 write pressure，不能只按“每一步都会 store”估算 secondary 写流量。

### S1：本轮只生成 KV；store 到下一个 worker invocation 才启动

当前 invocation 的 post-forward 不提交这个新 store，只把它放进
`_unsubmitted_store_jobs`。下一个 worker invocation 的 pre-forward 才调用
GPU→CPU `transfer_async()`。

这样 source GPU KV 已经由前一轮 forward 产生，而且 store 的发起被移到下一
轮开头，避免阻塞前一轮 sampling。

源码：

- 当前 invocation 延迟：
  [offloading_connector.py#L111-L120](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading_connector.py#L111-L120)；
- 下一 invocation 提交：
  [offloading/worker.py#L280-L296](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/worker.py#L280-L296)。

### S2：GPU→CPU 完成后，立即 cascade 到所有 secondaries

worker 报告 GPU→CPU completion 后，scheduler：

1. 调 `manager.complete_store()`，CPU block 从 `-1` 变成 ready 的 `0`；
2. 对每个 secondary 分别调用一次 `primary.prepare_read()`，各增加一次 ref；
3. 为每个 secondary 创建 tier cascade job；
4. 立即调用 `secondary.submit_store()`。

这段发生在 `scheduler.update_from_output()` 中，不等待下一次
`build_connector_meta()`。对一个 block 有 N 个 secondaries，就会有 N 个
独立 source pin，必须分别 completion 才全部解除。

源码：
[tiering/manager.py#L481-L537](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L481-L537)。

后续 manager poll 到每个 cascade completion 时调用一次
`primary.complete_read()`。最后一个 ref 回到 `0` 后，CPU block 才重新可逐出。

因此对同一个 block：

```text
GPU→CPU 与 CPU→secondary 是串行依赖的两段
```

但不同 block/job 的两段可以重叠，secondary cascade 也可以和后续 model
compute 重叠。

### S3：request finished 不等待 secondary cascade

`request_finished()` 会处理仍在飞的 GPU↔CPU connector job 和 GPU block
复用 fence，但不会等待已经提交的 CPU→secondary cascade。tier job 可以在
request 生命周期结束后继续，由 manager 后续 poll 收账。

源码：

- connector request finish：
  [offloading/scheduler.py#L1162-L1201](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py#L1162-L1201)；
- tiering finish 与 pending work：
  [tiering/manager.py#L567-L590](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L567-L590)。

源码还有一个明确的范围边界：request 结束时不足一个完整 offloaded block 的
尾部不会补 store。

## 6. CPU primary 的真实状态机

`ref_cnt` 同时表达 ready 与 pin：

| `ref_cnt` | 含义 | lookup | 可逐出 |
|---:|---|---|---|
| `-1` | GPU→CPU store 或 secondary→CPU promotion 正在写入 | `None` | 否 |
| `0` | 数据 ready，没有 transfer 使用它 | `True` | 是 |
| `>0` | 正被 CPU→GPU load 或 CPU→secondary cascade 当作 source | `True` | 否 |
| absent | primary 没有该 key | `False` | 不适用 |

状态迁移：

```text
write reserve:     absent → -1
write success:          -1 → 0
write failure:          -1 → absent，并释放 slot
prepare source read:     0 → 1，或 n → n+1
complete source read:    n → n-1
最后一个 source 完成:     1 → 0
```

源码：

- 状态定义：
  [cpu/policies/base.py#L10-L33](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/cpu/policies/base.py#L10-L33)；
- lookup/pin/unpin：
  [cpu/manager.py#L115-L165](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/cpu/manager.py#L115-L165)；
- reservation/capacity/eviction：
  [cpu/manager.py#L168-L236](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/cpu/manager.py#L168-L236)；
- complete/rollback：
  [cpu/manager.py#L239-L269](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/cpu/manager.py#L239-L269)。

由此得到一个实验上非常重要的结论：secondary 越慢，cascade source pin
维持越久；可逐出的 CPU blocks 越少；新的 store 或 promotion 越容易被
primary capacity 拒绝。即使 candidate 只替换 secondary engine，上层出现的
load/store 比例也会被 candidate 自身完成速度反馈改变。这是原生语义的一部分，
不是 scheduler 协同优化。

## 7. 到底谁优先：load、store 还是 promotion？

不存在一个全局的“load 永远优先”或“store 永远优先”。只能分层说明。

### 7.1 scheduler/CPU slot admission

waiting request 的 lookup 和 promotion reservation 发生在
`build_connector_meta()` 之前；而 `build_connector_meta()` 先调用
`manager.on_schedule_end()` flush promotions，再调用 `_build_store_jobs()`
预约本 pass 的新 GPU stores。

所以在**同一个 pass 新出现的 CPU slot 竞争**中，promotion reservation 先于
新 store reservation。但它不会抢走已有 pin，也不会绕过 primary capacity；
这不是全局 load priority。

### 7.2 worker GPU↔CPU submission

pre-forward 的调用顺序是：

```text
submit 上一个 invocation 延迟的 GPU→CPU stores
→ wait jobs_to_flush
→ submit 当前 CPU→GPU loads
```

不过两个方向由独立 transfer handler/stream 执行，所以函数调用先后不等于
设备层面严格的 store-before-load。不同 request 的两个方向可以并行；同一个
request 则不允许 load 与未完成 connector job 并存。

源码：
[cpu/gpu_worker.py#L169-L176](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/cpu/gpu_worker.py#L169-L176)、
[cpu/gpu_worker.py#L477-L536](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/cpu/gpu_worker.py#L477-L536)。

### 7.3 原生 FS secondary

原生 FS 默认有 16 个 read-priority 线程和 16 个 write-priority 线程。两组
线程分别优先 drain load queue 或 store queue，但自己的首选 queue 为空时会
去另一个 queue 工作。因此它是“双队列、双线程组、带 fallback”，不是一个
严格的全局读优先队列。

源码：

- 默认线程数：
  [fs/manager.py#L83-L131](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/fs/manager.py#L83-L131)；
- 取任务规则：
  [fs/thread_pool.py#L50-L56](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/fs/thread_pool.py#L50-L56)、
  [fs/thread_pool.py#L153-L179](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/fs/thread_pool.py#L153-L179)。

## 8. `jobs_to_flush` 是复用 fence，不是取消

GPU block 可能在 pending store 读完前被 preempt、释放或重新分配。scheduler
会把相关 connector store job 放进 `jobs_to_flush`。

worker 在 pre-forward：

1. 先提交尚未提交的 deferred stores；
2. 对 fence 中的 jobs 阻塞 `wait()`；
3. 确认 store 不再读取这些 GPU blocks 后才允许当前 forward 复用。

job 没有被丢弃，稍后仍正常通过 completion 路径收账。

源码：

- 建立 fence：
  [offloading/scheduler.py#L1020-L1041](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py#L1020-L1041)；
- worker wait：
  [offloading/worker.py#L271-L289](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/worker.py#L271-L289)。

这意味着 store 通常可以后台化，但在 GPU block reuse/preemption 压力下会转化成
前台 stall。端到端实验必须单独记录 flush count 和 flush wait time，否则会把
这类 stall 错归因给 model compute 或 secondary load。

## 9. completion poll gate 的额外 observation lag

`TieringOffloadingManager` 用 `_processed_jobs_this_step` 试图做到“每 step 最多
poll 一次 secondary completion”。但 connector 的调用顺序产生了一个边界：

```text
本 pass 早期 lookup
    → 第一次 poll，flag=True

build_connector_meta()
    → manager.on_schedule_end()
         因 flag=True，本次 poll no-op
         随后 flag=False
         flush promotions
    → _build_store_jobs()
         若调用 manager.prepare_store()
             → 又 poll 一次，flag=True

下一 pass 早期 lookup
    → 若 flag 仍为 True，本次不 poll
```

如果 promotion 恰好在 `_build_store_jobs()` 的晚 poll **之后**完成，下一 pass
早期 lookup 可能因为 flag 仍为 `True` 而看不到 completion。它可能要到下一次
晚 poll，甚至再下一个 pass 才被承认。

这个现象只在晚段确实走到 `prepare_store()` 等触发条件时出现，不应写成“每次
promotion 固定多等一轮”。但在持续 store pressure 下，它是一个真实的、
candidate-independent 的 scheduler observation lag。

源码：

- poll gate：
  [tiering/manager.py#L179-L190](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L179-L190)；
- `on_schedule_end()` reset：
  [tiering/manager.py#L567-L578](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L567-L578)；
- `prepare_store()` 再触发 poll：
  [tiering/manager.py#L397-L427](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L397-L427)；
- connector 的相对顺序：
  [offloading/scheduler.py#L1014-L1046](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py#L1014-L1046)。

## 10. 对本项目实验设计的直接约束

以下结论已经可以由源码得到，不需要先跑性能实验：

1. **端到端 read path 比纯 secondary read 多两类固定开销。**  
   原生 FS 有异步 existence lookup；所有 backend 都有 CPU→GPU connector
   transfer 和 completion observation。微基准更快不能直接等价为 TTFT 更快。

2. **secondary load 只服务连续 external prefix。**  
   prefix 长度决定有多少 prefill compute 可以被替代；primary capacity 拒绝或
   中间 block miss 会截断收益。需要把“查到多少”“promotion 多少”“最终
   CPU→GPU 多少”分别计数。

3. **store pressure 会反向影响 load admission。**  
   cascade pin 占住 primary block；promotion 又需要 primary destination。
   读写混合压力必须测，不能分别测完读、写就推导混合场景。

4. **更快 backend 的收益可能被 pass 粒度量化。**  
   如果 FS 和 candidate 都在同一个 scheduler observation 窗口内完成，端到端
   可能看不出差异；只有跨过 poll/pass 边界时才突然少一次等待。需要同时报告
   raw tier latency 和 `completion → observed` latency。

5. **candidate 的 lookup/index 方法本身是 treatment 的一部分。**  
   如果 uring-slab 用内存索引，可能消除原生 FS 的 L0 existence pass。这是合法
   的最终系统优势，但分析时必须拆成 `lookup/index` 与 `data I/O`，否则不能说
   清 io_uring/slab/提交模型各自贡献。

6. **store 通常后台化，但不是永不影响前台。**  
   primary slot shortage、source pin 和 `jobs_to_flush` 都能把 store 压力转成
   admission failure 或前台 stall。

因此端到端 instrumentation 至少需要以下时间点和计数：

| 类别 | 最低必要打点 |
|---|---|
| lookup | lookup start/result；`True/False/None`；FS lookup flush/complete |
| promotion | primary reserve；secondary submit/start/complete；manager observed |
| GPU load | GPU slot alloc；CPU pin；worker submit/complete；scheduler observed；request resume |
| GPU store | CPU reserve；job create；下一 invocation submit；complete/observed |
| cascade | secondary submit/start/complete；manager observed；CPU unpin |
| capacity | primary free/evictable/pinned/in-flight；promotion/store admission failure |
| fence | `jobs_to_flush` count、wait start/end、涉及的 GPU blocks |

推荐从这些时间点导出，而不是只记录一个 backend latency：

```text
secondary_service_time = tier_complete - tier_start
secondary_queue_time   = tier_start - tier_submit
observation_lag        = manager_observed - tier_complete
cpu_to_gpu_time        = gpu_load_complete - gpu_load_submit
resume_lag             = request_resume - gpu_load_scheduler_observed
end_to_end_hit_wait    = request_resume - first_external_lookup
```

这些打点用于解释原生 FS 与 uring-slab 的最终 A/B，不要求修改调度决策，也不
构成 scheduler 协同优化。

## 11. 源码结论与待实验问题的边界

### 已由源码确定

- secondary 只能经 CPU primary 接入，没有 GPU↔secondary 直连；
- fresh FS hit 有独立的异步 existence lookup 阶段；
- promotion 必须先占 CPU destination，完成被 poll 后才可 CPU→GPU；
- CPU→GPU load 在 pre-forward 提交；
- 本轮新 GPU→CPU store 延迟到下一个 worker invocation；
- GPU→CPU 完成后立即发起 CPU→secondary cascade；
- CPU primary 的 pin/capacity 会耦合 read 与 write；
- request finished 不等待 secondary cascade；
- FS 线程池不是严格全局 load priority；
- completion observation 可能晚于真实 I/O completion。

### 仍必须通过实验回答

- 在目标 GPU、模型和并发深度下，一个 scheduler pass 的实际墙钟长度；
- backend latency 要下降多少才能跨过一个 observation window；
- promotion 与其他 request forward 的实际重叠比例；
- primary 容量、cascade pin 和 flush fence 各自造成多少 stall；
- prefix 长度 × load pressure × store pressure 的哪一片区域能把 backend
  优势转化为 TTFT 或吞吐收益；
- 原生 FS 与 uring-slab 的 lookup/index 差异占最终收益多少。

这正是本项目应遵守的因果链：

```text
secondary engine 改变
→ queue/service/completion 时序改变
→ primary pin/capacity 与 scheduler observation 改变
→ request resume 时间或可服务吞吐改变
```

如果前两段明显更快、最后一段没有收益，结论应是“backend 改善被上层时序或
其他瓶颈吸收”，而不是继续调整 trace 直到得到正结果。

## 12. 对旧架构理解的修正

旧文档
`projects/project6_vllm024_second_tier_study/docs/VLLM_024_ARCHITECTURE.md`
不应继续作为时序依据，至少需要修正：

1. CPU→GPU load 在 pre-forward 启动，不是在 sampling 后；
2. `jobs_to_flush` 是先提交 deferred store 再阻塞等待，不是丢弃工单；
3. `prepare_load()` 和 load job 创建在 `update_state_after_alloc()`，不在
   `build_connector_meta()`；
4. fresh FS hit 还有一个被旧主序列遗漏的 existence lookup 阶段；
5. GPU load completion 可在同一 worker invocation 被观察，也可更晚，不能
   固定为 N+2；
6. “所有异步都发生在圈与圈之间”不成立；准确说法是 scheduler 只在规定
   poll 点承认异步完成；
7. secondary completion poll 不是简单的“固定每 pass 一次”，存在第 9 节的
   reset/late-poll 边界。

后续文档或面试陈述应以本文固定 commit 的时序为准。
