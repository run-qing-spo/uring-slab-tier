# 纯写与读写混合实验设计

日期：2026-08-05  
状态：纯写48配对完成；50:50混合首批8配对完成  

## 1. 共同口径

- 模型、prompt长度、output长度、primary大小和server参数与稳态纯读实验一致；
- QPS始终指实际发起的HTTP请求总QPS；
- 当前固定总QPS为100；
- 每个arm使用全新server；
- FS/uring-slab采用8个紧邻配对、16个arm的ABBA顺序；
- 正式数据集在该arm的正式窗口前不得走过相同的目标路径；
- 预热、conditioning和正式集合使用不同的prompt ID时，保证首个KV block也不同；
- 客户端结束后，在监控窗口内调用reset同步drain全部tier I/O，再结束窗口；
- 请求失败、store/promotion failure或preemption不为0时，该arm不进入性能估计。

## 2. 稳态纯写

纯写不能通过重复写同一prompt实现；同一prompt已经存在于secondary后，再次访问会
变成load。因此使用3组互不重叠的数据：

```text
warmup-store(A) → reset/drain
conditioning-store(B) → reset/drain
start_window
measurement-store(C，全新数据) → reset/drain
end_window
```

正式窗口判据：

- 128个HTTP请求，每个prompt只出现一次；
- `primary_store_attempts = 128`；
- `primary_store_failures = 0`；
- `primary_promotion_attempts = 0`；
- 实际dispatch QPS贴近100；
- client failure和preemption为0。

TTFT解释为“后台secondary写入存在时，请求首token受到的影响”。客户端TTFT不等于
store job完成延迟；后者需使用submit-to-completion计时单独报告。

## 3. 50:50稳态读写混合

### 3.1 总QPS与两个发送端

总HTTP QPS固定为100：

- read sender：50 QPS；
- write sender：50 QPS。

两个发送端共享一个未来的绝对开始时间。read sender在`t=0`发送首请求，write
sender在`t=10 ms`发送首请求；之后两端各每20 ms发送一次。合并后形成均匀的
100 QPS序列，避免两个发送端每20 ms同时发出两个请求造成微突发。

两个发送端均为真正开环，不设置客户端并发槽。server变慢时在途请求自然增长。

### 3.2 数据集合

使用3组互不重叠的数据：

- R：正式read集合，128条；
- S：store路径conditioning集合，128条；
- W：正式write集合，128条。

R先写入secondary，再读一次以进入稳态read状态；S用于让post-reset store路径运行
一次；W在正式窗口前从未出现。

### 3.3 单arm生命周期

```text
seed-store(R)
reset/drain
conditioning-read(R)
conditioning-store(S)
reset/drain
start_window

并行启动：
  read sender：以50 QPS读取R，每条一次
  write sender：以50 QPS写入W，每条一次，首请求错后10 ms

等待两个sender结束
reset/drain
end_window
stop_server
```

正式阶段共256个请求，持续约2.56秒。read和write请求分别记录，不合并成一个无法
解释的TTFT均值。

### 3.4 正式窗口判据

预期server计数：

- read：128个请求，共1024个secondary blocks被promotion；
- write：128个请求，128次request级store reservation；
- `primary_promotion_failures = 0`；
- `primary_store_failures = 0`；
- client failure和preemption为0；
- 两个sender各自的realized dispatch QPS接近50，总计接近100；
- 两端最大dispatch lag单独报告；
- server waiting不能持续增长。

## 4. 结果指标

每个FS/uring配对分别计算：

1. read请求mean、median、p95 TTFT及`uring/FS`配对ratio；
2. write请求mean、median、p95 TTFT及`uring/FS`配对ratio；
3. read与write各自的客户端完成吞吐；
4. peak in-flight、waiting、preemption和primary failure；
5. 若启用async JSONL，分别报告load/store job的submit-to-observed-completion时间。

不把read和write TTFT简单合并作为主指标，因为二者路径和解释不同。

## 5. 配对与统计

首批使用8个配对、16个arm：

```text
FS, uring | uring, FS | FS, uring | uring, FS |
FS, uring | uring, FS | FS, uring | uring, FS
```

独立实验单位为server配对，不把单个prompt当成独立系统重复。主结果保留全部有效
arm；预先声明的median/MAD规则仅作为过滤敏感性分析，并同时报告未过滤结果。

## 6. 后续读写比

先完成50:50。确认两条发送流、计数和路径均正确后，再保持总QPS=100扩展到：

- read 25 / write 75 QPS；
- read 75 / write 25 QPS。

纯读100/0与纯写0/100作为端点，最终形成固定总负载下的读写比曲线。
