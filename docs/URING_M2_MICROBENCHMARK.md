# M2：C++ blocking pool 消融

M2 保持单 slab、`O_DIRECT`、相同 primary buffer、slot offset、load 优先和 store
并发保留，把 io_uring owner 替换为 C++ worker pool，每个 worker 执行 blocking
`pread/pwrite`。

```bash
python scripts/run_uring_m2_suite.py \
  --root-dir /mnt/nvme/uring-slab-microbench \
  --output-dir results/m2-suite-01 \
  --block-size-bytes <实际slot_bytes> \
  --jobs 128 \
  --blocks-per-job 8 \
  --workers 32
```

M2 与 M3 比较时必须匹配并发上限。主比较建议使用：

```text
M2 workers=32
M3 total_qd=32
```

否则差异会同时包含 blocking/io_uring 模型和不同并发度。M2 的内部
`dispatch_to_cqe` 字段实际表示 worker dispatch 到 `pread/pwrite` 返回的时间，
只是为了保持跨 arm JSON schema 一致。
