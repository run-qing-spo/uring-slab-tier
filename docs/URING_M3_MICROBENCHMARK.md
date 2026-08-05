# M3：单 SQE submit 消融

M3 与 M4 使用相同的单 slab、C++ DataEngine、io_uring、QD 和 workload；唯一
变化是 `submit_batch_size=1`，每次 `io_uring_submit()` 只提交一个 SQE。

owner 会连续 submit 直到填满可用 QD，所以 M3 消融的是批量提交，不是并发度。

```bash
python scripts/run_uring_m3_suite.py \
  --root-dir /mnt/nvme/uring-slab-microbench \
  --output-dir results/m3-suite-01 \
  --block-size-bytes <实际slot_bytes> \
  --jobs 128 \
  --blocks-per-job 8 \
  --total-qd 128
```

正式结果必须满足：

```text
engine_stats_delta.submit_batch_size_max = 1
engine_stats_delta.submit_calls = engine_stats_delta.submitted_blocks
```

M4 则应观察到大于 1 的实际 batch，否则该 workload 没有触发批量提交，M3/M4
对比不能评价 batching 收益。
