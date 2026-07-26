# 从 vLLM 0.24.0 源码事实推出的性能假设 v1

状态：**HYPOTHESIS REGISTRY — BEFORE CANDIDATE PERFORMANCE DATA**  
日期：2026-07-26（Asia/Shanghai）  
上游：vLLM `v0.24.0`，commit
`ee0da84ab9e04ac7610e28580af62c365e898389`

本文不使用现有噪声 campaign 或旧 prototype 的性能结果来生成假设。它只把
vLLM 0.24.0 的源码事实、计划中的 candidate 机制和可证伪预测连接起来。

## 1. 推导规则

每条内容分成三种证据等级：

- **源码事实**：vLLM 0.24.0 已经确定的控制流、数据流或实现；
- **candidate 设计前提**：预分配 slab、内存索引、C++ 数据面和可批量提交；
- **性能假设**：尚未成立，必须由后续微基准或端到端实验反驳或支持。

源码可以证明“某种开销存在”，不能证明“它足够大”“candidate 一定更快”
或“收益一定能转化为 TTFT”。特别禁止以下偷换：

- 原生 FS 与 candidate 都使用 O_DIRECT，不能写成 page-cache 对比；
- 线程池已经异步，不能写成“同步 FS 对异步 io_uring”；
- secondary lookup hit 不等于 promotion accepted，更不等于成功送到 GPU；
- backend completion 不等于 scheduler 当时就能观察到 completion；
- 更高请求并发不会自动创造 secondary hit，只会改变排队和 primary 压力；
- 更长 prefix 不保证相对收益单调增加。

关键源码入口：

- [`vllm/v1/kv_offload/tiering/manager.py`](https://docs.vllm.ai/en/v0.24.0/api/vllm/v1/kv_offload/tiering/manager/)：
  级联、promotion、pin 和完成收割；
- [`vllm/v1/kv_offload/tiering/fs/manager.py`](https://docs.vllm.ai/en/v0.24.0/api/vllm/v1/kv_offload/tiering/fs/manager/)：
  FS tier 与异步 lookup；
- [`vllm/v1/kv_offload/tiering/fs/io.py`](https://docs.vllm.ai/en/v0.24.0/api/vllm/v1/kv_offload/tiering/fs/io/)：
  每 block 的 O_DIRECT 文件 I/O；
- [`vllm/v1/kv_offload/tiering/fs/thread_pool.py`](https://docs.vllm.ai/en/v0.24.0/api/vllm/v1/kv_offload/tiering/fs/thread_pool/)：
  双队列线程池；
- [`vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py`](https://github.com/vllm-project/vllm/blob/v0.24.0/vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py)：
  scheduler step、store 构建与 connector metadata。

本文只登记由上述固定版本源码事实推出的待验证命题，不把当前仓库之外的研究
笔记作为正式依赖。

## 2. 总性能模型

设一次成功 secondary promotion 涉及：

- `N`：KV block 数；
- `B`：总逻辑字节数；
- `T_lookup`：secondary existence/index lookup；
- `T_queue`：secondary 内部排队；
- `T_data`：CPU primary 与 secondary 之间的数据传输；
- `T_observe`：engine 完成到 scheduler 收割 completion 的等待；
- `T_H2D`：CPU primary 到 GPU 的传输；
- `T_other`：其余调度、排队和模型执行时间。

则 revisit TTFT 中与这条路径相关的时间近似为：

```text
T_revisit
≈ T_lookup
+ T_queue
+ T_data
+ T_observe
+ T_H2D
+ T_other
```

原生 FS 的 CPU↔secondary 部分可以写成一个待拟合而非预设成立的模型：

```text
T_FS
≈ N × T_file_metadata
+ T_python_runtime
+ T_device(B, read/write mix, outstanding)
+ T_observe
```

计划中的 uring-slab 则是：

```text
T_uring_slab
≈ N × T_slot/index
+ T_cpp_control
+ T_ring_submit/reap
+ T_device(B, read/write mix, outstanding)
+ T_observe
```

两式共享设备、O_DIRECT、逻辑字节和上层 polling 语义。需要解释的是前三项，
不能把共同部分重复算作 candidate 贡献。

## 3. 资格假设

### H0：backend 收益存在一个严格的“可达性乘数”

优先级：**P0，所有性能 claim 的前置条件**

源码事实：

1. lookup 先查 CPU primary，只有 primary miss 才查 secondary；
2. secondary 命中后必须先在 CPU primary 预留 promotion slot；
3. primary 无可用或可逐出 slot 时，promotion 返回 `False`，上层按 miss 重算；
4. secondary 不能直接访问 GPU，成功 promotion 后仍需 CPU→GPU；
5. secondary I/O 失败同样不能产生可用命中。

性能假设：

```text
P(useful secondary load)
= P(revisit candidate)
× P(GPU miss)
× P(CPU miss | GPU miss)
× P(secondary hit)
× P(promotion accepted)
× P(secondary load success)
× P(CPU→GPU success)
```

因此 candidate 对 serving 的影响近似还要乘以上述漏斗。若没有真实
secondary bytes，或 promotion acceptance 很低，替换 backend 不应产生稳定
serving 收益。

可观察预测：

- T0 无复用和 T1 CPU hit 场景中，FS/uring 的请求指标应基本相同；
- 只有 T2 secondary hit 且 promotion accepted 的请求才进入主 TTFT 比较；
- 同一 workload 中，backend effect 应随 useful-secondary-loaded bytes 占比
  增大，而不是只随总请求并发增大；
- 若没有 candidate I/O 却观察到显著 TTFT 差异，优先判为 workload、缓存状态
  或接入语义不一致。

反证/失败边界：

- second-tier hit 很高但 promotion acceptance 很低时，只能得出 primary gate
  结论，不能评价 backend；
- no-secondary-I/O 场景出现稳定收益，会反证 A/B 的语义等价性。

### H0b：被成功加载不等于比 recompute 值得

优先级：**P0，no-second-tier 资格检查；不是主基线**

源码事实：

- secondary lookup/promotion 失败时，上层把对应 block 当作 external miss，
  请求通过模型执行重新生成 KV；
- secondary 命中时，请求必须等待 lookup、promotion、completion observation
  和 CPU→GPU 路径；
- 两条路径在相同 prefix 上构成“恢复 KV”与“重新计算 KV”的反事实。

性能假设：

对选入正式 workload 的 prefix 长度 `L` 和并发条件 `c`，至少应满足：

```text
T_lookup
+ T_queue
+ T_data
+ T_observe
+ T_H2D
< T_recompute(L, c)
```

否则即使 uring-slab 比 FS 快，也只能证明 secondary backend 实现改善，不能
证明使用 second tier 对 serving 有意义。

可观察预测：

- prefix 较短或 mixed-I/O 过载时，恢复路径可能不如 recompute；
- prefix 增长会同时增加 recovery bytes 和 recompute 工作，盈亏边界必须实测，
  不能只根据 SSD 带宽推断；
- `no-second-tier` 只需作为 eligibility gate 确认主 workload 位于值得恢复的
  一侧，不升级为与 FS、uring 并列的主基线。

反证/失败边界：

- 两个 second-tier backend 都稳定慢于 recompute，却仍宣称 serving 正收益；
- 用不同 prefix、并发或缓存状态比较 recovery 与 recompute。

## 4. 数据面机制假设

### H1：file-per-block 元数据路径是 slab 的第一项可分离收益

优先级：**P0，核心机制**

源码事实：

- FS 为每个 offloaded block 生成独立路径和 `.bin` 文件；
- store 先 `exists`，再创建目录，O_DIRECT 打开临时文件，写入、close、
  `os.replace`；
- load 对每个 block 执行 open、O_DIRECT `readv`、close；
- 一个 vLLM job 会在 FS 线程池中拆成 `N` 个 per-block task。

candidate 设计前提：

- uring-slab 使用预分配单文件和固定 slot offset；
- key→slot 由内存索引解析，不为每个 block 创建、rename 或打开独立文件。

性能假设：

- slab 首先降低的是每 block 固定开销、元数据波动和 CPU/GiB，而不是凭空提高
  设备峰值带宽；
- store 的差异应大于 load，因为 FS store 额外包含目录、临时文件和 rename；
- 当 block 较小、blocks/job 较多或 metadata churn 较强时，相对收益最大；
- 当数据传输时间远大于元数据时间时，相对收益缩小，绝对收益仍可能随 `N`
  增长。

可观察预测：

- `files → py-pool-slab` 后，metadata syscalls/block、文件/inode 数、
  CPU core-seconds/GiB 和 store p95 同时下降；
- 若设备带宽未饱和但 FS CPU 或 metadata 指标先出现拐点，支持该假设；
- 纯 load 的改善小于 store 并不反证整体方法，因为两条路径的源码成本不同。

反证/失败边界：

- 改成 slab 后 syscall/CPU/job-sojourn 均无相邻工作点改善；
- 收益只来自 FS 使用不同 O_DIRECT、不同预热或不同逻辑字节；
- 唯一改善来自候选容量更大或命中率不同，而不是相同 job stream。

### H1b：内存索引可能消除 FS existence lookup 的一个固定 step 成本

优先级：**P0，完整 tier 机制；不属于纯数据 I/O**

源码事实：

- FS 的 `lookup()` 委托给 `FsAsyncLookupManager`；
- 未解析的文件存在性检查可先返回 `None`，在后台执行 path lookup；
- `on_schedule_end()` 才推进本 step 的异步 lookup，后续 step 才能得到
  `True/False`；
- 即使 existence 为 `True`，之后还要预留 primary slot 并提交 promotion。

candidate 设计前提：

- uring-slab 的 resident/in-flight key 状态保存在进程内索引；
- 对稳定 resident 或 absent key，lookup 可以同步返回 `True/False`；真正
  in-flight 的 key 仍必须返回 `None`。

性能假设：

- candidate 可能在没有提高设备读带宽的情况下，消除 FS 第一次 existence
  lookup 的一次或多次 scheduler-step quantization；
- 该收益更接近每次 request/prefix lookup 的固定延迟，而不是每 GiB 收益；
- 短 prefix 或设备 I/O 很快时，它在 TTFT 中的相对占比可能最大；
- 这是完整 backend 的合法差异，但必须与 H1 的文件数据路径分开报告。

可观察预测：

- FS 的首次 unresolved lookup `None` 比例和 lookup-resolution steps 高于
  candidate；
- candidate 的 `promotion_submit_ns - lookup_start_ns` 更短，而两侧实际
  data service 可以相同；
- 做 lookup-parity 诊断（让 candidate 也延迟一 step）后，这部分 TTFT 差异
  应消失，但数据面差异仍保留。

反证/失败边界：

- candidate 因索引恢复、in-flight 或 Python 控制面同样经常返回 `None`；
- scheduler 的其他固定阶段使两侧仍在同一 step 才能 submit promotion；
- 把 lookup 收益错误归因给 io_uring。

### H2：Python per-block task 和完成记账会形成第二项软件开销

优先级：**P1，核心消融**

源码事实：

- `submit_store/load` 为每个 block 构造 Python callable；
- task 被逐个压入 `deque`；
- 每个 block 完成后进入 `JobState.task_done()` 的 Python/lock 记账；
- 默认创建 16 个 read-priority 和 16 个 write-priority Python threads；
- I/O syscall 本身可释放 GIL，但路径构造、队列、closure、状态和 completion
  处理仍经过 Python 对象与锁。

candidate 设计前提：

- `cpp-pool-slab` 与 `uring-slab` 把 per-block 数据面及 completion 聚合放到
  C++，Python 只处理 job 级控制。

性能假设：

- `py-pool-slab → cpp-pool-slab` 的主要收益应体现为 user/system CPU、
  context switch、submit 开销和高并发 tail，而不一定提高单个大 I/O 的带宽；
- 收益随 blocks/job 和 outstanding block 数增加；
- QD 很低、job 很大或设备服务时间占绝对多数时，这一差异可能不可见。

可观察预测：

- 相同 slab、相同 blocking I/O 下，C++ 版本 CPU/GiB 降低；
- Python 版本先出现 scheduler/runtime CPU 饱和时，吞吐拐点早于 C++；
- 若吞吐相同但 CPU/GiB 稳定降低，仍支持“资源效率”子命题，但不能单独写成
  TTFT 收益。

反证/失败边界：

- `py-pool-slab` 与 `cpp-pool-slab` 的 CPU、submit、tail 全部不可区分；
- 差异来自线程数、buffer copy 或 I/O 语义不一致。

### H3：io_uring 的贡献取决于批量提交是否真正改变提交模型

优先级：**P1，核心消融；允许负结果**

源码事实：

- 原生 FS 使用阻塞 syscall + 多线程并行；
- 线程池已经非阻塞地接收上层 job，因此“上层 submit 不阻塞”不是 io_uring
  独有能力。

candidate 设计前提：

- `cpp-pool-slab` 与 `uring-slab` 保持相同 slab/index，只替换 blocking pool
  与 ring submit/reap；
- uring 路径需要实际批量 SQE/CQE，而不是每个 I/O 都单独 enter/reap。

性能假设：

- io_uring 的增量收益主要出现在中高 outstanding、可批量 submit/reap 的区域；
- 低 QD 下 ring 管理成本可能使 p50 相同甚至更差；
- 若文件系统或虚拟块设备把 O_DIRECT I/O punt 到 io-wq，io_uring 可能只改变
  用户态线程形状，而不提高设备能力；
- 因此核心结论属于 `uring-slab` 整体，不能预设 io_uring 单独贡献为正。

可观察预测：

- `cpp-pool-slab → uring-slab` 时，enter 次数/I/O、context switch 和高 QD
  CPU/GiB 下降；
- 改善应在至少两个相邻 QD 工作点存在，而不是单个极端点；
- 若设备 await、带宽和 CPU 都不变，则 io_uring 增量贡献为零；
- 若低 QD tail 恶化，应把它写入适用边界，而不是隐藏。

反证/失败边界：

- C++ blocking pool 在全部目标工作点等于或优于 io_uring；
- candidate 没有实际 batching，或依赖不同线程数、不同 extent、不同 direct
  I/O 才获利。

## 5. 读写竞争假设

### H4：FS 是读写分区加互助，不是严格的全局 load priority

优先级：**P0，决定 store-pressure 主轴**

源码事实：

- FS 有 load queue 和 store queue；
- read-priority threads 先取 load，空时帮助 store；
- write-priority threads 先取 store，空时帮助 load；
- 默认两组各 16 线程；
- 两个队列持续非空时，近似 16 路 load + 16 路 store；单向负载时另一组可以
  借给当前方向，最多接近 32 路。

性能假设：

- isolated load 下，FS 可借用 write threads，candidate 不一定有巨大优势；
- store 压力上升且两个队列同时非空后，FS load 的可用优先 worker 数从接近
  32 收缩到约 16，并与写 I/O 继续竞争设备；
- 带 read-reserved credits 或有界 store admission 的 candidate 可以把
  load-tail 拐点推向更高 store 压力；
- 没有读保护的单 ring FIFO candidate 反而可能比 FS 更差。

可观察预测：

- 固定 load offered rate，增加 store offered rate 后，FS load queue wait/p95
  出现拐点；
- candidate 的收益应主要来自 queue wait 而不是把同一设备 service time
  错写为更快；
- pure read、balanced、write-heavy 三点应形成连续趋势；
- candidate 保护 load 时，store backlog 或 store latency 可能上升，必须同时
  报告，不能只展示读侧。

反证/失败边界：

- load p95 与 store 压力无关；
- candidate 仅通过少做 store、丢 job 或减少逻辑字节保护读；
- 优势只来自强制 FS 使用不合理线程参数。

### H5：唯一新 prefix 会产生“没有复用收益的写压力”

优先级：**P1，解释 store-pressure 来源**

源码事实：

- `_build_store_jobs()` 每 step 检查新形成的 eligible 完整 block；
- GPU→CPU store 完成后，block 会 cascade 到所有 secondary tiers；
- tiering 不支持 `store_threshold >= 2`；
- 默认 `offload_prompt_only=true`，可用 `max_offload_tokens` 限制范围；
- duplicate key 可以避免再次写入，但第一次出现的唯一 prefix 仍会 store。

性能假设：

- store pressure 主要由 unique eligible KV byte rate 决定，不应直接用请求并发
  代替；
- 一次性 prefix 比例增加时，系统可能花 secondary 带宽写入永不 revisit 的
  KV，并干扰有价值的 promotion；
- uring-slab 可以降低吸收这些写入的成本、推迟混合负载拐点，但不能让没有
  reuse 的写入本身产生 serving 价值。

可观察预测：

- 固定 revisit/read workload，仅提高 unique eligible bytes/s，load queue、
  primary cascade pin 和设备写流量同步上升；
- candidate 如果只改善 store throughput，但 load/TTFT 不变，应保留为数据面
  结论；
- no-revisit workload 中不应宣称 second tier 的用户收益。

反证/失败边界：

- 实际 store offered bytes 没随 unique eligible bytes 增长，说明上层过滤、
  duplicate 或 primary admission 在主导；
- candidate 通过改变 `offload_prompt_only` 或 `max_offload_tokens` 获利，
  属于上层策略变化，不属于主实验。

## 6. 上层转化假设

### H6：更快 completion 会通过缩短 primary pin 生命周期产生间接收益

优先级：**P0，最重要的系统转化机制之一**

源码事实：

- cascade 前，primary `prepare_read()` 增加 source block ref count；secondary
  store completion 后才 `complete_read()` 解 pin；
- promotion 在 `submit_load()` 前就用 `prepare_write()` 预留 primary slot，
  completion 后才 `complete_write()` 把它变成 ready；
- CPU→GPU load 又会 pin primary block，完成后才释放；
- pinned、writing 和 protected slot 都不能被 eviction。

性能假设：

- 更快的 secondary store/load 不只缩短 job sojourn，还会降低
  `primary slot-seconds pinned/reserved`；
- 在 primary 压力区，它可能提高 promotion acceptance、store absorption 和
  可逐出 slot 数，从而产生大于直接 I/O 差值的间接系统收益；
- primary 很宽裕时，该间接路径应消失，backend 比较退化为直接延迟/CPU 对比。

可观察预测：

- candidate 先降低 cascade pin duration、promotion reservation duration 和
  pinned-slot 水位，然后才可能改善 promotion acceptance；
- 随 CPU primary 缩小或 promotion wave 增大，FS 更早出现
  `promotion_reject_primary_full`；
- 若 backend 更快但 pin duration 未变，说明 completion observation 或其他
  transfer 阶段才是 pin 的主导部分。

反证/失败边界：

- promotion acceptance 与 pin 水位在相同 offered load 下完全不变；
- candidate 通过扩大 CPU primary、改变 eviction policy 或修改 scheduler
  获利；
- 只报告 secondary hit，不报告 promotion accepted。

### H7：scheduler step polling 会把连续的 I/O 改善量化成阶梯收益

优先级：**P0，最重要的负结果解释之一**

源码事实：

- secondary completion 只通过 `get_finished_jobs()` 被上层承认；
- manager 每个 scheduler step 至多实际 poll 一次；
- promotion 在 step 末批量 submit；
- engine 已完成但尚未被 poll 时，primary slot 仍不会变成 ready；
- request 需要后续 step 才能继续 CPU→GPU load。

性能假设：

- internal engine latency 的连续改善不会线性转化为 observed sojourn 或 TTFT；
- FS 和 candidate 若都在同一个下一次 poll 前完成，TTFT 可能完全相同；
- candidate 只有跨过一个或多个 step 边界时，才可能获得近似整数 step 的收益；
- scheduler 越忙、step 越长或 poll 越稀疏，engine 改善越容易被
  completion→observed lag 吞掉。

可观察预测：

- `engine_done_ns → scheduler_observed_ns` 之间存在非零且随 step 变化的 gap；
- TTFT 差值可能呈阶梯、双峰或高方差，而不是平滑跟随 device latency；
- microbenchmark 显著更快但两侧 observed completion 落在同一 step 时，
  end-to-end 差异接近零；
- 若 candidate 刚好提前一个 poll window，TTFT 收益可能大于纯 I/O 差值。

反证/失败边界：

- scheduler 实际在 step 内持续收割 completion；
- internal completion、observed completion 和 TTFT 始终近似线性同步；
- 没有记录 engine-done 时间却把差异归因给 polling。

### H8：CPU staging 和 polling 给端到端收益设置 Amdahl 上限

优先级：**P0，决定是否值得扩大端到端实验**

源码事实：

- secondary 不能直接访问 GPU；
- 路径固定为 `secondary → CPU primary → GPU`；
- uring-slab 只能替换 CPU↔secondary；
- lookup、scheduler observation 和 CPU→GPU 都仍在关键路径上。

性能假设：

若 FS 路径中可由 candidate 改变的直接关键路径占比为 `S_backend`，忽略 H6
的间接 pin 效应时，即使 backend 无限快：

```text
maximum direct TTFT reduction <= S_backend
```

因此数据面 2× 不代表 TTFT 2×。当 `T_H2D + T_observe + T_other` 主导时，
更快 backend 可能只转化为 CPU 效率，不转化为用户延迟。

可观察预测：

- microbenchmark effect 大、secondary critical-path share 小的点，TTFT effect
  也小；
- 长 prefix 下，如果 CPU→GPU 随字节线性增长并成为主导，backend 相对收益
  会平台化；
- serving 收益显著超过 direct share 时，应查 H6 的 pin/acceptance 间接路径，
  而不是直接宣称违反 Amdahl 上限。

反证/失败边界：

- 没有逐段时间线就声称 backend 是 TTFT 瓶颈；
- candidate 修改 CPU→GPU、scheduler 或 cache policy 后仍把全部收益归给
  secondary engine。

## 7. 三个主轴的预期形状

### H9：prefix 长度同时放大数据量和 block 数，relative effect 不保证单调

优先级：**P0，prefix 主轴**

源码事实：

- cache/promotion 以完整 offloaded block 为单位；
- FS 每 block 一个 task/文件操作；
- 同一 request 的 promotion 在 step 末合并为一个 job，但 FS 内部仍拆成
  per-block task；
- CPU→GPU 字节数也随成功 promotion 的 block 数增长。

性能假设：

prefix 轴可能出现三个区域：

1. **短 prefix**：固定 lookup/step 成本占比大，H1b 的同步内存索引可能主导；
2. **中等 prefix**：per-block metadata、Python task 和 queue 开销被放大，
   H1/H2/H3 最可能形成可见相对收益；
3. **长 prefix**：设备、primary reservation 或 CPU→GPU 饱和，绝对节省继续
   增长，但相对收益平台化或缩小。

因此项目不预注册“prefix 越长，uring 相对收益必然越大”，只预期存在可解释
的区域和拐点。

可观察预测：

- absolute saved milliseconds 大体随 loaded blocks 增长；
- relative effect 允许非单调，但相邻点的变化必须能由 lookup、metadata、
  queue、device、poll 或 H2D share 解释；
- 只在单个 prefix 点出现的正结果不能定义适用边界。

### H10：load pressure 存在“FS 软件拐点”和“共同硬件拐点”

优先级：**P0，load-pressure 主轴**

源码事实：

- promotion 按 `(tier, request)` 在 step 末形成 job；
- FS 再把每个 job 拆成 block task；
- promotion 在提交 I/O 前已经占用 primary slots；
- completion 只在后续 poll 被收割。

性能假设：

- 随 simultaneous revisit/promotion wave 增加，FS 可能先在 Python task、
  metadata 或 32-thread 模型处出现 queue knee；
- candidate 若降低这些软件开销，会把 knee 推后；
- 再继续提高 load，双方最终会遇到共同设备带宽、primary slot 或 CPU→GPU
  瓶颈，此后相对优势缩小或失去 serving 转化。

可观察预测：

- FS queue wait 和 primary reserved slots 先于设备完全饱和增长，支持软件拐点；
- candidate knee 右移但共同 device/H2D 指标最终收敛；
- 过载区必须报告 offered load、completed load 和 backlog，不能只报完成吞吐。

### H11：store pressure 决定 load-tail 保护是否有意义

优先级：**P0，store-pressure 主轴**

该假设由 H4 和 H5 合成：

- 低 store 压力：双方主要比较 isolated load，FS 可以借用 write threads；
- 中 store 压力：队列同时非空，FS 读写分区、metadata 写事务和 device mix
  开始影响 load tail，candidate 的 slab/store 路径和 read credits 最有价值；
- 极高 store 压力：共同设备写带宽或 primary cascade pin 主导，纯 backend
  优势可能不足以保护 TTFT。

可观察预测：

- candidate 的有效区应是连续的 store-pressure 区间，而不是挑出的单点；
- load p95 改善必须与相同 store completed bytes、无 job drop 同时成立；
- 若 candidate 只把写积压无限后推，应在 store absorption 守护指标上 FAIL。

reuse distance 在上述三个主轴中只负责稳定触发 secondary hit，不承担独立性能
解释。它一旦让 GPU/CPU miss 和 secondary hit 闭合，就应保持固定。

## 8. 公平性和非核心假设

### H12：FS 无限持久空间与 candidate 有限 slab 不能混进主性能 claim

源码事实：

- FS 没有容量参数或 secondary LRU；
- reset 不删除 persistent secondary files；
- candidate 正式 contract 要求有限 capacity、LRU、pin 和 eviction。

推论：

- 主性能比较必须让 working set 完全落在 candidate capacity 内，并为每个 arm
  使用干净、等价的数据生命周期；
- FS 文件数长期增长可能改变 metadata 行为，但这是独立的长期 churn 假设；
- candidate eviction 导致 hit rate 下降时，结果不能解释为纯 engine 性能；
- 有限容量是一项功能边界，不允许通过命中率不等价替 candidate 制造优势。

## 9. 假设优先级与消融映射

| 假设 | 要解释的机制 | 最小对比 |
|---|---|---|
| H0 | backend 是否实际可达 | manager funnel，不比较 I/O 快慢 |
| H0b | recovery 是否值得 | no-second-tier eligibility gate |
| H1 | file-per-block/metadata | files vs py-pool-slab |
| H1b | async path lookup vs memory index | lookup-only / lookup-parity diagnostic |
| H2 | Python runtime | py-pool-slab vs cpp-pool-slab |
| H3 | io_uring submit model | cpp-pool-slab vs uring-slab |
| H4/H5 | mixed read/write 与写放大 | 固定 load，扫描 store pressure |
| H6 | pin 生命周期与 promotion acceptance | 相同 engine load，不同 primary pressure |
| H7 | scheduler poll 量化 | engine-done vs observed-done vs step |
| H8 | CPU staging/Amdahl ceiling | request critical-path decomposition |
| H9 | prefix 边界 | 固定 hit 条件，扫描 loaded blocks |
| H10 | load-pressure knee | promotion wave/outstanding load |
| H11 | store-pressure 有效区 | 相同 completed store bytes 的混合负载 |
| H12 | capacity 公平性 | working set fits；另做 bounded-capacity |

核心命题 B 不要求每条假设都为正。最低完整因果链是：

1. H1/H2/H3 的消融能够解释 uring-slab 数据面差异；
2. H0/H0b 证明主 workload 的 backend 可达且 recovery 值得；
3. H4/H9/H10/H11 给出 prefix、load、store 三轴边界；
4. H6/H7/H8 能解释数据面收益为何转化或没有转化。

如果数据面明显更快但 serving 无收益，H7/H8 成为主解释；只有
H1/H2/H3/H4 的消融明确暴露 candidate engine 内部可以修复的问题，才允许
迭代实现。不能修改 scheduler，也不能继续调整 trace 直到得到正结果。

## 10. 现在已经能由源码确定、无需实验争论的结论

1. 研究对象不是 page cache，而是 file layout、Python runtime、submission
   model、lookup/index 和 mixed-I/O policy；
2. FS 已经异步，candidate 不能靠“异步”两个字构成贡献；
3. load 有线程分区优先，但没有全局、可抢占的绝对优先级；
4. store 会给 primary block 加 pin，promotion 会提前占 primary slot；
5. primary-full 可以把真实 secondary hit 降级为 recompute；
6. completion 只在 scheduler step poll 时被承认；
7. CPU staging 和 CPU→GPU 是 uring-slab 无法消除的结构性下界；
8. prefix、load pressure、store pressure 足以作为适用边界主轴；
9. reuse distance 只负责制造深度命中；
10. io_uring 的独立贡献必须由 `cpp-pool-slab → uring-slab` 消融决定，不能从
    vLLM 源码或 API 名字直接推出。
