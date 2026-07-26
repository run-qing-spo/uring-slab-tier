# vLLM 0.24.0 原生 FS second tier：实现与参数全表

状态：**SOURCE-AUDITED**  
上游版本：vLLM `v0.24.0`  
锁定 commit：`ee0da84ab9e04ac7610e28580af62c365e898389`  
审计日期：2026-07-26

本文只陈述上述 commit 的源码事实。所有行号都指向该 commit，而不是本地
工作树的当前 HEAD。

本地审计仓库位于
`projects/offload/vllm-main-wt`；审计时该工作树 HEAD 是
`54503ecec0f3ac31e5ecfc5f28652e4cc42307b5`，因此取证统一使用
`git show ee0da84ab9e04ac7610e28580af62c365e898389:<path>`，没有把当前
工作树内容当成 v0.24.0。文中的源码路径使用以下缩写：

| 文中路径 | commit 内完整前缀 |
|---|---|
| `fs/...` | `vllm/v1/kv_offload/tiering/fs/...` |
| `tiering/...` | `vllm/v1/kv_offload/tiering/...` |
| `cpu/...` | `vllm/v1/kv_offload/cpu/...` |
| `kv_offload/...` | `vllm/v1/kv_offload/...` |
| `config/...` | `vllm/config/...` |
| `core/...` | `vllm/v1/core/...` |
| `offloading/scheduler.py` | `vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py` |
| `tests/...` | commit 内同名 `tests/...` |

## 1. 结论摘要

原生 FS tier 的准确定位是：

> 一个以 offloaded block 为文件粒度、使用 Python 阻塞线程执行
> `O_DIRECT` I/O、以异步文件存在性检查提供 lookup 的持久 secondary
> block store。

它不是一个完整的磁盘 cache engine。它没有：

- 磁盘容量或 quota；
- secondary LRU、TTL 或高低水位；
- queue depth 或最大 pending job；
- 显式背压或 queue-full 拒绝；
- fd cache、批量系统调用或预分配 slab；
- checksum、文件 header 或内容版本；
- I/O 重试；
- 正常 eviction/delete；
- FS 自身的吞吐、排队和时延指标。

最重要的实现事实：

1. 一个 `.bin` 文件对应一个 **offloaded block**，不是必然对应一个 GPU
   block。一个 offloaded block 可以由 `block_size_factor` 个 GPU block
   组成。
2. Linux 上 store 和 load 都使用 `O_DIRECT`。不能把实验写成
   “buffered FS 对比 direct-I/O uring”。
3. 每个 block 都独立执行 open、一次 write/readv 和 close；store 另外执行
   exists、mkdir 检查、临时文件和 rename。
4. 默认线程模型是 16 个 read-preferred worker 加 16 个
   write-preferred worker。它不是严格的全局 read priority。
5. transfer queue 和 lookup queue 都是无界队列，FS 不提供直接背压。
6. 某 key 首次进入 FS lookup state 时必然返回 `None`；结果要经过
   scheduler step 末尾 flush 才能在后续 step 被观察。
7. load 成功后文件保留；reset 也保留 secondary 文件。只有 load 失败会尝试
   删除源文件。
8. `shutdown()` 会清除尚未执行的 transfer task，且不会为它们生成失败
   completion；它是进程退出语义，不是可恢复的 orderly cancellation。

## 2. 组件与数据路径

```text
TieringOffloadingManager
        |
        | submit_store / submit_load / get_finished_jobs
        v
FileSystemTierManager
        |
        +-- FileMapper
        |     key -> persistent .bin path
        |
        +-- FsAsyncLookupManager
        |     one background lookup thread
        |     os.path.exists(final_path)
        |
        +-- DualQueueThreadPool
              load deque + store deque
              read-preferred workers + write-preferred workers
                    |
                    +-- store_block(): open/write/close/replace
                    +-- load_block():  open/readv/close
```

secondary tier 不能直接访问 GPU。实际路径固定为：

```text
store: GPU -> CPU primary -> FS
load:  FS -> CPU primary -> GPU
```

FS manager 接收的 `primary_kv_view` 是 CPU primary 的共享 mmap
memoryview。一次 task 的内存位置是：

```text
offset = block_id * primary_kv_view.strides[0]
length = primary_kv_view.strides[0]
```

因此实际文件字节数等于 CPU primary 的对齐后 row stride。Tiering spec 会把
每个 offloaded block 的总字节数向 `mmap.PAGESIZE` 对齐，文件可能包含末尾
padding。

源码：

- `fs/manager.py:83-169`
- `tiering/base.py:42-140`
- `cpu/shared_offload_region.py:39-53,159-171`
- `cpu/spec.py:69-104`

## 3. 文件 namespace 与布局

### 3.1 最终路径

每个 key 的最终路径为：

```text
<base_path>_r<rank>/
    <hash[0:3]>/
        <hash[3:5]>_g<group_idx>/
            <full_hash>.bin
```

其中：

```text
base_path =
<root_dir>/<model_name.replace("/", "_")>_<namespace_sha256[:12]>
```

`namespace_sha256[:12]` 是 12 个十六进制字符，即 48 bit namespace
摘要。参与摘要的字段是：

- `model_name`
- `hash_block_size`
- `gpu_blocks_per_file`
- `tp_size`
- `pp_size`
- `pcp_size`
- `dcp_size`
- `dtype`
- `kv_cache_groups`
- `inference_engine`

`rank` 不进入摘要，而是出现在 `_r<rank>` 后缀中。

注意两个目录不是父子关系：

```text
<base_path>/config.json
<base_path>_r<rank>/.../*.bin
```

源码：`file_mapper.py:15-16,24-62,112-139`。

### 3.2 parallel-agnostic 条件

FS manager 总是请求 `parallel_agnostic=True`，但 FileMapper 只在以下条件
全部成立时真正移除 parallelism 差异：

- 只有一个 KV cache group；
- 该 group 是 `FullAttentionSpec`；
- 不是 `MLAAttentionSpec`；
- 没有使用 V2 model runner。

满足时 TP/PP/PCP/DCP 都被强制为 1，rank 被强制为 0。否则保留实际
parallel sizes 和 rank，不能假定不同并行布局共享文件。

源码：`fs/manager.py:110-116`，`file_mapper.py:40-49,85-110`。

### 3.3 `config.json` 的边界

manager 构造时：

1. 创建 `<base_path>`；
2. 如果 `config.json` 不存在，直接用 `open(..., "w")` 写入；
3. 如果已经存在，不读取、不校验也不更新。

这份文件是记录，不是强一致性验证。首次多进程并发创建还有
`exists -> open("w")` 竞态；写入也没有临时文件、rename 或 fsync。

源码：`fs/manager.py:118-125`。

## 4. Lookup 实现

### 4.1 文件存在性

FS lookup 的实际检查只有：

```python
os.path.exists(file_mapper.get_file_name(key))
```

它不检查：

- 文件长度；
- checksum；
- header 或版本；
- 内容是否与 key 匹配；
- 是否能以 `O_DIRECT` 成功打开；
- 是否存在残留临时文件。

源码：`fs/manager.py:45-59`。

### 4.2 异步状态机

每个 FS tier 有一个独立 lookup daemon thread：

1. scheduler lookup 一个尚未进入 lookup state 的 key；
2. 创建 `LookupState(result=None)`；
3. key 加入本 scheduler step 的 `_lookup_batch`；
4. 本次调用返回 `None`；
5. `on_schedule_end()` 把整批 key 放入 lookup queue；
6. worker 串行执行 `os.path.exists`；
7. 后续 scheduler step 的 lookup 才 drain result 并返回 `True/False`。

同一 key 的多个 request 共享一个 lookup state。结果缓存到最后一个引用该
key 的 request 执行 cleanup 为止；期间不会因为新的 store 完成而主动
invalidate cached `False`。

两个 lookup queue 都无界，lookup worker 数量固定为 1，没有配置项。

FS 没有覆盖 `touch()`，所以 secondary file 没有 recency 更新或 LRU。
它也没有覆盖 `has_pending_work()`，继承值为 `False`。普通 transfer
仍由上层 `_transfer_jobs` 使 manager 保持 pending；但只有 lookup
queue/worker 活动时，FS 自身不会通过该接口要求 engine 继续 step。

源码：

- `async_lookup.py:46-105,125-190,196-231`
- `fs/manager.py:135-141,186-191`
- `tiering/base.py:156-172`
- `tiering/manager.py:581-587`

### 4.3 最短 promotion 时序

在理想情况下，一个只存在于 FS 的 block 仍至少经历：

```text
step S:
    first lookup -> None
end S:
    flush filesystem exists lookup

step S+1:
    observe True
    reserve CPU primary slot
    public lookup still returns None
end S+1:
    submit batched FS load

step S+2 or later:
    poll load completion
    primary lookup -> True
```

实际需要几个 wall-clock scheduler step 取决于 lookup 和 load 是否在下一次
poll 前完成。这是源码时序，不是性能测量。

源码：`tiering/manager.py:227-342,567-578`。

## 5. Store 实现

### 5.1 Job 拆分

`submit_store()` 对每个 `(key, block_id)` 构造一个 task：

```text
key -> final file path
block_id -> primary memory offset
one key -> one store_block() call
```

job completion 在所有 block task 结束后才产生。任一 block 失败会令整个
job `success=False`，但不会 fail-fast，其他 block 仍继续执行。

源码：`fs/manager.py:143-155`，`fs/thread_pool.py:21-47,165-180`。

### 5.2 单 block store

`store_block()` 的顺序是：

1. 如果 final file 已存在，立即成功返回；
2. 为当前 worker thread 取得一个长期复用的随机临时 suffix；
3. 创建 parent directories；
4. 从 primary memoryview 取得平坦 byte slice；
5. 使用以下 flags 创建临时文件：

```text
O_CREAT | O_EXCL | O_WRONLY | O_TRUNC | O_DIRECT
```

6. 单次 `os.write()`；
7. 如果短写，抛出异常，不循环补写；
8. close；
9. `os.replace(temp, final)`。

文件创建 mode 是 `0644`，仍受进程 umask 影响。

源码：`fs/io.py:11-24,27-72`。

### 5.3 Duplicate 语义

如果 final path 在 store 开始时已经存在：

- 不重写；
- 不检查长度；
- 不验证 checksum；
- 不确认现有文件能否 direct-read；
- job 的这个 block task 仍视为成功。

两个并发 duplicate store 都可能先看到 final 不存在，分别写自己的临时
文件，再先后 replace；最后完成者覆盖前者。源码没有 per-key lock。

在正常 vLLM key 语义下，相同 key 应表示相同 KV 内容；但 FS 自身没有验证
这一前提。

### 5.4 原子性与持久性

临时文件和 final file 位于同一目录，`os.replace` 使最终文件名的可见切换
具备 namespace 原子性，正常读取者不会看到本进程的半写 final file。

但实现没有：

- `fsync`/`fdatasync` data file；
- fsync parent directory；
- crash recovery；
- startup temp-file scavenging。

因此不能把它描述成 power-loss durable store。

## 6. Load 实现

### 6.1 单 block load

`load_block()`：

1. 取得目标 primary byte slice；
2. `os.open(source, O_RDONLY | O_DIRECT)`；
3. 单次 `os.readv(fd, [view_slice])`，直接写入 primary memory；
4. 短读即失败，不循环补读；
5. close。

成功 load 不删除文件。

源码：`fs/io.py:75-101`。

### 6.2 失败语义

任意 open/readv/短读异常都会：

1. 尝试删除 final source file；
2. 删除失败只写 warning；
3. 重新抛出原异常；
4. 最终由线程池聚合为 `JobResult(success=False)`。

这意味着临时性 I/O 错误、权限错误、O_DIRECT 不支持或 buffer 未对齐，也会
尝试删除原本可能正确的 source file。

如果 readv 在失败前已经修改了部分 primary slice，FS 不负责清零或回滚；
上层以整个 job 失败处理预留的 primary blocks。

一个多 block load 中：

- 已成功读入的 block 不回滚；
- 失败 block 的 source file 会被尝试删除；
- 其他成功 source file 保留；
- 上层只得到一个 job 级 `False`，没有 errno 或失败 key。

文件比目标 block 更长时不会报错，尾部被忽略。相同长度的内容损坏也不会被
FS 检测。

## 7. `O_DIRECT` 的准确边界

源码定义：

```python
O_DIRECT = getattr(os, "O_DIRECT", 0)
```

所以：

- Linux 暴露 `os.O_DIRECT` 时，store/load 都强制 direct I/O；
- macOS 等不暴露时，flag 静默变成 0，即普通 buffered I/O；
- 没有配置开关；
- 没有启动时 capability probe；
- 没有运行时 buffered fallback；
- 没有显式 address/offset/length/filesystem alignment 检查。

目标 Linux 环境中如不满足 direct-I/O 约束，task 会异步失败。Tiering
primary 把 aggregate block stride 对齐到 page size，并使用 mmap
memoryview，这为正式路径提供了对齐基础。

源码：`fs/io.py:11-12,52-66,84-101`，
`cpu/shared_offload_region.py:39-53,159-171`。

## 8. 线程池、优先级和背压

### 8.1 实际优先级模型

线程池有两条 per-block FIFO deque：

- load queue；
- store queue。

线程分为两组：

- read-preferred worker：先取 load queue，空时取 store queue；
- write-preferred worker：先取 store queue，空时取 load queue。

默认是：

```text
16 read-preferred + 16 write-preferred
```

这不是严格全局 read priority：

- 两边持续积压时，近似各 16 路；
- 只有单向工作时，最多 32 个 worker 都能服务该方向；
- `Condition.notify(n_tasks)` 不会定向唤醒某一组；
- task 一旦被 worker 取走，不能抢占；
- 跨两个 queue 没有全局 FIFO。

“read/write priority”更准确的实验表述应是：

> fixed preferred-worker shares with symmetric borrowing when the preferred
> queue is empty.

源码：`fs/thread_pool.py:50-119,153-180`。

### 8.2 无直接背压

transfer queue 是无界 deque；lookup queue 是无界 `SimpleQueue`。FS 没有：

- queue-depth 参数；
- 最大 outstanding job；
- queue-full；
- producer blocking；
- 同步拒绝；
- I/O admission control。

`submit_store/load` 不执行磁盘 I/O，但仍在 scheduler thread 中同步完成
O(number of blocks) 的路径计算、partial 构造和入队，并在入队期间持有
Condition。

积压通过 CPU primary 间接反馈：

- store job 未完成时，source primary blocks 继续被 pin；
- promotion load 未完成时，target primary blocks 继续被 reserved；
- primary 无可用 slot 时，promotion 会被拒绝并表现为 secondary
  unavailable。

因此 FS 深队列可能通过 primary slot/pin 压力影响 serving，而不是在 FS
submit 接口处显式报告背压。

源码：

- `fs/thread_pool.py:65-71,93-119`
- `tiering/manager.py:192-225,271-342,512-537`

## 9. Completion、drain、reset 和 shutdown

### 9.1 Completion

`JobState` 记录：

- `job_id`
- 预期 task 数；
- 已完成 task 数；
- aggregate success。

最后一个 task 完成时，finished queue 收到且只收到一个：

```text
(job_id, aggregate_success)
```

`get_finished_jobs()` 一次排空当时所有结果，并转换为 `JobResult`。结果不包含
方向、bytes、errno 或失败 block；方向由上层保存的 `JobMetadata` 恢复。

上层每 scheduler step 最多 poll 一次。同一个 step 的第一次 poll 之后才完成
的 I/O，通常要到下一 step 才被观察。

源码：

- `fs/thread_pool.py:21-47,121-127,165-180`
- `fs/manager.py:171-179`
- `tiering/manager.py:162-225`

### 9.2 `drain_jobs()`

`drain_jobs()` 只等待 transfer pool 的 `_inflight_jobs == 0`。

返回后：

- 不再有 FS transfer worker 访问 primary memoryview；
- completion 可能仍在 finished queue 等待 poll；
- lookup worker/queue 不一定 idle，但 lookup 不访问 primary memory。

上层 `reset_cache()` 的安全顺序是：

1. 对所有 secondary 调 `drain_jobs()`；
2. poll 所有 transfer completion；
3. 清除尚未 submit 的 deferred promotion；
4. reset CPU primary；
5. 保留 FS 持久文件。

源码：`fs/thread_pool.py:129-138`，
`tiering/manager.py:602-629`。

### 9.3 `shutdown()`

FS shutdown：

1. lookup sentinel 入队并 join lookup thread；
2. transfer pool 设置 stop；
3. 直接清空 load/store queue；
4. 把 inflight job count 强制设为 0；
5. join worker。

未开始 task 不生成失败 completion。这个行为与
`SecondaryTierManager.drain_jobs()` 文档中“取消的 queued transfer 仍应产生
失败结果”的一般要求不一致，但同 commit 的 FS 单测明确把“shutdown 丢弃
pending task”作为预期行为。

正式实验路径应使用：

```text
drain -> poll all completion -> shutdown
```

不得用 pending shutdown 比较两个 backend。

源码：

- `tiering/base.py:211-230`
- `fs/thread_pool.py:140-151`
- `fs/manager.py:193-202`
- `tests/v1/kv_offload/tiering/test_fs_tier.py:255-268`

## 10. 全部配置参数

### 10.1 直接 FS tier 参数

这些字段位于 `secondary_tiers` 中。

| 参数 | 默认 | 校验 | 实际作用 |
|---|---:|---|---|
| `type` | 无 | 必填；内置值为 `"fs"` | factory 选择 `FileSystemTierManager` |
| `root_dir` | 无 | Python 构造参数必填；无额外 schema 校验 | namespace 和 block files 的根目录 |
| `n_read_threads` | `16` | 无正数/类型显式校验 | read-preferred worker 数 |
| `n_write_threads` | `16` | 无正数/类型显式校验 | write-preferred worker 数 |

未知字段会由 factory 原样作为 keyword argument 传给 manager，最终通常产生
Python `TypeError`。

线程数的隐藏边界：

- 一组为 0 时，另一组仍能借用并处理两个方向；
- 两组总数为 0 时，job 会入队但永不执行；
- 负数被 `range()` 当作 0；
- 没有启动时 fail-fast。

源码：`tiering/factory.py:28-52,61-65`，
`fs/manager.py:83-101,127-133`。

### 10.2 启用 tiering/FS 所需参数

| 层级 | 参数 | 默认 | FS 实验要求/作用 |
|---|---|---:|---|
| KV connector | `kv_connector` | `None` | 使用 `"OffloadingConnector"` |
| KV connector | `kv_role` | `None` | connector 启用时必填；本地双向实验用 `"kv_both"` |
| extra config | `spec_name` | `"CPUOffloadingSpec"` | 必须显式设为 `"TieringOffloadingSpec"` |
| extra config | `cpu_bytes_to_use` | 无 | 必填；所有 workers 合计的 CPU primary 容量 |
| extra config | `secondary_tiers` | `[]` | 必须是 list；包含 FS tier config |

`secondary_tiers` 可以有多个 tier。上层对新写入 primary 的 block 向所有
secondary cascade store；lookup 按配置顺序检查 secondary，并对遇到的第一个
hit 发起 promotion。

源码：

- `config/kv_transfer.py:22-60,92-110`
- `kv_offload/factory.py:17-72`
- `cpu/spec.py:60-104`
- `tiering/spec.py:68-165`
- `tiering/manager.py:227-269,481-537`

还有一条 convenience activation path：

| 参数/环境变量 | 默认 | 作用与 FS 边界 |
|---|---:|---|
| `kv_offloading_size` | `None` | 单位 GiB；设置后向 extra config 注入/覆盖 `cpu_bytes_to_use=size*2^30` |
| `kv_offloading_backend` | `"native"` | 只有 `kv_offloading_size` 非空时参与自动 connector 选择；FS 路径必须是 native |
| `VLLM_USE_SIMPLE_KV_OFFLOAD` | `0` | 为 1 时 native convenience path 改用 `SimpleCPUOffloadConnector`，不能进入 Tiering/FS |

`kv_offloading_size` 不会自动把 `spec_name` 改成
`TieringOffloadingSpec`。所以只设置这个 convenience 参数仍是 CPU-only；
若与显式 KV transfer config 合用，也必须保留 Tiering spec 和 FS
`secondary_tiers`。

源码：`config/cache.py:176-185`，`config/vllm.py:775-809`，
`envs.py:1909-1912`。

### 10.3 影响 FS 工作量或可达性的上层参数

| 参数 | 默认 | 校验/特殊语义 | 对 FS 的影响 |
|---|---:|---|---|
| `block_size` | GPU block token size | 指定时必须是统一 GPU block size 的整数倍 | 决定 `block_size_factor`、每文件 bytes 和文件数量 |
| `eviction_policy` | `"lru"` | 仅 `"lru"`/`"arc"` | 只管理 CPU primary；FS 本身无 eviction |
| `offload_prompt_only` | `true` | 使用 Python `bool()` 转换 | true 时不生成 decode block store |
| `kv_load_failure_policy` | `"fail"` | `"fail"`/`"recompute"` | 决定 FS load failure 后请求失败还是重算 |
| request `max_offload_tokens` | 无上限 | 必须是 `type(x) is int and x >= 0` | 每请求 store token 上限 |
| `enable_kv_cache_events` | `false` | 全局 KV event 配置 | 影响上层/primary event；FS 无自己的 I/O event |
| `enable_cross_layers_blocks` | `"False"` | 字符串转小写后与 `"true"` 比较 | 改变 KV packed layout；不是 FS 参数，但实验中必须固定 |

`block_size` 是 token 数，不是直接的 byte size。实际 bytes 由模型 KV
geometry、world size、dtype、KV groups 和 page alignment 派生。

`offload_prompt_only` 应使用 JSON boolean。字符串 `"false"` 经 Python
`bool("false")` 仍为 true。

`KVTransferConfig` 里的 `engine_id`、`kv_buffer_device`、`kv_buffer_size`、
`kv_rank`、`kv_parallel_size`、`kv_ip`、`kv_port` 和
`enable_permute_local_kv` 没有被内置 OffloadingConnector/FS 数据路径消费，
不能当作 FS 调优参数。`kv_connector_module_path` 是自定义 connector 的动态
加载入口，也不是内置 FS 参数。

源码：

- `kv_offload/base.py:445-515`
- `cpu/spec.py:69-104,112-131`
- `tiering/spec.py:66-84,113-155`
- `offloading/scheduler.py:233-267,831-875`
- `config/kv_transfer.py:69-72`
- `core/kv_cache_utils.py:1263-1274`

### 10.4 被拒绝或不生效的 inherited 参数

| 参数 | Tiering 行为 |
|---|---|
| `store_threshold >= 2` | 明确抛出 `ValueError`，不支持 |
| `store_threshold` 为 0/1 | 不启用过滤；Tiering primary 不使用 tracker |
| `max_tracker_size` | 只被 `CPUOffloadingSpec` 读取；Tiering spec 不读取，因而不生效 |
| `self_describing_kv_events=true` | `TieringOffloadingSpec` 构造时明确拒绝 |
| `spec_module_path` | 内置 `TieringOffloadingSpec` 不需要；只用于未注册的自定义 spec |

源码：`cpu/spec.py:48-58,117-131`，
`tiering/spec.py:68-84,151-155`，
`kv_offload/factory.py:33-47`。

### 10.5 影响持久 namespace 的派生配置

这些不是 FS constructor 参数，但会改变文件 namespace 或 key：

- model name；
- cache dtype；
- GPU/hash block size；
- KV cache group 的 block size 和 layer names；
- TP/PP/PCP/DCP 与 rank（除非满足 parallel-agnostic 条件）；
- V2 model runner；
- prefix-caching hash algorithm；
- `PYTHONHASHSEED`。

多个进程共享同一个 `root_dir` 时，必须在进程启动前使用相同的固定
`PYTHONHASHSEED`。否则初始化 chain hash 的 `NONE_HASH` 不同，相同 token
内容也会产生不同 block filenames。FS 只记录这一要求，不验证环境。

源码：

- `file_mapper.py:64-110`
- `fs/manager.py:73-80`
- `core/kv_cache_utils.py:87-114`

### 10.6 FS 没有的配置

以下参数在 v0.24.0 原生 FS tier 中不存在：

- `disk_bytes_to_use`
- `capacity`
- `queue_depth`
- `max_pending_jobs`
- `max_pending_bytes`
- `read_priority`
- `write_priority`
- `lookup_threads`
- `io_engine`
- `direct_io`
- `fsync`
- `file_mode`
- `preallocate`
- `eviction_policy`（FS 自身）
- `high_watermark` / `low_watermark`
- `retry_count`
- `timeout`
- `checksum`
- `compression`
- `delete_after_load`
- `clear_on_reset`

实验配置不能声称调过这些原生 FS 参数。

## 11. 最小正式配置

```json
{
  "kv_connector": "OffloadingConnector",
  "kv_role": "kv_both",
  "kv_load_failure_policy": "fail",
  "kv_connector_extra_config": {
    "spec_name": "TieringOffloadingSpec",
    "cpu_bytes_to_use": 4294967296,
    "block_size": 16,
    "eviction_policy": "lru",
    "offload_prompt_only": true,
    "secondary_tiers": [
      {
        "type": "fs",
        "root_dir": "/mnt/nvme/vllm-kv",
        "n_read_threads": 16,
        "n_write_threads": 16
      }
    ]
  }
}
```

跨进程复用时：

```bash
PYTHONHASHSEED=0
```

只设置 `--kv-offloading-size` 的 convenience path 会自动启用 native CPU
offloading，但默认 spec 仍是 `CPUOffloadingSpec`；它本身不会自动增加 FS
secondary。要使用 FS，配置中仍必须选择 `TieringOffloadingSpec` 并提供
`secondary_tiers`。

源码：`config/vllm.py:775-809`，`kv_offload/factory.py:33-72`。

## 12. 已确认的调用前置条件

FS manager 自身没有完整验证输入，正式路径依赖上层保证：

- `len(keys) == len(block_ids) > 0`；
- `job_id` 唯一；
- block ID 有效；
- primary row address、offset 和 length 满足 direct-I/O alignment；
- store source 在 completion 前保持 pin；
- load target 在 completion 前保持 reserved；
- 生命周期遵循 drain/reset/shutdown 顺序。

特别是：

- task iterable 用 `zip(keys, block_ids)`；
- completion 预期 task 数使用 `len(keys)`。

如果 block IDs 更少或 job 为空，job 可能永远没有 completion，
`drain_jobs()` 永久等待。如果 block IDs 更多，额外 ID 被静默忽略。

源码：`fs/manager.py:143-169`，`fs/thread_pool.py:21-47,93-138`。

### 12.1 正式路径之外的源码级边界

以下不是本项目必须触发的功能，但属于实现审计事实：

- enqueue 没有检查 pool 是否已经 stop；shutdown 后继续 submit 不会
  fail-fast；
- shutdown 把 inflight 强制归零时，正在运行的最后 task 随后仍可能把计数
  减成负数，因此不能把 shutdown 与 drain/submit 并发使用；
- async lookup 的 `batch_lookup()` 调用在 `try` 内，但 FS 返回 generator，
  generator 的实际迭代在 `try` 外；迭代期异常可能结束唯一 lookup worker；
- lookup result 只有 `(key, bool)`，没有 request generation。旧 request
  cleanup 后同 key 被新 request 重新登记时，迟到结果存在写入新 state 的
  ABA 风险；
- cached `False` 不会因为同 request 生命周期中的后续 store 主动失效。

这些边界说明 candidate correctness contract 应明确限定合法生命周期和输入，
而不是把 FS 没有安全支持的通用并发语义强加给 candidate。

源码：`fs/thread_pool.py:93-180`，
`async_lookup.py:125-185,196-231`。

## 13. 对公平实验的直接约束

### 13.1 必须相同的外部条件

FS 与 uring-slab 必须共享：

- 同一个 offloaded block byte geometry；
- 相同 keys、block IDs、job boundaries 和提交顺序；
- 相同 offered load 和 outstanding jobs；
- 相同 CPU primary capacity/policy；
- 相同 completion poll 时点；
- 相同 fresh/persistent state 规则；
- 相同成功 job、block 和 byte 对账；
- 相同 direct-I/O correctness 要求。

不应强迫两者使用相同内部 QD 或线程数。线程池与 io_uring submission model
正是 treatment 的一部分；应控制的是 adapter-visible offered work。

### 13.2 每个正式 run 必须隔离 backing state

FS 遇到已存在文件会把 store 变成 no-op，且 reset 不删除文件。因此每个
正式 run 必须：

- 使用新的隔离 `root_dir`/namespace，或
- 在运行前以可验证方式得到完全相同的预置状态。

candidate slab/index 也必须使用对应的 fresh/prepopulated state。否则比较的
可能是 FS duplicate no-op 对 candidate 真写，或反过来。

### 13.3 容量公平

FS 没有磁盘容量限制，uring-slab 必然有固定 slab capacity。正式 workload
必须保证：

- working set 不超过 candidate capacity；或
- 两边用同一逻辑容量策略，由实验 harness 明确控制。

不能把 candidate capacity eviction 与“无限 FS 文件累计”混为 backend
I/O 性能差异。

### 13.4 需要单独观测的上层放大机制

由于 FS 没有直接背压，至少应观测：

- submitted/completed jobs 和 bytes；
- pending transfer jobs；
- CPU primary pin/reservation；
- promotion accepted/rejected；
- completion 到 scheduler-observed 的 step lag；
- lookup `None` step 数；
- read/store 并发与实际 read share。

否则 FS 深队列可能已经通过 primary saturation 改变了 TTFT，而报告中只看见
最终磁盘带宽。

## 14. 仅靠源码可以和不可以得到的结论

### 14.1 可以直接成立

- FS 是 file-per-offloaded-block，而不是 slab；
- Linux 正式路径 store/load 都强制 `O_DIRECT`；
- store 有 temp file + replace，load 直接 readv 到 primary；
- 每 block 都有独立 open/close，store 还有 metadata 操作；
- 默认 16/16 preferred-worker split，可在队列为空时借用；
- queue 无界，无显式背压；
- lookup 是单线程、按 step flush 的 async existence lookup；
- FS 没有容量、回收和 secondary eviction；
- duplicate store 由 final-file existence 决定；
- reset 保留文件，load 成功保留文件；
- shutdown 会丢弃 queued task；
- 配置表和上述缺失参数。

### 14.2 仍然必须实验

源码不能证明：

- open/close、mkdir、rename 是否是目标机器上的主瓶颈；
- slab 相对 file-per-block 能改善多少；
- Python thread pool 的 CPU 成本是否显著；
- io_uring 相对 C++ blocking pool 的独立贡献；
- read/write preferred-worker split 在何种比例下最优；
- FS queue 积压何时使 primary promotion 被拒绝；
- 更快 backend 是否会转化为 TTFT 或 SLO goodput；
- 正式 workload 中的 second-tier 使用频率。

因此源码阅读负责提出机制和控制变量，性能实验负责估计效应大小，二者不能
互相替代。

## 15. 源码索引

- [FS manager](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/fs/manager.py)
- [FS block I/O](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/fs/io.py)
- [FS dual-queue thread pool](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/fs/thread_pool.py)
- [Async lookup](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/async_lookup.py)
- [FileMapper](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/file_mapper.py)
- [Secondary tier interface](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/base.py)
- [Tiering manager](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/manager.py)
- [Tiering spec](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/spec.py)
- [Secondary tier factory](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/tiering/factory.py)
- [CPU offloading spec](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/cpu/spec.py)
- [Shared primary mmap](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/cpu/shared_offload_region.py)
- [Offloading base/spec](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/v1/kv_offload/base.py)
- [KV transfer config](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/vllm/config/kv_transfer.py)
- [Upstream FS tests](https://github.com/vllm-project/vllm/blob/ee0da84ab9e04ac7610e28580af62c365e898389/tests/v1/kv_offload/tiering/test_fs_tier.py)
