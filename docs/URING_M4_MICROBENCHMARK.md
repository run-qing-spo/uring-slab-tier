# M4：当前 uring-slab 数据面微基准

`scripts/benchmark_uring_m4.py` 直接驱动当前 C++ `DataEngine`，保留生产实现的：

- 单个预分配 slab；
- `O_DIRECT`；
- 单 owner io_uring；
- fixed file；
- load 优先和 store QD 限制；
- 多 SQE 批量 `io_uring_submit()`；
- block completion 经 Python poll 聚合为 job completion。

它绕过 lookup、tier control plane、scheduler 和 GPU，使用与 M0 相同的 L100、
S100、L50、S50 和 MIX 时序与 JSON 口径。

## 一次运行完整 M4

```bash
python scripts/run_uring_m4_suite.py \
  --root-dir /mnt/nvme/uring-slab-microbench \
  --output-dir results/m4-suite-01 \
  --block-size-bytes <端到端实验的实际primary row stride> \
  --jobs 128 \
  --blocks-per-job 8 \
  --total-qd 128 \
  --pending-capacity 4096 \
  --max-inflight-jobs 32
```

运行前必须在目标 Linux 环境编译并安装当前仓库，使
`uring_slab_tier._uring_slab_engine` 可导入。`root-dir` 必须与 M0 位于同一待测
NVMe，M0/M4 的 block size、job 数、QPS、poll interval 和 inflight 上限必须一致。

## 结果

`result.directions.load/store` 与 M0 schema 一致；此外
`result.engine_stats_delta` 输出 DataEngine 内部的：

```text
load/store count
enqueue到dispatch的queue time sum/max
dispatch到CQE的time sum/max
```

load 数据在正式窗口前通过同一个 DataEngine store 到 slab，seed completion 和
累计 stats 不进入正式结果。
