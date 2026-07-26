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

下一步先完成 C++ data engine 设计，再开始实现。
