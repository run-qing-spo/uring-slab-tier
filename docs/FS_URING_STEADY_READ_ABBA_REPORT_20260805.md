# FS 与 uring-slab 稳态纯读 ABBA 对比报告

日期：2026-08-05  
状态：稳态纯读阶段结论  
实验机：littlecarrot  

## 1. 摘要

在 100 QPS、128-token prompt、1-token output、64 MiB CPU primary tier 的
稳态重复读取场景下，完成了4个独立批次，共32个紧邻配对、64个全新 vLLM
server 的 FS/uring-slab ABBA 对比。

以每个 arm 的平均 TTFT 为指标，32个配对的 `uring-slab / FS` 几何平均比值为
0.8737，即 uring-slab 的 TTFT 约低12.63%。基于32个配对 log-ratio 的 t 区间为
0.8534–0.8946，对应约10.54%–14.66%的降低。uring-slab 在32个配对中的31个
更快。

按照追加实验前固定的规则，在每个 backend 内用 arm mean 的 median/MAD 计算
稳健 z-score，并以 `|z| > 3.5` 标记离群 arm；删除包含离群 arm 的完整配对后，
保留22个配对，uring-slab 在22/22个配对中更快，TTFT降低12.08%。未过滤与
过滤后的效应接近，因此本报告将结论固定为：

> 当前证据支持完整 uring-slab backend 在该稳态纯读工作点上相对原生 FS
> 具有约12%的 TTFT 优势；慢状态的来源及具体子机制尚未确定。

## 2. 实验目的与边界

本实验比较的是完整 backend：

- 原生 FS secondary tier；
- 当前完整 uring-slab secondary tier。

它不是只替换 I/O syscall 的消融实验。两端还同时存在文件布局、lookup、控制面、
Python/C++ 实现及 completion 模型等差异，因此结果不能单独归因于 io_uring。

此前 none 基线用于确认 100 QPS 工作点没有先被原生推理、GPU 调度或客户端发送
能力主导。本次正式窗口进一步要求无请求失败、无 preemption、无
store/promotion failure，且实际发送速率贴近 100 QPS。

## 3. 固定配置

| 项目 | 配置 |
|---|---|
| 模型 | facebook/opt-125m，本地 snapshot |
| prompt | 128 条，每条 128 tokens，每条首个 KV block 唯一 |
| output | 1 token |
| offered load | 干净开环 100 QPS |
| CPU primary | 64 MiB |
| KV block size | 16 tokens |
| prefix caching | 开启 |
| max model length | 2048 |
| max num seqs | 256 |
| max batched tokens | 2048 |
| tensor/pipeline parallel | 1 / 1 |
| monitor | memory 模式，正式窗口内累加 |

每条 128-token prompt 对应 8 个 KV blocks，因此每个正式窗口共有 1024 次
secondary-to-primary promotion。

## 4. 稳态重复读取协议

每个 arm 使用一个全新 server，执行：

1. 用固定 prompt 集合运行一次，向 secondary 写入数据；
2. `reset_prefix_cache?reset_external=true`，同步 drain tier I/O 并清理 GPU/primary；
3. 重复读取同一 prompt 集合作为丢弃的 conditioning；
4. 再次 reset GPU/primary；
5. 开启正式监控窗口；
6. 重复读取同一 prompt 集合并记录正式 TTFT；
7. 结束窗口并关闭 server。

reset 保留 secondary residency，因此正式窗口是稳态 secondary read，不是首次读取。
所有正式窗口均记录到：

- `primary_promotion_attempts = 1024`；
- `primary_promotion_failures = 0`；
- `primary_store_attempts = 0`。

## 5. ABBA 配对设计

A 固定为 FS，B 固定为 uring-slab。8 个配对、16 个 arm 的执行顺序为：

```text
FS, uring | uring, FS | FS, uring | uring, FS |
FS, uring | uring, FS | FS, uring | uring, FS
```

arm 之间不加入额外等待。每个配对内两端使用完全相同的 prompt、顺序和 QPS。

## 6. 首批完整结果

| 配对 | 顺序 | FS mean TTFT | uring mean TTFT | uring / FS | uring 变化 |
|---:|---|---:|---:|---:|---:|
| 1 | FS → uring | 8.468 ms | 8.867 ms | 1.0472 | +4.7% |
| 2 | uring → FS | 8.520 ms | 7.453 ms | 0.8747 | −12.5% |
| 3 | FS → uring | 8.491 ms | 7.496 ms | 0.8828 | −11.7% |
| 4 | uring → FS | 8.507 ms | 7.314 ms | 0.8597 | −14.0% |
| 5 | FS → uring | 10.237 ms | 7.504 ms | 0.7330 | −26.7% |
| 6 | uring → FS | 8.530 ms | 7.615 ms | 0.8928 | −10.7% |
| 7 | FS → uring | 8.506 ms | 7.381 ms | 0.8677 | −13.2% |
| 8 | uring → FS | 8.521 ms | 7.354 ms | 0.8630 | −13.7% |

算术汇总的 arm mean 为：

- FS：8.723 ms；
- uring-slab：7.623 ms。

正式效应使用配对 ratio，而不是两个 backend 的未配对算术均值。

## 7. 首批稳健性检查

| arm 内指标 | 配对几何均值 uring/FS | 变化 | 95% log-ratio t 区间 | uring 更快 |
|---|---:|---:|---:|---:|
| mean TTFT | 0.8741 | −12.6% | 0.8066–0.9472 | 7/8 |
| median TTFT | 0.8664 | −13.4% | 0.7758–0.9677 | 7/8 |
| 去掉前 32 请求后的 mean | 0.8750 | −12.5% | 0.8160–0.9383 | 7/8 |

配对顺序没有显示出足以解释主效应的差异：

- FS 先运行的4组，平均 ratio 约 0.883；
- uring-slab 先运行的4组，平均 ratio 约 0.873。

需要注意：三种估计量来自同一批实验数据，并不是三批独立重复。`7/8` 的双侧
精确符号检验约为 `p = 0.0703`；95% t 区间排除 1 则依赖配对 log-ratio
近似假设。不能把 128 个 prompt 当成 128 个独立 server 重复来扩大样本量。

## 8. 四批合并结果

随后按完全相同协议追加3个独立批次。合并4批共32个配对、64个独立server：

| 口径 | 配对数 | uring更快 | uring/FS | TTFT变化 | 95%区间 |
|---|---:|---:|---:|---:|---:|
| 未过滤 | 32 | 31/32 | 0.8737 | −12.63% | 0.8534–0.8946 |
| 预设MAD过滤 | 22 | 22/22 | 0.8792 | −12.08% | 0.8727–0.8858 |

4个独立批次的未过滤效应分别为：

- batch01：−12.59%；
- batch02：−14.53%；
- batch03：−12.01%；
- batch04：−11.33%。

过滤规则在追加实验开始前固定：在每个backend内部，对arm mean TTFT使用
median/MAD计算稳健z-score，`|z| > 3.5`标记离群；若任一arm被标记，则删除其
所在的整个配对。该规则标记7个FS arm和3个uring-slab arm，共删除10个配对。
过滤只作为预设敏感性分析，未过滤结果仍为主结果。

未过滤与过滤估计只相差0.55个百分点，说明约12%的效应不依赖离群处理。

## 9. 异常臂

首批实验中，两个方向各出现一个整臂慢状态：

- 配对1的 uring-slab：8.867 ms，高于其余多数 uring arm；
- 配对5的 FS：10.237 ms，高于其余多数 FS arm。

保留这两个 arm 后，主估计为 −12.6%；仅作敏感性检查、同时移除这两个配对时，
剩余6组的几何平均估计约为 −12.7%。因此当前效应大小不依赖这两个异常恰好抵消。

但是，没有证据证明两个慢状态具有相同原因，也不能把它们直接定义为后端无关
伪影。它们可能来自 backend、设备或外部环境，必须保留在正式结果中。

追加批次证明慢状态并非只出现一次；按预设规则，32个FS arm中有7个、32个
uring-slab arm中有3个被标记。该发生率差异目前不用于机制结论，也不作为删除
原始数据的理由。

## 10. 运行有效性

64个正式arm中：

- 实际 dispatch QPS：99.952–100.034；
- 最大 dispatch lag：2.120 ms；
- 客户端请求失败：0；
- promotion failure：0；
- store failure：0；
- preemption：0；
- server waiting peak：最大为2，没有观察到持续增长。

因此没有证据表明本次结果由客户端限流失败、primary reservation failure、
GPU preemption 或持续 server 排队主导。

## 11. 当前可以与不可以声称的结论

可以声称：

1. 在指定配置和稳态重复读取协议下，完整 uring-slab backend 的 TTFT 配对估计
   比原生FS低约12.6%；
2. 该优势在31/32个未过滤配对和22/22个过滤后配对中出现，并跨4个批次复现；
3. 正式窗口确实执行 pure load/promotion，没有混入 store failure 或重算瓶颈。

暂时不能声称：

1. 12.6% 全部由 io_uring 单独贡献；
2. 两个慢臂一定是设备噪声或 reset 伪影；
3. 该结果代表首次 secondary read；
4. 32个配对足以完整描述低概率慢状态的真实发生率；
5. 结果可以直接外推到其他模型、prompt 长度、QPS、读写比或硬件。

## 12. 稳态纯写初步结果

纯写使用3组互不重叠的prompt：第一组预热store路径，reset并同步drain；第二组
吸收post-reset store路径的一次性状态，再次reset并drain；正式窗口只写入从未
出现过的第三组。客户端完成后，在结束监控窗口前再次reset，确保所有异步写均已
完成。

完成1个批次、8个ABBA配对、16个独立server：

| 配对 | 顺序 | FS mean TTFT | uring mean TTFT | uring/FS |
|---:|---|---:|---:|---:|
| 1 | FS → uring | 4.303 ms | 3.109 ms | 0.7225 |
| 2 | uring → FS | 4.212 ms | 2.976 ms | 0.7066 |
| 3 | FS → uring | 4.134 ms | 3.089 ms | 0.7472 |
| 4 | uring → FS | 4.319 ms | 2.994 ms | 0.6931 |
| 5 | FS → uring | 4.106 ms | 2.942 ms | 0.7164 |
| 6 | uring → FS | 4.168 ms | 3.041 ms | 0.7295 |
| 7 | FS → uring | 4.162 ms | 3.027 ms | 0.7275 |
| 8 | uring → FS | 4.249 ms | 3.108 ms | 0.7315 |

配对几何平均`uring/FS = 0.7216`，即uring-slab的TTFT低27.84%；基于8个
log-ratio的t区间为0.7079–0.7357，对应约26.43%–29.21%的降低，uring-slab
在8/8个配对中更快。

16个正式窗口合计：`store_attempts=2048`、`store_failures=0`、
`promotion_attempts=0`、请求失败为0、preemption为0，实际dispatch QPS为
99.953–100.024。

这里的指标是“异步secondary store存在时的请求TTFT”。虽然窗口结束前同步等待了
所有写完成，但客户端TTFT不包含客户端结束后的drain等待，因此不能把27.84%解释为
secondary写任务完成时间的提升；写完成延迟仍需逐job计时。

## 13. 后续工作

1. 追加独立纯写ABBA批次，确认27.84%的初步效应能否跨批次复现；
2. 进行总HTTP QPS固定为100的50:50读写混合ABBA实验；
3. 使用submit、设备完成和completion收割分段计时定位慢状态；
4. 用files → slab → C++ pool → io_uring消融实验拆分具体机制；
5. 将首次读取场景作为独立实验保留，不与稳态重复读取结论混用。

## 14. 结果与脚本位置

远端原始结果：

```text
/home/adminz/uring-slab-experiments/results/
fs-uring-abba-20260805T-fs-uring-abba-01
```

本地编排脚本：

```text
scripts/run_fs_uring_abba.sh
```

单臂生命周期脚本位于远端仓库：

```text
/home/adminz/uring-slab-experiments/repos/uring-slab-tier/
scripts/run_tier_qps_arm.sh
```

远端纯写结果：

```text
/home/adminz/uring-slab-experiments/results/
fs-uring-write-abba-20260805T-write-abba-01
```

纯写脚本：

```text
scripts/run_tier_write_qps_arm.sh
scripts/run_fs_uring_write_abba.sh
```
