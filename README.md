# uring-slab tier

目标：为 vLLM 0.24.0 设计并实现一个纯 C++ uring-slab second-tier data
engine，再通过薄 Python adapter 接入 vLLM。

当前尚未实现 candidate，也没有 candidate 性能数据。

## 代码边界

- `csrc/`：未来的纯 C++ data engine。
- `python/`：未来的薄 binding / vLLM adapter。
- `docs/`：实现前必须理解的接口分析、设计约束和冻结决策。

目标 vLLM 版本为 `v0.24.0`，官方 tag commit：
`ee0da84ab9e04ac7610e28580af62c365e898389`。

## 下一步与验收原则

1. 先完成 C++ data engine 设计，再开始实现。
2. data engine 微基准以持续读取吞吐为主指标，但 correctness、错误率、
   超时、load latency p95/p99 和最长 stall 都是硬约束；不能用排队换吞吐。
3. 接入 vLLM 后必须再做端到端验收，TTFT、ITL 或 SLO goodput 任一超过
   预先冻结的容忍范围，即使吞吐提升超过 10% 也判 candidate 不可用。

具体延迟阈值尚未冻结；必须在看到 candidate 性能数据前，根据原生 FS
baseline 或实际服务 SLO 确定，不能根据 candidate 结果事后调整。
