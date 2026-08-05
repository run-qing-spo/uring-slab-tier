# FS 与 uring-slab 稳态50:50读写混合ABBA报告

日期：2026-08-05  
状态：首批8配对完成  
实验机：littlecarrot

## 1. 首批结论

在总HTTP QPS为100、读50 QPS、写50 QPS的稳态混合负载中：

| 请求类型 | uring/FS配对几何均值 | TTFT降低 | 95%区间对应的降低 | uring更快 |
|---|---:|---:|---:|---:|
| read | 0.8314 | 16.86% | 12.14%–21.33% | 8/8 |
| write | 0.7500 | 25.00% | 21.89%–27.99% | 8/8 |

arm算术均值方面，FS read/write分别为9.063/4.156 ms，uring-slab分别为
7.527/3.122 ms。当前是首批结果，应增加独立批次后再固定最终效应量。

## 2. 离群敏感性

上述16.86%和25.00%是保留全部8对的原始结果。按预先声明的backend内
median/MAD规则（`|robust z| > 3.5`）检查read和write arm mean，并删除含任一
离群arm的整对后：

- 第2对的uring read为8.074 ms（z=4.436），uring write为3.515 ms（z=4.799）；
- 第7对的FS read为10.073 ms（z=4.000）；
- 删除第2、7对后，read TTFT降低16.57%，6/6方向一致；
- 删除第2、7对后，write TTFT降低25.99%，6/6方向一致。

第7对会放大uring的read优势，第2对则会压低uring的read/write优势，两者方向相反；
因此过滤前后read效应仅从16.86%变为16.57%。

作为另一项敏感性检查，每个arm的read和write流各自丢弃正式窗口前32条请求后，
read TTFT降低15.43%（8/8），write TTFT降低25.01%（8/8）。因此窗口开头的
post-reset过渡会将read主结果放大约1.4个百分点，但不能解释全部read优势。

## 3. 稳态定义

每个arm使用全新server以及三个互不相交的prompt集合：

```text
seed-store(R) → reset/drain
conditioning-read(R)
conditioning-store(S) → reset/drain
start_window
并行：read sender读取R；write sender写入全新W
reset/drain → end_window
```

read与write sender分别使用独立HTTP client和独立50 QPS开环时间线，二者共享未来
起点；write首请求错后10 ms，使合并到达序列约为每10 ms一个请求。

## 4. 路径与负载验证

16个正式窗口合计：

- 4096个客户端请求全部成功；
- `primary_store_attempts = 2048`，failure为0；
- `primary_promotion_attempts = 16384`，failure为0；
- preemption为0；
- waiting peak最大为2；
- read实际QPS为49.989–50.003；
- write实际QPS为49.987–50.014。

这些计数对应每arm 128个新写请求和128个读请求，每个读prompt包含8个KV block。

## 5. 解释边界

read结果表示写流同时存在时secondary read对请求TTFT的影响，write结果表示读流同时
存在时异步secondary store压力下的请求TTFT。两者均不是纯设备I/O延迟，也不能仅凭
本实验拆分为lookup、I/O或其他控制面部分。

## 6. 源数据

远端根目录：

```text
/home/adminz/uring-slab-experiments/results/fs-uring-mixed-abba-20260805T-mixed-abba-01
```

机器可读汇总：

```text
/home/adminz/uring-slab-experiments/results/fs-uring-mixed-abba-20260805T-mixed-abba-01/summary.json
```

每个arm的关键文件：

```text
client-measurement-read.jsonl
client-measurement-write.jsonl
client-measurement-mixed-summary.json
server-window-summary.json
server.log
configuration.txt
```

脚本：

```text
scripts/run_mixed_open_loop_client.py
scripts/run_tier_mixed_qps_arm.sh
scripts/run_fs_uring_mixed_abba.sh
scripts/analyze_mixed_abba.py
```
