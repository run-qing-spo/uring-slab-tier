# M1：Python pool + 单 slab 消融

M1 与 M0 使用相同的原生 `DualQueueThreadPool`、线程数、job 流、primary buffer
和 completion 聚合，只把数据布局从 per-block 文件改为一个预分配 slab：

```text
M0：每block open/read或临时文件write/rename/close
M1：持久fd + preadv/pwritev + slot offset
```

```bash
python scripts/run_slab_m1_suite.py \
  --root-dir /mnt/nvme/uring-slab-microbench \
  --output-dir results/m1-suite-01 \
  --block-size-bytes <实际slot_bytes> \
  --jobs 128 \
  --blocks-per-job 8 \
  --n-read-threads 16 \
  --n-write-threads 16
```

M0 与 M1 的差异应解释为 slab layout 机制包，包括单文件、预分配、持久 fd、
offset 定位以及移除 per-block open/close/rename；不能进一步拆成某一个 syscall
的独立贡献。
