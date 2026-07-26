# vLLM 0.24.0：store、lookup、promotion、load 的触发条件

状态：**SOURCE FACTS — REVIEWED**  
上游版本：vLLM `v0.24.0`  
锁定 commit：`ee0da84ab9e04ac7610e28580af62c365e898389`

本文只回答调用条件和时序，不把源码推导写成性能结论。这里的 `store` 和
`load` 各有两段，必须严格区分：

```text
store: GPU → CPU primary → secondary
load:  secondary → CPU primary → GPU
```

## 1. 一页结论

| 动作 | 直接触发条件 | 不会触发/会被阻断的条件 |
|---|---|---|
| GPU→CPU store | 本 scheduler step 为请求生成了新的、完整且可 offload 的 block；CPU primary 能为新 key 分配或逐出 slot | block 尚未完整；已处理过；被 prompt/max-token/SWA 规则排除；CPU primary 没有可用或可逐出 slot |
| CPU→secondary store | 对原生 FS：GPU→CPU store 成功完成，scheduler 收到 worker completion 并调用 `complete_store()`；对 `REQUEST_LEVEL` tier：`prepare_store()` 还会直接提交已在 CPU primary ready 的 prefix-hit block | 对原生 FS：GPU→CPU 未完成或失败，或没有新 key；对 request-level 补充路径：primary block 尚未 ready |
| lookup | scheduler 为请求查询 external KV hit，且该请求没有未完成的 GPU↔CPU transfer，并且没有设置 `skip_reading_prefix_cache` | 请求已有 load/store transfer；跳过 prefix cache；剩余范围不足一个完整 offloaded block |
| promotion | CPU primary miss，同时某个 secondary `lookup()` 明确返回 `True` | primary hit/in-flight；所有 secondary miss；secondary lookup 尚未完成；CPU primary 无法接收 promotion |
| secondary→CPU load | promotion 已占好 CPU slot；到本 scheduler step 的 `on_schedule_end()` 时，将同一 `(tier, request)` 的 promotion 合成一个 `submit_load()` | lookup 还未得到 `True`；promotion 因 CPU primary 满而被拒绝 |
| CPU→GPU load | secondary promotion 已完成并被 scheduler poll 到；再次 lookup 时 key 已在 CPU primary ready；scheduler 为 external-hit tokens 分配了 GPU blocks | promotion 仍在途/失败；请求未获得 external hit；GPU load job 尚未完成 |

最重要的纠正是：

> second-tier store 不是等 CPU primary eviction 才触发。vLLM 会主动把成功写入
> CPU primary 的新 block 级联到所有 secondary tiers。CPU primary eviction
> 只影响它能否接收新的 GPU store 或 secondary promotion。

## 2. Store 的完整触发链

### 2.1 scheduler 何时尝试 GPU→CPU store

`OffloadingConnectorScheduler.build_connector_meta()` 每个 scheduler step 都会
调用 `_build_store_jobs()`。但这不等于“每一步把全部 KV 重写一次”。

对当前 step 中确实被调度的每个 request，代码先计算：

```text
num_tokens_after_batch
→ 不超过 request 当前 token 数
→ 不超过 per-request max_offload_tokens（如果设置）
→ offload_prompt_only=true 时不超过 prompt 长度
→ 向下取整到完整 offloaded block
```

然后只处理 `[next_stored_block_idx, num_complete_blocks)` 中的新 block。
以下 block 会被过滤：

- 尚未形成完整 offloaded block；
- EAGLE 的易变尾 block；
- sliding-window/SSM 的空 placeholder；
- 已被重新分配而置零的 stale block；
- 按 full-attention 对齐规则永远无法形成 load hit 的 SWA block。

因此默认 `offload_prompt_only=true` 时：

- chunked prefill 可在多个 step 中产生 store；
- prompt 每形成新的完整 block 才有新 store；
- decode block 不会进入 store；
- 如果关闭该配置，decode 也只会在形成完整 block 时产生 store，而不是每个
  token 都产生一次。

源码：

- [`scheduler.py#L831-L1012`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py#L831-L1012)
- [`base.py#L466-L472`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/base.py#L466-L472)

### 2.2 CPU primary 什么时候接受或拒绝 store

`manager.prepare_store()` 先去掉已经存在于 CPU primary 的 key。对新 key：

1. 优先使用 free slot；
2. 不足时从 eviction policy 中逐出 `ref_cnt==0` 且不在本批 protected key
   集合中的 block；
3. 可逐出 block 不足或 policy 找不到合法 victim 时返回 `None`。

返回 `None` 时，scheduler 不推进 `next_stored_block_idx`，因此保留了重试
位置；如果该请求之后再次被调度，会从该位置重试。但若请求不再被调度，源码
并不保证还会补交这次 store。

`TieringOffloadingSpec` 不支持 `store_threshold>=2`，所以本项目的 tiering
路径不存在“观察多次后才 store”的 admission threshold。

源码：

- [`cpu/manager.py#L168-L236`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/cpu/manager.py#L168-L236)
- [`tiering/spec.py#L143-L155`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/spec.py#L143-L155)

### 2.3 GPU→CPU 并不是在创建 metadata 时立即开始

scheduler 本 step 生成 GPU→CPU store job 后，worker 的
`prepare_store_kv()` 先把它放进 `_unsubmitted_store_jobs`。实际
`transfer_async()` 被推迟到下一个 engine step 的开头，以免 store 干扰本
step 的 sampling 相关传输。

源码：

- [`worker.py#L280-L296`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/worker.py#L280-L296)

### 2.4 什么时候真正触发 secondary `submit_store()`

GPU→CPU worker job 完成后，worker completion 回到 scheduler；
`update_connector_output()` 调用：

```text
TieringOffloadingManager.complete_store(keys)
```

它先把 CPU block 标成 ready，然后对**所有** secondary tiers：

1. `primary.prepare_read(keys)`，给 source slot 增加 ref count；
2. 创建 `is_promotion=false` 的 `JobMetadata`；
3. 立即调用 `tier.submit_store(job_metadata)`。

secondary store 完成被 `get_finished_jobs()` poll 到后，
`primary.complete_read()` 才释放这次 pin。secondary store 成功与否不决定
CPU primary 中的数据是否 ready；它只决定 secondary 是否获得副本。

原生 FS 返回默认 `BLOCK_LEVEL` policy，因此通常只级联本 request 新计算的
block。只有返回 `REQUEST_LEVEL` 的自定义 tier，才会要求把已经存在于 CPU
primary 的 prefix-hit block 也额外 `submit_store()` 到该 tier。

源码：

- [`scheduler.py#L1106-L1147`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py#L1106-L1147)
- [`tiering/manager.py#L397-L534`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L397-L534)
- [`base.py#L57-L68`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/base.py#L57-L68)

## 3. Lookup 的触发条件和三态传播

### 3.1 scheduler 什么时候进入 lookup

`get_num_new_matched_tokens()` 用于查询 GPU prefix hit 之外还能从 external
KV 加载多少 token。只有以下条件同时成立才会实际调用 `_lookup()`：

- 请求已经有 `RequestOffloadState`；
- 当前没有该请求未完成的 GPU↔CPU load/store job；
- `request.skip_reading_prefix_cache` 为 false。

lookup 只处理 `num_locally_computed_tokens` 之后、至少一个完整 offloaded
block 的范围。全注意力组要求连续 prefix hit；任一确定 miss 会截断后续
prefix。多 KV group 时，每个 group 都必须支持同一个最终 hit 边界。

源码：

- [`scheduler.py#L388-L622`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py#L388-L622)
- [`scheduler.py#L636-L681`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py#L636-L681)

### 3.2 Tiering manager 的查找顺序

对每个 key：

1. 先 poll 本 step 尚未处理的 secondary completion；
2. 查 CPU primary：
   - `True`：直接命中，不查 secondary；
   - `None`：primary slot 正在写入，整个 key defer；
   - `False`：继续查 secondary；
3. 按配置顺序查所有 secondary tiers：
   - 第一个 `True` 触发 promotion；
   - 某个 tier 返回 `None` 时仍会继续查后续 tier；
   - 没有 `True`、但至少一个 `None`，总结果为 `None`；
   - 全部明确 `False`，总结果为 `False`。

因此 second tier 真正参与请求 load 的必要条件是：

```text
GPU prefix miss
∧ CPU primary miss
∧ secondary 中存在相同 key
∧ request 未禁用 prefix-cache read
∧ 命中范围至少一个完整 offloaded block
```

源码：

- [`tiering/manager.py#L179-L269`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L179-L269)

### 3.3 原生 FS 为什么首次 lookup 通常返回 `None`

FS 不在 scheduler 线程同步调用 `os.path.exists()`。首次看到 `(key,
request)` 时：

1. `lookup()` 建立 result 为 `None` 的状态并把 key 加入本 step batch；
2. 本 step 结束时 `FS.on_schedule_end()` 才把整批 existence query 交给后台
   lookup thread；
3. 后续 scheduler step 的 lookup 非阻塞地 drain 已完成结果；
4. 若后台查询已经完成，则返回 `True` 或 `False`；否则仍返回 `None`，可以跨越
   多个 step。

`on_request_finished()` 会清除没有其他活跃 request 引用的 lookup state。
所以新 revisit request 通常仍需重新做一次异步 file-existence lookup；它不
是永久的进程级命中索引。

源码：

- [`fs/manager.py#L133-L191`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/fs/manager.py#L133-L191)
- [`async_lookup.py#L125-L185`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/async_lookup.py#L125-L185)

## 4. Promotion 的触发与拒绝

promotion 不是 scheduler 单独调用的 API，而是
`TieringOffloadingManager.lookup()` 的副作用：

```text
primary.lookup(key) == False
∧ tier.lookup(key) == True
→ _initiate_promotion()
```

`_initiate_promotion()` 立即调用 CPU primary 的 `prepare_write([key])`：

- 有 free/evictable slot：占用 slot，并将 key 标为 write-in-flight；同 step
  再次 lookup 会在 primary 得到 `None`，不会重复 promotion；
- 没有可用 slot：本次 promotion 返回 `False`；源码没有为它建立独立的
  promotion backpressure/wait queue。

这意味着“FS 中有 key”并不等于“会发生 FS load”。CPU primary slot/pin
压力可以在 I/O 提交前就拒绝 promotion。该位置会向本次 lookup 传播确定 miss；
若没有其他 pending `None` 使整个 request defer，它会截断可用的连续 prefix，
后续部分走 recompute。

成功占 slot 后，key 和目标 primary block id 先按 `(tier, request)` 聚合；
当前 `lookup()` 仍返回 `None`，请求等待后续 scheduler step。

源码：

- [`tiering/manager.py#L271-L318`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L271-L318)
- [`cpu/manager.py#L168-L236`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/cpu/manager.py#L168-L236)

## 5. Load 的两段触发链

### 5.1 secondary→CPU load

`lookup()` 只预留 slot，不直接逐 block 调用 `submit_load()`。在本 step
`build_connector_meta()` 调用 `manager.on_schedule_end()` 时：

1. 先 poll secondary completion；
2. 将本 step 同一 `(tier, request)` 的 promotion blocks 合成一个
   `JobMetadata(is_promotion=true)`；
3. 调用一次 `tier.submit_load()`；
4. 原生 FS 把每个 block 展开成 `load_block` task，放入 read queue。

secondary completion 只有在之后某个 scheduler step 被
`get_finished_jobs()` poll 到时才生效。成功 completion 调用
`primary.complete_write(..., success=true)`，CPU slot 才从 in-flight 变成
ready；失败则回滚该 primary slot。

源码：

- [`tiering/manager.py#L320-L342`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L320-L342)
- [`tiering/manager.py#L192-L225`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L192-L225)
- [`tiering/manager.py#L566-L578`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L566-L578)
- [`fs/manager.py#L158-L179`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/fs/manager.py#L158-L179)

### 5.2 CPU→GPU load

promotion ready 后，scheduler 再次 lookup 才得到 `True` 并计算出
`num_external_tokens>0`。GPU blocks 分配完成后，
`update_state_after_alloc()`：

1. 收集 external-hit 对应的 key 和 GPU destination block ids；
2. `manager.prepare_load(keys)`，pin CPU primary source slots；
3. 创建 CPU→GPU load job；
4. worker 在当前 step 的 `start_kv_transfers()` 中启动 load；
5. worker completion 使请求进入 `finished_recving`，scheduler
   `manager.complete_load()` 后解除 CPU pin。

所以端到端 secondary hit 至少包含两个独立传输：

```text
secondary → CPU primary
CPU primary → GPU
```

uring-slab 只直接优化第一段以及 secondary lookup/管理开销；第二段保持为
vLLM 原生 CPU↔GPU 路径。

源码：

- [`scheduler.py#L683-L778`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py#L683-L778)
- [`tiering/manager.py#L345-L394`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L345-L394)
- [`worker.py#L280-L332`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/worker.py#L280-L332)

## 6. 一次原生 FS revisit 的最短状态机

对一个不在 GPU/CPU、但在 FS 中的 key，新 request 通常经历：

```text
step N:
  GPU miss
  → CPU primary miss
  → FS lookup 首次返回 None
  → on_schedule_end 提交批量 file-existence lookup

step N+1:
  FS lookup 返回 True
  → primary 预留 promotion slot
  → lookup 返回 None
  → on_schedule_end 调用 FS submit_load

step N+2 或更晚:
  poll 到 FS load completion
  → primary slot ready
  → lookup 返回 True
  → scheduler 分配 GPU blocks
  → 启动 CPU→GPU load

CPU→GPU completion:
  request 才能使用这段 KV 继续执行
```

实际可能多于上述 step 数，因为 existence lookup、FS load 或 completion
都可能尚未赶上当前可见的 scheduler polling 时机。

## 7. 对实验设计的直接约束

以下结论可由代码确定，不需要先跑性能实验：

1. **要测 second-tier load，必须同时制造 GPU miss 和 CPU primary miss。**
   只做 revisit 而没有逐出 CPU primary，测到的是 CPU hit。
   `TieringOffloadingManager.reset_cache()` 会先 drain secondary I/O、清空
   primary，同时有意保留 FS 等 persistent secondary，可用来做确定性的功能
   资格测试；但它是 sleep/weight-update/resume 控制路径，不应伪装成普通在线
   trace 的自然 eviction。
2. **prime 阶段必须等待 secondary store completion。** GPU→CPU 完成不代表
   FS 文件已经写完。
3. **reuse distance 只是触发器。** 它的任务是稳定逐出 GPU/CPU，同时保留
   secondary；不必成为主要性能解释轴。
4. **prefix 必须至少覆盖一个完整 offloaded block。** 不足一个 block 时不会
   进入 external load。
5. **默认 decode 不产生 store 压力。** `offload_prompt_only=true` 时，背景
   store 应通过新的完整 prompt block 产生；若改为 false，必须对两个 backend
   使用相同配置。
6. **load pressure 不能只用请求并发定义。** 只有 secondary hit 且 promotion
   被 CPU primary 接受的请求才形成实际 secondary read offered load。
7. **CPU primary 太小会让实验从 I/O 压力测试变成 promotion-rejection 测试。**
   必须分别记录 secondary hit、promotion accepted/rejected 和实际 submitted
   load bytes。
8. **backend I/O 完成不等于 scheduler 立刻受益。** completion 只在 manager
   的显式 polling 点被承认，并受 `_processed_jobs_this_step` gate 限制；它
   不会在 I/O 完成瞬间被 scheduler 立即承认。因此 I/O 节省可能被
   scheduler-step observation lag 量化或掩盖。
9. **原生 FS 的异步 existence lookup 是 baseline 的真实成本。** candidate
   用内存索引同步返回 `True/False` 仍保持三态接口语义，但这部分收益必须与
   纯数据 read 收益分开观测。

`reset_cache()` 的源码语义见
[`tiering/manager.py#L603-L629`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L603-L629)。

## 8. 仍需实验回答的问题

源码不能回答：

- 不同 workload 中上述路径出现的频率；
- primary slot/pin 压力达到何值后 promotion rejection 显著增加；
- FS lookup、secondary load、CPU→GPU 和 scheduler observation 各占 TTFT
  多少；
- store 与 load 跨 request 并发时的实际设备争用；
- uring-slab 的更快 completion 能否跨过 scheduler step 边界；
- 哪些 prefix/load/store 压力组合能形成端到端收益。

这些才是后续 instrumentation、微基准和端到端实验需要回答的内容。
