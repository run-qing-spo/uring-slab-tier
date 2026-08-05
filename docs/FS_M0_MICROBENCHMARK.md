# M0：原生 FS 数据面微基准

`scripts/benchmark_fs_m0.py` 绕过 scheduler、lookup 和 tier control plane，直接
复用 vLLM v0.24.0 的：

- `store_block()` / `load_block()`；
- `DualQueueThreadPool`；
- `JobState` completion 聚合。

因此 M0 保留 per-block 文件、`O_DIRECT`、store 临时文件加 rename、load 的
open/readv/close 和原生 Python 双队列线程池。文件名由基准程序生成，不包含
`FileMapper`、异步 existence lookup 或 request 控制面开销。

## 正式运行

必须在安装了目标 vLLM 的 Linux 实验环境运行，`root-dir` 必须位于待测 NVMe：

```bash
python scripts/benchmark_fs_m0.py \
  --direction store \
  --root-dir /mnt/nvme/uring-slab-microbench \
  --block-size-bytes <端到端实验的实际primary row stride> \
  --jobs 128 \
  --blocks-per-job 8 \
  --qps 100 \
  --n-read-threads 16 \
  --n-write-threads 16 \
  --max-inflight-jobs 32 \
  --output results/m0-store.json
```

纯 load 使用相同参数并改为 `--direction load`。程序先通过原生 store 数据面生成
不计时的 load 数据集，再开始正式 load 窗口。`--jobs` 表示每个启用方向的 job
数，因此 mixed 会各提交 128 个 load/store job。

## 五个正式 workload

以下命令省略共同参数；每个命令都需要追加前例中的 `root-dir`、block size、线程、
inflight 和 output 参数。

```bash
# L100：纯 load 100 QPS
python scripts/benchmark_fs_m0.py --direction load --qps 100 ...

# S100：纯 store 100 QPS
python scripts/benchmark_fs_m0.py --direction store --qps 100 ...

# L50：mixed read 的同速率对照
python scripts/benchmark_fs_m0.py --direction load --qps 50 ...

# S50：mixed write 的同速率对照
python scripts/benchmark_fs_m0.py --direction store --qps 50 ...

# MIX：load 50 + store 50 QPS；两条流相差10ms，合并为均匀100 QPS
python scripts/benchmark_fs_m0.py \
  --direction mixed \
  --read-qps 50 \
  --write-qps 50 \
  --write-start-offset-ms 10 \
  ...
```

MIX 共享同一个原生 `DualQueueThreadPool`、primary buffer 和 NVMe，但 load/store
使用互不重叠的文件集合；JSON 在 `result.directions.load/store` 中分别报告两个
方向。

每次运行创建新的 `m0-<run-id>` 目录且拒绝覆盖。正式模式要求 Linux
`O_DIRECT`；`--allow-buffered-dev` 只能用于非 Linux 冒烟检查，输出会标记为不可
用于正式比较。

## 输出

JSON 包含：

- job latency 的 mean/median/p95/p99/max 和原始 samples；
- submit call latency；
- offered schedule 的 dispatch lag；
- job/block/byte throughput；
- 观察到的最大并发 job 数；
- 进程 user/system CPU 和 context switches。

混合干扰使用同速率对照计算：

```text
read_interference = MIX.load.job_latency - L50.job_latency
write_interference = MIX.store.job_latency - S50.job_latency
```

M0 与后续 arm 必须使用相同 block size、blocks/job、QPS、线程或 QD 上限、轮询
间隔和设备。若 dispatch lag 明显增长，说明 `max-inflight-jobs` 或系统处理能力
已经限制 offered load，不能把该 arm 当作干净的 100 QPS 对比。

## 一次运行完整 M0

```bash
python scripts/run_fs_m0_suite.py \
  --root-dir /mnt/nvme/uring-slab-microbench \
  --output-dir results/m0-suite-01 \
  --block-size-bytes <端到端实验的实际primary row stride> \
  --jobs 128 \
  --blocks-per-job 8
```

该命令顺序运行 L100、S100、L50、S50 和 MIX，并拒绝覆盖已存在的 output
目录。
