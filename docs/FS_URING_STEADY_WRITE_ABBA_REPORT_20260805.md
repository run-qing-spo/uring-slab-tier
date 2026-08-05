# FS 与 uring-slab 稳态纯写 ABBA 对比报告

日期：2026-08-05  
状态：48配对正式实验完成  
实验机：littlecarrot  

## 1. 正式结论

在100 QPS、128-token prompt、1-token output、64 MiB CPU primary tier的稳态
纯写场景中，48个紧邻ABBA配对全部显示uring-slab的请求TTFT低于FS。

配对几何平均`uring-slab / FS = 0.7320`，对应请求TTFT降低26.80%；基于48个
配对log-ratio的95% t区间为0.7258–0.7382，对应TTFT降低26.18%–27.42%。

96个arm中，48个FS arm的算术均值为4.175 ms，48个uring-slab arm为3.057 ms。这个结论
描述的是异步secondary store压力下的请求TTFT，不是单个写任务的完成延迟。

## 2. “稳态纯写”的定义

正式数据不能在窗口前出现，否则再次访问会变成secondary load而不是store。每个
arm使用3组互不重叠的prompt：

```text
warmup-store(A) → reset/drain
conditioning-store(B) → reset/drain
start_window
measurement-store(C，全新数据) → reset/drain
end_window
```

三个集合各包含128条128-token prompt，首个KV block在集合内和集合间均不同。
正式客户端对C中每条prompt只请求一次。

## 3. 固定配置

| 项目 | 配置 |
|---|---|
| 模型 | facebook/opt-125m，本地snapshot |
| 正式prompt | 128条 × 128 tokens |
| output | 1 token |
| offered load | 干净开环100 HTTP QPS |
| CPU primary | 64 MiB |
| KV block size | 16 tokens |
| max model length | 2048 |
| max num seqs | 256 |
| max batched tokens | 2048 |
| monitor | memory窗口累加 |
| 配对顺序 | ABBA，A=FS，B=uring-slab |

## 4. 六批结果

| 批次 | 配对数 | uring/FS | TTFT降低 | uring更快 |
|---:|---:|---:|---:|---:|
| 01 | 8 | 0.7216 | 27.84% | 8/8 |
| 02 | 8 | 0.7318 | 26.82% | 8/8 |
| 03 | 8 | 0.7284 | 27.16% | 8/8 |
| 04 | 8 | 0.7358 | 26.42% | 8/8 |
| 05 | 8 | 0.7365 | 26.35% | 8/8 |
| 06 | 8 | 0.7379 | 26.21% | 8/8 |
| **合并** | **48** | **0.7320** | **26.80%** | **48/48** |

首批27.84%并非单批偶然结果；后五批独立估计为26.21%–27.16%，方向一致。
正式效应使用每对arm mean的ratio，再对48个ratio取几何平均。

顺序敏感性方面，FS先运行的24对降低26.14%，uring先运行的24对降低27.46%；
两种顺序结论一致，但存在约1.3个百分点的次序差，因此正式结果仍以ABBA平衡后的
全部48对为准。

## 5. 数据路径验证

96个正式窗口合计：

- `primary_store_attempts = 12288`，恰好等于96 × 128；
- `primary_store_failures = 0`；
- `primary_promotion_attempts = 0`；
- 12288个客户端请求全部成功；
- preemption为0；
- server waiting peak最大为1，没有持续排队；
- 实际dispatch QPS为99.953–100.034；
- 最大dispatch lag为1.164 ms。

每个arm的客户端完成后，脚本在正式窗口内调用reset，同步drain全部tier I/O，随后
才结束窗口。因此正式窗口覆盖了写任务完成，但客户端TTFT本身不包含客户端结束后
额外发生的drain等待。

## 6. 解释边界

当前26.80%的结果表示：

> 在持续产生全新secondary store的100 QPS请求流中，完整uring-slab backend下
> 的请求TTFT相对FS更低。

它不等于secondary写完成延迟降低26.80%，也不能单独归因于io_uring。FS与
uring-slab还同时改变了数据布局、控制面、语言实现、提交和completion模型。

## 7. 原始数据与脚本

远端六批原始数据根目录：

```text
/home/adminz/uring-slab-experiments/results/fs-uring-write-abba-20260805T-write-abba-01
/home/adminz/uring-slab-experiments/results/fs-uring-write-abba-20260805T-write-abba-02
/home/adminz/uring-slab-experiments/results/fs-uring-write-abba-20260805T-write-abba-03
/home/adminz/uring-slab-experiments/results/fs-uring-write-abba-20260805T-write-abba-04
/home/adminz/uring-slab-experiments/results/fs-uring-write-abba-20260805T-write-abba-05
/home/adminz/uring-slab-experiments/results/fs-uring-write-abba-20260805T-write-abba-06
```

包含48个逐配对结果和全部汇总字段的机器可读文件：

```text
/home/adminz/uring-slab-experiments/results/fs-uring-write-abba-48pairs-summary.json
```

每个arm目录中的关键文件：

```text
client-measurement-write.jsonl
client-measurement-write-summary.json
server-window-summary.json
server.log
configuration.txt
```

prompt集合：

```text
prompts-warmup.jsonl
prompts-conditioning.jsonl
prompts-measurement.jsonl
```

脚本关系：

```text
run_fs_uring_write_abba.sh
  └── 依次调用 run_tier_write_qps_arm.sh
        ├── 启动一个全新server
        ├── warmup/conditioning
        ├── 正式纯写窗口
        ├── drain并保存结果
        └── 关闭server
```

本地脚本：

```text
scripts/run_tier_write_qps_arm.sh
scripts/run_fs_uring_write_abba.sh
scripts/analyze_write_abba.py
```

## 8. 离群敏感性

按backend分别对96个arm mean应用median/MAD稳健z-score规则，阈值为`|z| > 3.5`。
本次没有任何arm被标记，因此过滤后结果与原始48配对结果完全相同，没有依赖人工
剔除离群点。
