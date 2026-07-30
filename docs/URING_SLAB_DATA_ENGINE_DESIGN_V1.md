# uring-slab 数据引擎设计 V1

1. DataEngine 只负责在 primary 内存与 slab 文件之间搬运 block。
2. 输入是 `BlockIo(job_id, key, primary位置, secondary_slot, store/load)`，输出是 `BlockCompletion(job_id, key, success)`。
3. `job_id` 和 `key` 仅用于原样返回，DataEngine 不管理 lookup、slot 分配和 job 聚合。
4. store 从 primary 写入 `secondary_slot * slot_size`，load 则反向读取。
5. 每个被接受的 `BlockIo` 恰好返回一次 completion，错误或短读写均算失败。
6. primary 内存必须保持有效，直到对应 completion 返回。
7. 第一版使用一个 owner 线程独占一个 io_uring，并批量提交 SQE、收割 CQE。
8. QD 做成配置项，通过压测寻找吞吐和延迟的拐点。
9. 第一版按到达顺序提交 load/store，是否优先 load 或预留 QD 由混合负载测试决定。
10. 只有单个 owner/ring 无法跑满 NVMe 时，才增加彼此独立的 owner/ring。
