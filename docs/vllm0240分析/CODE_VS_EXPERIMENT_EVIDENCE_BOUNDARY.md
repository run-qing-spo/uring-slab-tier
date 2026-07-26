# 哪些问题由代码回答，哪些问题必须实验

状态：**研究证据边界，candidate 性能数据产生前冻结**

上游范围：vLLM `v0.24.0`，commit
`ee0da84ab9e04ac7610e28580af62c365e898389`

项目主命题：不改变 vLLM 上层语义，只替换 native FS second-tier engine

本文不设计新的 backend，也不修改冻结的 contract 或统计协议。它只规定：
针对每个研究问题，什么证据足够，什么结论不能从源码直接推出。

## 1. 四类证据

| 记号 | 证据类型 | 能回答什么 | 不能回答什么 |
|---|---|---|---|
| `S` | Source proof | 固定版本中的控制流、接口、默认值、状态转换和实现机制 | 该机制在某 workload 中出现多频繁、耗时多少 |
| `V` | Functional validation | 配置能否运行、路径是否真实触发、状态和数据是否闭合 | 性能优劣、瓶颈占比、适用边界 |
| `E` | Performance/causal experiment | 延迟、吞吐、CPU、排队、效果量、因果归因和端到端转化 | 超出实验机器、模型和 workload 的普遍性 |
| `X` | Out of scope | 本项目主动不回答 | 不得在作品或答辩中暗示已经证明 |

判定规则：

1. 问题含“代码会不会、何时调用、支持什么参数、失败后做什么”，优先用 `S`。
2. 问题含“在这套安装和配置中是否真的走到该路径、结果是否正确”，至少需要
   `V`。
3. 问题含“多快、多少、多久、是否成为瓶颈、是否值得、能否改善 TTFT/吞吐”，
   必须使用 `E`。
4. 源码可以给出性能假设和理论上限，但不能给出非零效果量。
5. `V` 通过只表示功能路径成立，不能升级为性能结论。

## 2. 已经可以由 vLLM 0.24.0 源码回答的问题

以下结论固定在指定 commit，不需要再跑性能实验。

| 问题 | 证据 | 源码足以支持的结论 |
|---|---|---|
| secondary 是否直连 GPU | `S` | 否。secondary 位于 scheduler 进程，只读写 CPU primary memoryview；数据路径固定经过 CPU staging |
| store 数据路径 | `S` | `GPU → CPU primary → secondary`；secondary store 只在 GPU→CPU store 完成后发起 |
| load 数据路径 | `S` | `secondary → CPU primary → GPU`；新 backend 不能消除 CPU→GPU 传输 |
| 是否“每一步都重写全部 KV” | `S` | 否。每 step 会检查 store，但只处理 eligible 的新完整 block；duplicate、prompt-only、token cap 和 primary admission 都会过滤 |
| 什么时候可能读 secondary | `S` | 依次需要 GPU miss、CPU miss、secondary hit、CPU promotion slot 可分配、secondary load 成功和 CPU→GPU load 成功 |
| FS hit 是否必然成为 useful hit | `S` | 否。primary promotion reservation 失败时，上层把它作为 external miss 并 recompute |
| `lookup(None)` 的含义 | `S` | 结果或传输仍在途，本 scheduler step 跳过并在后续 step 重试 |
| FS existence lookup 是否同步 | `S` | 否。FS 使用后台 lookup，首次查询可能返回 `None`，结果在后续 step 被承认 |
| completion 何时生效 | `S` | 完成只有被 `get_finished_jobs()` 和上层固定收账点观察后才改变 scheduler 可见状态 |
| 同 step 的 promotion/store 顺序 | `S` | promotion reservation/flush 早于本 step 新 GPU→CPU store 的构建 |
| 是否存在全局严格 load 优先 | `S` | 不存在。既有 cascade、promotion、GPU transfer 的 pin 和 FS 队列仍可互相影响 |
| primary 满时 store 如何处理 | `S` | 新 store admission 失败时 cursor 不前进，后续 step 可以重试 |
| primary 满时 promotion 如何处理 | `S` | promotion 返回失败，上层可能直接 recompute，而不是无限等待 |
| FS 的读写线程模型 | `S` | 默认 16 read-priority + 16 write-priority threads；各自优先本队列，空闲时可帮助另一方向 |
| FS 的文件布局 | `S` | 每个 block 一个 `.bin`，hash 前缀分层目录 |
| FS store 实现 | `S` | O_DIRECT 临时文件写入后 `os.replace()`；已有文件可跳过重写 |
| FS load 实现 | `S` | O_DIRECT 打开并 `readv()` 到 primary memoryview |
| FS 是否使用 page cache 作为主数据路径 | `S` | 不是；正式对比不能写成 buffered FS 对 O_DIRECT uring |
| FS 是否有磁盘容量/LRU | `S` | 没有可配置的磁盘容量、磁盘 LRU 或后台回收水位 |
| FS 可配置参数 | `S` | `root_dir`、`n_read_threads`、`n_write_threads`；上层另有 CPU bytes、block size、eviction policy、prompt-only 等参数 |
| 是否需要重写 FS baseline | `S` | 不需要。正式 baseline 可以直接实例化上游 `FileSystemTierManager` |
| 新 tier 如何无 fork 接入 | `S` | 可通过自定义 offloading spec 和 factory registration 接入，保持 vLLM source tree 不变 |
| candidate 的接口形状 | `S` | `lookup/submit/get_finished/on_* /touch/has_pending_work/drain/shutdown` 的调用边界可由上游和 contract 固定 |
| scheduler 协同是否是替换 engine 的必要部分 | `S` | 不是。engine 可以在不修改 scheduler 语义的条件下替换；协同优化属于另一课题 |

这些结论的主要源码索引记录在
`projects/project6_vllm024_second_tier_study/docs/VLLM_024_ARCHITECTURE.md`。

## 3. 不能只靠阅读源码，必须做功能验证的问题

这些问题不需要性能 campaign，但“代码看起来正确”不是充分证据。

| 问题 | 证据 | 最小验证 |
|---|---|---|
| 锁定 wheel 是否对应目标源码语义 | `V` | distribution/module version、source commit、实际 import path 对账 |
| 自定义 spec/factory 是否能被真实 vLLM 加载 | `V` | 不修改 upstream source 的 import/config smoke |
| native FS 的 store/load 路径在目标环境是否可执行 | `V` | store→lookup→load→checksum |
| second-tier hit 是否能在真实 manager 中 promotion | `V` | T2 强制 spill/revisit，记录 hit、accepted、loaded bytes |
| GPU/CPU local hit 是否真的排除 | `V` | 运行时事件与 loaded tokens/device bytes 对账 |
| candidate 是否满足 exactly-once completion | `V` | success/failure、unknown/duplicate completion contract tests |
| duplicate/in-flight 的状态是否正确 | `V` | 同 key store/load/lookup 交错测试 |
| capacity、LRU、pin 是否无泄漏 | `V` | 有/无可逐出 slot、重叠 load pin、snapshot invariant |
| queue/ring full 是否原子失败 | `V` | fault injection，验证整 job failure 和完整 rollback |
| short I/O/单 block error 是否回滚 | `V` | 定点故障注入、索引不可暴露部分新 key |
| `drain_jobs()` 是否真的停止访问 primary memory | `V` | drain 后复用/保护 memoryview 的测试 |
| O_DIRECT 是否真实启用且无静默 fallback | `V` | 对齐/非对齐 smoke、系统调用或错误路径检查 |
| slab checksum 和 slot offset 是否正确 | `V` | 多 slot、随机顺序、覆盖写和全区 checksum |
| instrumentation 是否漏事件或重复记账 | `V` | submitted/completed job、block、byte、event sequence 全闭合 |
| 进程重启后需要的实验路径是否成立 | `V` | 只有最终 workload 依赖跨进程持久性时才做相应 smoke |

`V` 的通过标准是“路径和状态正确”，不是“实现有性能价值”。

## 4. 必须由性能或因果实验回答的问题

### 4.1 数据面与完整 tier

| 问题 | 为什么源码不够 | 必需实验 |
|---|---|---|
| uring-slab 是否比 native FS 快 | 内核、设备、队列、CPU 和实现常数未知 | 同 offered load 的 paired tier-contract benchmark |
| file-per-block 的代价有多大 | 源码只能证明存在 open/close/rename，不能给出占比 | files→slab 消融 |
| Python pool 的代价有多大 | GIL、线程调度和 I/O 释放行为依赖运行时 | Python pool→C++ pool 消融 |
| io_uring 提交模型贡献多少 | 可能被 io-wq、设备延迟或 batching 掩盖 | C++ blocking slab→uring slab 消融 |
| 最优 queue depth/thread 数是多少 | 属于硬件和 workload 相关参数 | 微基准 sweet-spot 扫描 |
| load/store 并发时谁干扰谁 | 源码只给队列规则，不能给设备争用曲线 | 固定 load，扫描 store pressure；反向亦然 |
| tail latency 是否更稳定 | 调度、设备和云噪声均是动态量 | 重复 paired runs，报告 p95/p99 和 CI |
| CPU core-seconds/GiB 是否降低 | 不能由调用次数直接推断 CPU 实耗 | 进程 CPU、context switch、syscall 计量 |
| FS 是否是当前 workload 的吞吐瓶颈 | 存在 I/O 不等于主导性能 | offered load、queue growth、device 和 job sojourn 联合证据 |

### 4.2 上层可达性与关键路径

| 问题 | 为什么源码不够 | 必需实验 |
|---|---|---|
| 某个合成 trace 是否真正使用 secondary | 控制流只给必要条件，不给实际缓存状态 | T0–T3 小型触发验证 |
| promotion acceptance 在目标压力下是多少 | 取决于实时 free/evictable/pinned slots | capacity × wave × store-pressure replay/smoke |
| secondary wait 占 TTFT 多少 | scheduler step、I/O、H2D 和 GPU 计算均有实际时长 | 时间戳漏斗和 critical-path share |
| 更快 backend 能否穿过 scheduler observation lag | completion 可能等待下一 step | backend A/B 与 completion→observed lag 计量 |
| primary-full 是否吞掉 backend 优势 | 需要比较 rejection 和 recompute 的实际比例 | 单独的 primary-pressure campaign |
| prefix 多长才值得 load 而非 recompute | 两边的常数和并发效应未知 | `T_load(L,c,r)` 与 `T_recompute(L,c)` crossover |
| load/store 压力适用边界在哪里 | 队列与设备服务率未知 | prefix × load pressure × store pressure 边界实验 |

### 4.3 端到端系统收益

以下结论都必须经过端到端实验，微基准不能替代：

- revisit TTFT 是否改善以及改善多少；
- p95/p99 是否改善而不是只改善平均值；
- 固定 TTFT/ITL SLO 下 goodput 是否提高；
- background store 是否伤害 cold request TTFT；
- load/store 是否伤害 decode ITL；
- 数据面优势是否被 H2D、scheduler poll 或 GPU compute 掩盖；
- 负结果的瓶颈究竟位于 backend、CPU primary 还是 scheduler observation。

端到端只比较 native FS 与最终 uring-slab。完整四阶段消融留在微基准。

## 5. 对“是否值得做新 engine”的严格回答

这个问题包含三个不同含义，证据要求不同：

1. **是否有合理工程动机？**

   `S` 足够。FS 的 file-per-block、Python blocking pool、异步 existence
   lookup 和无容量管理，为 slab/C++/io_uring 提供了明确优化机会。

2. **是否值得实现为作品项目？**

   由项目目标决定，不是性能命题。即使最终为负结果，完整的 contract、
   消融和瓶颈定位仍有作品价值。

3. **是否能声称优于 native FS？**

   必须经过 `E`。至少需要完整 tier 微基准的稳定效果量和因果消融；若要声称
   serving 收益，还必须通过关键路径 gate 和端到端 A/B。

所以，源码可以批准“开始实现”，不能批准“性能结论成立”。

## 6. 原始六个问题的证据归属

| 原始问题 | 代码能回答的部分 | 仍需验证/实验的部分 |
|---|---|---|
| 1. scheduler 如何使用 FS、何时使用、参数有哪些 | 调用链、store/load 条件、优先顺序、失败语义、配置项均为 `S` | 某 workload 的实际 hit/load 比例是 `V/E` |
| 2. second tier 如何接入、接口和参数 | 接口、factory/spec 接入、memoryview/job/completion 语义为 `S` | adapter 加载和 contract correctness 为 `V` |
| 3. 什么时候会用 second tier、是否值得新 engine | 必要条件和优化机会为 `S`；不需要证明线上频率 | useful-hit、critical-path share、实际性能价值为 `V/E` |
| 4. FS 哪里值得优化、微基准怎么归因 | 候选开销和消融边界为 `S`；正式 FS 不需重写 | 各机制的效果量、调度影响大小为 `E` |
| 5. 合成 trace 如何设计 | trace 参数到 store/load/promotion 的因果映射可由 `S` 推导 | 实际读写字节、排队、crossover 和适用边界为 `E` |
| 6. 如何管理代码 | 独立 engine、Python adapter、版本和 evidence 规则无需性能实验 | CI、打包和复现实操需要工程验证，但不是研究性能实验 |

## 7. 本项目明确不需要做的实验

以下内容为 `X`，除非以后扩大主命题：

- second-tier workload 在线上出现的频率；
- 真实生产 trace 的代表性；
- LMCache 性能对比；
- scheduler 协同优化；
- 多 GPU、多模型、多文件系统、多云机器的普适性；
- crash recovery、多进程共享写和长期稳定性；
- 生产级容量扩展、碎片整理、监控和运维。

可以在 limitations 中说明未覆盖，但不能写成已证明。

## 8. 最小充分实验集

在上述删减后，主命题只强制以下实验：

1. `V-Correctness`：candidate contract、故障注入、checksum 和事件对账；
2. `E-Micro`：native FS vs final uring-slab 的 paired 完整 tier 微基准；
3. `E-Ablation`：files→slab→C++ pool→io_uring 的集中消融；
4. `V-Reachability`：一个 T2 workload 证明 secondary hit、promotion 和真实
   device read 闭合；
5. `E-CriticalPath`：最小 GPU smoke 测 secondary wait share；
6. `E-E2E`：只有前五项通过才进行 native FS vs final uring-slab A/B。

`no-second-tier` 或 CPU-only 只允许作为资格检查：确认选定 workload 中
secondary load 相对 recompute 具有进入比较的可能性。它们不是主基线，不在每个
微基准或端到端工作点重复。主系统比较始终是 native FS 对 final uring-slab。

不需要先做生产频率研究，也不需要用大量 trace 搜索正结果。

## 9. 答辩时的表述边界

可以说：

- “源码证明 vLLM 0.24.0 的 secondary 路径必须经过 CPU primary。”
- “源码证明 FS 是 O_DIRECT file-per-block，而不是 buffered-file baseline。”
- “功能验证证明我们的 adapter 在锁定版本上完成 exactly-once 和 checksum。”
- “实验表明在这些明确参数下，candidate 的效果量和适用边界如下。”

不可以说：

- “源码证明 uring-slab 一定更快。”
- “fio 更快，所以 vLLM TTFT 一定更快。”
- “FS hit 很多，所以 promotion 一定成功。”
- “一次 smoke 跑通，所以线上 workload 会受益。”
- “一个模型、一台云机的结果代表所有 vLLM 部署。”

核心原则：

> 源码决定机制和可达条件；功能验证决定路径是否正确闭合；性能实验决定量级、
> 瓶颈和收益；主命题之外的问题不实验，也不声称。
