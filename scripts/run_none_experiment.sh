#!/usr/bin/env bash

# none 实验只用于确认基础 server 在目标并发下没有先出现原生瓶颈，
# 不参与 FS 与 uring-slab 的性能对比。正式 workload 固定 128-token prompt
# 和 1-token output，只扫描并发 1、2、4、8。


# ---------- 固定配置 ----------

configure_experiment() {
  # TODO：集中定义模型、端口、primary、prompt、输出长度、并发点和结果目录。
  return 0
}


# ---------- Server 生命周期 ----------

start_none_server() {
  # TODO：以 64 MiB CPU primary、无 secondary tier 的配置启动 vLLM。
  return 0
}

wait_until_server_ready() {
  # TODO：等待健康检查通过，并保存 server 启动日志和最终解析配置。
  return 0
}

stop_none_server() {
  # TODO：正常停止本次 server，并确认监控后台数据已经写完。
  return 0
}


# ---------- Workload 准备 ----------

prepare_prompts() {
  # TODO：生成固定的一组 128-token prompts；同一点内每条只发送一次，
  # 不循环制造 GPU/CPU prefix hit；不同并发点在 reset 后重放同一组。
  return 0
}

run_warmup() {
  # TODO：使用与正式阶段相同的请求形状完成 JIT 和运行时预热。
  return 0
}

reset_before_measurement() {
  # TODO：等待请求 drain，通过 external reset 清 GPU prefix cache 和 CPU primary。
  return 0
}


# ---------- 独立监控窗口 ----------

begin_measurement_window() {
  # TODO：记录当前时间和 Prometheus 累计值，只让本点请求进入 JSONL 正式窗口。
  return 0
}

sample_server_metrics() {
  # TODO：窗口内采样 waiting/running、GPU KV 使用率和 CPU primary 使用指标。
  return 0
}

end_measurement_window() {
  # TODO：等待本点请求与 GPU→CPU store 完成，再保存窗口终点和累计值差分。
  return 0
}


# ---------- 正式闭环测试 ----------

run_one_concurrency() {
  # TODO：无 QPS 限速地运行固定数量的闭环 workers；记录每请求 TTFT，
  # 并保证本点 prompt 不重复、每个并发点使用独立监控窗口。
  return 0
}

run_concurrency_sweep() {
  # TODO：依次执行并发 1、2、4、8；每个点使用干净的初始缓存状态。
  return 0
}


# ---------- 收尾与资格判断 ----------

finalize_results() {
  # TODO：归档客户端结果、server JSONL、server 日志和每个并发点的配置。
  return 0
}

check_none_qualification() {
  # TODO：硬性检查请求失败、新 JIT、preemption、primary reservation failure
  # 和非预期 cache hit 均为零；再判断队列是否持续增长及 TTFT 是否异常抬升。
  return 0
}


main() {
  # TODO：确认骨架后，再按以上阶段串联完整实验流程。
  return 0
}

# 当前文件仅为待审核骨架，暂不调用 main，避免误启动实验。
