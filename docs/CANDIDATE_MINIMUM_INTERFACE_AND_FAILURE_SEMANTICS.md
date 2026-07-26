# Candidate 最小接口与失败语义

状态：**SOURCE-DERIVED DESIGN INPUT — NO CANDIDATE PERFORMANCE DATA**

上游：vLLM `v0.24.0`
commit：`ee0da84ab9e04ac7610e28580af62c365e898389`

本文只回答两个问题：

1. 为了在本项目的真实 vLLM 路径上替换 FS tier，candidate adapter 最少必须
   实现什么；
2. 构造、提交、I/O、回滚、drain 和 shutdown 失败时，状态和 completion
   必须如何收敛。

本文不把生产级恢复、多进程共享、跨重启索引恢复或通用存储能力加入项目范围。

## 1. 上游源码依据

锁定 commit 的关键来源：

- [`SecondaryTierManager`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/base.py#L42-L230)：
  接口、非阻塞要求、primary memoryview、`JobMetadata`、completion 和 drain；
- [`TieringOffloadingManager`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py#L111-L636)：
  job 登记、promotion、cascade、completion 消费、reset 和 shutdown；
- [`FileSystemTierManager`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/fs/manager.py#L62-L202)：
  v0.24.0 baseline 对接口的实际实现；
- [`OffloadingConnectorScheduler`](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py)：
  request 生命周期、lookup retry 和 scheduler step 调用位置。

以下内容必须区分：

- **上游事实**：由上述源码决定，candidate 无权改变；
- **项目决定**：上游允许多种合法实现，本项目为可验证性选择其中一种；
- **范围外能力**：实验路径不会依赖，不要求实现。

## 2. 最小代码边界

```text
TieringOffloadingManager                 Python adapter
------------------------                 --------------
JobMetadata ---------------------------> admission / key-slot policy
primary_kv_view[block_id] <------------> offset validation
JobResult <----------------------------- completion aggregation
                                                |
                                                v
                                         C++ data engine
                                         submit / poll / drain
```

Python adapter 拥有：

- key → secondary slot 索引；
- slot 容量、LRU、eviction 和 secondary load pin；
- duplicate/in-flight 处理；
- job → keys/slots 的账本；
- C++ completion 到 `JobResult` 的聚合；
- 失败回滚和可观测计数。

C++ engine 只拥有：

- 已校验的 primary offset、secondary offset、长度、方向和 job id；
- 异步提交、短 I/O 检查、completion 和 drain；
- 不决定 key、LRU、victim、promotion 或 vLLM policy。

## 3. 接口不是“11 个方法都同等必需”

### 3.0 构造与 factory 接入也是最小接口

candidate 必须能由 v0.24.0 `SecondaryTierFactory` 从 `secondary_tiers` 配置创建。
构造器至少接受：

```text
offloading_spec
primary_kv_view
tier_type
path
disk_bytes_to_use
queue_depth
```

其中前三项由 factory/上游注入，后三项是本项目最小 candidate 配置。
`primary_kv_view` 的第一维 stride 是一个 offloaded block 的字节跨度；candidate
必须在构造期完成 block bytes、地址、slab slot 和 O_DIRECT 对齐校验。

独立包通过自定义 `spec_module_path` 在模块加载时注册 `uring_slab` tier type；
正式路径不修改 vLLM source tree。无法注册、未知参数或构造失败都属于启动失败，
不得推迟到首个 request 才暴露。

### 3.1 上游抽象类强制覆盖的 6 个方法

| 方法 | 最小行为 | scheduler 线程要求 |
|---|---|---|
| `lookup(key, ctx)` | 返回严格三态 | O(1)、非阻塞 |
| `submit_store(job)` | 接收 primary→secondary job | 只做有界元数据工作和异步提交 |
| `submit_load(job)` | 接收 secondary→primary job | 只做有界元数据工作和异步提交 |
| `get_finished_jobs()` | 交付此后未交付过的 terminal 结果 | 非阻塞 poll |
| `on_new_request(ctx)` | 返回 request offload policy | 轻量；本项目固定 BLOCK_LEVEL |
| `drain_jobs()` | 等所有已提交 job terminal | 唯一允许主动阻塞的传输接口 |

### 3.2 基类有默认实现，但本项目 candidate 应覆盖的 3 个方法

| 方法 | 上游默认 | 本项目为什么覆盖 |
|---|---|---|
| `touch(keys, ctx)` | no-op | uring-slab 声称使用 LRU，必须更新 resident key |
| `has_pending_work()` | `False` | 提供可审计 pending 真值，覆盖本地 completion/rollback |
| `shutdown()` | no-op | 必须 drain/关闭 ring、fd 和线程，保证 memoryview 安全 |

`TieringOffloadingManager` 自己的 `_transfer_jobs` 已足以让所有已登记 job
继续触发 scheduler step。因此，在 v0.24.0 当前实现中，
`has_pending_work()` 不是 accepted job 得以继续被 poll 的唯一条件；它仍应
由 candidate 正确实现，用于候选内部状态审计及避免未来出现未被上层 job
表覆盖的 deferred work。

### 3.3 本项目可安全继承 no-op 的 2 个方法

- `on_request_finished(ctx)`：candidate 不维护 request 级状态时可 no-op；
  已提交 job 可能仍在途，不得在这里取消或释放其 job 资源；
- `on_schedule_end()`：candidate 不在 Python 层延迟提交时可 no-op；C++ 内部
  batching 不要求 Python 在 step 末 flush。

如果以后引入 request-level policy 或 Python deferred submission，必须提升
contract revision，不能悄悄改变这两个方法。

## 4. 输入与上层资源所有权

`JobMetadata` 包含：

```text
job_id
keys
block_ids
is_promotion
req_context
```

正式路径的前置条件：

- `job_id` 由一个 `TieringOffloadingManager` 单调生成，在其生命周期内唯一；
- `len(keys) == len(block_ids) >= 1`，相同位置一一对应；
- store 的 `is_promotion=False`，load 的 `is_promotion=True`；
- `block_id` 指向一个完整、有效、对齐的 primary slot；
- 一个 candidate 实例只被 scheduler 线程调用 Python 状态方法。

资源所有权不能混淆：

| 资源 | 谁分配/释放 | candidate 的义务 |
|---|---|---|
| store source primary pin | 上游 `prepare_read/complete_read` | 在 completion 前不再访问后才允许上层释放 |
| load target primary reservation | 上游 `prepare_write/complete_write` | failure 时不宣称 target 数据有效 |
| secondary slot reservation | candidate | store terminal 后 commit 或 rollback |
| secondary load pin | candidate | load terminal 后引用计数释放 |

candidate **不能直接释放 primary pin/reservation**。它只能交付 exactly-once
`JobResult`；上游在消费结果时释放这些资源。

## 5. 最重要的调用事实：上游先登记，再调用 submit

v0.24.0 的三个调用路径都先执行：

```text
_transfer_jobs[job_id] = job_metadata
```

然后才调用：

```text
tier.submit_store(job_metadata)
或
tier.submit_load(job_metadata)
```

并且没有异常回滚。

因此，不能采用下面这种语义：

```text
submit_* 同步抛出
→ 认为 job 没有 accepted
→ 不产生 completion
```

这会让异常进入 scheduler，同时在上层遗留 `_transfer_jobs`、store source pin
或 load target reservation。

本项目必须使用以下规则：

### 构造期失败可以抛异常

在 tier 被加入 manager 以前，下列错误必须 fail-fast：

- slab 路径不可创建或打开；
- capacity 小于一个 slot；
- block size、primary 地址或 slab offset 不满足 O_DIRECT 对齐；
- `io_uring` 初始化失败；
- 未知配置、非法 queue depth；
- C++ ABI/版本不匹配。

禁止静默退回 buffered I/O。

### 运行期可预期失败不得从 `submit_*` 抛出

下列情况必须正常返回，并最终产生一次 `JobResult(success=False)`：

- capacity 不足且没有可逐出的未 pin slot；
- duplicate in-flight policy 选择拒绝；
- submit queue/ring full；
- key 在 lookup 与 deferred `submit_load` 之间被逐出；
- short I/O、`EIO` 或单 block I/O failure；
- engine 接受前的可恢复 submission failure。

只有调用方违反 frozen contract 或 candidate 内部账本损坏时才允许 fail-fast，
并将整次 run 标为无效，而不是伪装成普通 job failure。

## 6. Lookup 三态与进展条件

| candidate key 状态 | `lookup` | 含义 |
|---|---:|---|
| `ABSENT` | `False` | 上层可以 recompute |
| `STORE_RESERVED/WRITING` | `None` | 已有操作会自行收敛，稍后重试 |
| `RESIDENT` | `True` | 此刻可接受 load |
| `QUARANTINED` | `False` | 数据可疑，禁止再次 promotion |

规则：

- 只有所有 block bytes 已成功写入、job 已 commit 后才能从 `None` 变 `True`；
- `None` 只能表示一个无需调用方重新提交、能够自行走向 terminal 的暂态；
- 永久错误不能通过永远返回 `None` 表达，否则请求会无限 defer；
- `lookup=True` 不预留 primary slot，也不产生 secondary pin；上游随后才决定
  promotion 是否能被 primary 接受；
- `submit_load` 必须重新验证所有 key，因为 lookup 和 step 末的 batched
  submit 之间存在时间窗口。

## 7. Key、slot 与 job 状态机

### 7.1 Store

```text
ABSENT
  → slot admission / victim removal
  → STORE_RESERVED
  → WRITING
  ├─ all blocks success → RESIDENT → success completion
  └─ any failure       → ABSENT   → failure completion
```

项目决定：

- resident duplicate：过滤物理 I/O；job 仍正常 success completion；
- in-flight duplicate：最小实现可以整 job立即 failure completion；
  若选择 coalesce，follower 必须等待 leader terminal，不能提前报 success；
- mixed resident/new job：resident key 保持不变；只有全部 new key 成功后才同时
  进入 resident；
- admission 为了复用 slot 可以先逐出旧 victim；后续 store failure 不要求
  恢复 victim，但必须保证没有部分新 key、重复 slot 或 reservation 泄漏。

### 7.2 Load

```text
all keys RESIDENT
  → secondary load-pin ref++
  → READING into upper-reserved primary targets
  ├─ all blocks success → ref-- → residency 保持 → success completion
  └─ any failure        → keys 对后续 lookup 不可用 → ref-- → failure completion
```

load 是 job 原子的：`JobResult` 只有一个 bool，而上游会用它一次性
`complete_write(all_keys, success)`。因此任一 block 失败，整个 job 必须失败。

失败后 primary target 可能已被部分覆盖；candidate 不清零也可以，但不得返回
success。上游收到 failure 后会移除这些未 ready 的 primary entries。

### 7.3 失败后的不可用化与 quarantine

load failure 不能简单“保持 secondary residency 可见”。否则下一轮 lookup
仍返回 `True`，可能不断创建 promotion、失败、再 promotion。

本项目采用：

1. 任一 key 在 `submit_load` 时已经不再 resident：整个 job failure；该 job
   中仍 resident 的其他 key 也全部安全退休为 `ABSENT`；
2. engine 接受 I/O 前的 queue/ring rejection：整个 job 的所有 key 移除
   lookup 可见性并安全退休为 `ABSENT`；这是保守丢缓存，不表示数据损坏；
3. short I/O、`EIO` 或其他数据路径失败：整个 job 的所有 key 立即移入
   `QUARANTINED`，即使底层能够指出只有其中一个 block 报错；
4. `lookup` 对两种状态都返回 `False`，让请求退化为 recompute；
5. 若同一 slot 还有其他 load ref，等 refcount 归零后再回收；
6. retired/quarantine slot 不得被新 store 覆盖，直到所有读者停止访问；
7. 为保持最小实现，该 key 在旧 generation 的 refcount 归零前拒绝新 store；
   归零并回收后，未来新 store 可以重新分配并恢复 resident；
8. 本项目不做介质修复，也不同时维护同一个 key 的多个 slot generation。

也可以设计“queue full 后保留 resident 并重试”，但必须额外证明重试有界且请求
不会在 `lookup=True → promotion failure` 中循环。为保持最小实现，本项目选择
失败后让 key 对后续 lookup 不可用。

## 8. Capacity 与 eviction

固定 slot 模型必须始终满足：

```text
free + store_reserved + resident_or_quarantined = capacity
load_pinned_slots <= resident_or_quarantined
一个物理 slot 同时最多属于一个 key generation
```

store admission：

1. 过滤 resident duplicate；
2. 计算全部新 key 所需 slot；
3. 只从 free 和未 pin resident LRU victim 中选择；
4. 如果不够，不能部分 admission，整个 job failure completion；
5. victim index 必须在 slot 被覆盖前移除；
6. store failure 释放所有新 reservation。

`touch()`：

- 只更新 `RESIDENT` key；
- absent、storing、quarantined 是 no-op；
- load-pinned resident 可以更新 recency，但不能成为 victim。

## 9. Completion 语义

accepted 的实际含义是：上游已经把 job 放入 `_transfer_jobs` 并调用
`submit_*`。对每个这样的 job：

```text
恰好一个 terminal JobResult
job_id 与提交一致
success 只在 job 全部 block 成功时为 True
结果在 get_finished_jobs() 交付前不能丢
交付后不能再次返回
```

结果顺序没有保证，不能依赖 FIFO。

candidate 在把结果返回给上游前，必须先完成自己的状态转移：

- store success：commit resident index；
- store failure：rollback reservation；
- load success：释放当前 job 的 secondary load refs；
- load failure：先使 key 对 lookup 不可用；数据路径失败时 quarantine；再释放
  当前 job refs；
- 清除 candidate job map。

未知、重复或方向不匹配的 C++ completion 是内部不变量破坏。此时继续返回普通
failure completion 可能释放错误的 primary slot；正确处理是 fail-fast，并把
整个实验 run 判为无效。

## 10. Pending、drain、reset 与 shutdown

candidate 的 pending snapshot 至少包含：

```text
accepted but not engine-submitted
engine queued
engine in-flight
rolling back
completed but not yet returned by get_finished_jobs()
```

任一非零时 `has_pending_work()` 返回 `True`。

`drain_jobs()`：

- 覆盖每个上游已经调用 `submit_*` 的 job，而不只是已进入 C++ engine 的 job；
- 推进尚未进入 engine 的本地 rejection、rollback 和 local failure completion
  到 terminal；
- 等待所有已提交 engine I/O terminal；
- 返回后任何 engine 线程都不得再读写 primary memoryview；
- completion 可以已经交付，也可以在下一次 `get_finished_jobs()` 中可得；
- 最小实现不取消 queued job，而是全部做完；
- 如果未来取消尚未开始的 job，每个取消 job 仍必须产生 failure completion。

上游 `reset_cache()` 的顺序是：

```text
tier.drain_jobs()
→ manager 统一 get_finished_jobs()
→ primary reset
```

所以 drain 不能只“停止 worker”而丢 completion。

上游普通 `shutdown()` 不先调用 manager-level drain。candidate 的
`shutdown()` 因此必须至少：

1. 阻止新提交；
2. drain 所有 engine I/O；
3. 关闭 ring/fd/线程；
4. 返回后不再访问 primary memoryview。

正式 correctness/benchmark 路径固定使用：

```text
drain_jobs()
→ 轮询并对账所有 completion
→ shutdown()
```

进程退出时未被上层消费的 completion 不属于性能结果；任何正式 run 在这之前
都必须已经对账为零。

## 11. 失败矩阵

| 失败点 | 对外结果 | candidate 状态 | 上层结果 |
|---|---|---|---|
| 构造/配置/O_DIRECT 对齐 | 同步抛出，tier 不创建 | 不得留下线程/fd/slab 临时状态 | 启动失败 |
| store 无容量 | 正常返回，local failure completion | 不得有新 key/reservation | 上层释放 source pin；以后可 recompute |
| store ring full | 正常返回，local failure completion | rollback 新 slot/index | 同上 |
| store short/error | failure completion | 所有新 key absent；释放 reservation | 同上 |
| load 任一 key 在 submit 时已失效 | local failure completion | 整个 job 的 key 全部退休为 absent | 上层移除 target reservation |
| load ring full | local failure completion | 整个 job 的 key 全部退休为 absent；secondary ref 不得泄漏 | 同上 |
| load short/error | failure completion | 整个 job 的 key 全部 quarantine；ref 归零后回收 | target 无效，后续 lookup miss/recompute |
| duplicate/unknown C++ completion | fail-fast，run invalid | 禁止猜测释放哪个资源 | 不产出性能结论 |
| drain 中 queued job | 等完成；或取消并 failure completion | 无 primary memory access 遗留 | reset 可安全继续 |
| shutdown 有在途 I/O | shutdown 内 drain | 关闭后不触碰 memoryview | 进程安全退出 |

## 12. 必需 correctness cases

candidate 进入任何性能比较前，至少通过：

1. 单 block、多 block store/load 和全区 checksum；
2. resident duplicate store；
3. in-flight duplicate store 的冻结策略；
4. mixed resident/new store；
5. capacity 满但有可逐出 victim；
6. capacity 满且所有 victim load-pinned；
7. overlapping load pin refcount；
8. lookup=True 后、submit_load 前 key 被逐出的防御性失败；
9. store queue full 和 load queue full；
10. store/load short I/O；
11. 每个 block 位置注入 I/O failure，验证 whole-job failure；
12. load failure 后 lookup=False，不能重复 promotion 坏数据；
13. unknown、duplicate completion 被 harness 拦截；
14. drain 后 engine 不再访问 primary memory；
15. completion 全部对账后 clean shutdown；
16. O_DIRECT 不满足时启动失败而非 buffered fallback。

这些测试证明接口与缓存正确性，不证明性能。

## 13. 明确不实现

- crash-consistent index 和进程崩溃恢复；
- 跨重启复用已有 slab 内容；
- 多进程并发写同一 slab；
- 跨节点共享；
- 在线扩容/缩容；
- production scrub、坏块修复和长期碎片整理；
- scheduler 协同优化；
- 通用 KV storage API。

正式实验每个 arm 使用新隔离 backing，candidate 生命周期内索引由成功 store
建立，因此以上能力不影响主命题。

## 14. 对现有 contract v2 草案的合并要求

在 candidate 实现开始前，现有
`projects/uring_slab_tier/docs/SECONDARY_TIER_CONTRACT_V2.md` 至少需要澄清：

1. 运行期 submit rejection 不能依赖“同步抛出即未 accepted”；上游在调用前
   已登记 job 且没有异常回滚；
2. primary source pin 和 target reservation 由上游根据 completion 释放，
   candidate 只管理 secondary reservation/pin；
3. load I/O failure 不能继续让可疑 key lookup hit；必须 quarantine/evict，
   否则可能重复 promotion；
4. 6 个 abstract method、3 个项目要求 override 和 2 个合法 no-op 应分开，
   避免把上游接口事实与本项目可观测性要求混为一谈。

这是源码审计修正，不是 candidate 性能结果驱动的规则变化。
