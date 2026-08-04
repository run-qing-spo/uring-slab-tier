#!/usr/bin/env bash

set -Eeuo pipefail

# none 实验只用于确认基础 server 在目标并发下没有先出现原生瓶颈，
# 不参与 FS 与 uring-slab 的性能对比。正式 workload 固定 128-token prompt
# 和 1-token output，只扫描并发 1、2、4、8。


# ---------- 固定配置 ----------

configure_experiment() {
  local script_dir
  script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

  VLLM_REPO="${VLLM_REPO:-/home/adminz/uring-slab-experiments/repos/vllm-0.24.0}"
  VLLM_PYTHON="${VLLM_PYTHON:-/home/adminz/uring-slab-experiments/venvs/vllm024-cu129-clean/bin/python}"
  MODEL_SNAPSHOT="${MODEL_SNAPSHOT:-/home/adminz/.cache/huggingface/hub/models--facebook--opt-125m/snapshots/27dcfa74d334bc871f3234de431e71c6eeba5dd6}"
  PORT="${PORT:-8000}"
  SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-benchmark-model}"
  CPU_AFFINITY="${CPU_AFFINITY:-0-15}"
  PRIMARY_BYTES="${PRIMARY_BYTES:-67108864}"
  BLOCK_SIZE="${BLOCK_SIZE:-16}"
  MAX_MODEL_LEN="${MAX_MODEL_LEN:-2048}"
  GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
  MAX_NUM_SEQS="${MAX_NUM_SEQS:-256}"
  MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-2048}"
  SERVER_READY_TIMEOUT_SECONDS="${SERVER_READY_TIMEOUT_SECONDS:-180}"
  PROMPT_COUNT="${PROMPT_COUNT:-128}"
  PROMPT_TOKENS="${PROMPT_TOKENS:-128}"
  OUTPUT_TOKENS="${OUTPUT_TOKENS:-1}"
  CLIENT_TIMEOUT_SECONDS="${CLIENT_TIMEOUT_SECONDS:-120}"

  RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
  RESULTS_ROOT="${RESULTS_ROOT:-$PWD/results/none/$RUN_ID}"
  PROMPTS_PATH="${PROMPTS_PATH:-$RESULTS_ROOT/prompts.jsonl}"
  PROMPT_GENERATOR="${PROMPT_GENERATOR:-$script_dir/generate_none_prompts.py}"
  CLOSED_LOOP_CLIENT="${CLOSED_LOOP_CLIENT:-$script_dir/run_closed_loop_client.py}"
  QUALIFICATION_TOOL="${QUALIFICATION_TOOL:-$script_dir/qualify_none_results.py}"
  mkdir -p "$RESULTS_ROOT"

  [[ -d "$VLLM_REPO/vllm" ]] || {
    echo "找不到 vLLM 源码目录：$VLLM_REPO" >&2
    return 1
  }
  [[ -x "$VLLM_PYTHON" ]] || {
    echo "找不到 vLLM Python：$VLLM_PYTHON" >&2
    return 1
  }
  [[ -f "$MODEL_SNAPSHOT/config.json" ]] || {
    echo "本地模型 snapshot 不完整或路径错误：$MODEL_SNAPSHOT" >&2
    return 1
  }
  [[ -f "$MODEL_SNAPSHOT/pytorch_model.bin" ]] || {
    echo "本地模型权重不存在：$MODEL_SNAPSHOT/pytorch_model.bin" >&2
    return 1
  }
  local tokenizer_file
  for tokenizer_file in tokenizer_config.json vocab.json merges.txt; do
    [[ -f "$MODEL_SNAPSHOT/$tokenizer_file" ]] || {
      echo "本地 tokenizer 文件不存在：$MODEL_SNAPSHOT/$tokenizer_file" >&2
      return 1
    }
  done
  [[ -f "$PROMPT_GENERATOR" ]] || {
    echo "找不到 prompt 生成器：$PROMPT_GENERATOR" >&2
    return 1
  }
  [[ -f "$CLOSED_LOOP_CLIENT" ]] || {
    echo "找不到闭环客户端：$CLOSED_LOOP_CLIENT" >&2
    return 1
  }
  [[ -f "$QUALIFICATION_TOOL" ]] || {
    echo "找不到 none 资格检查工具：$QUALIFICATION_TOOL" >&2
    return 1
  }
}


# ---------- Server 生命周期 ----------

start_none_server() {
  local concurrency="$1"
  local point_dir="$RESULTS_ROOT/concurrency-$concurrency"
  local monitor_path="$point_dir/tiering-monitor"
  local server_log="$point_dir/server.log"
  local kv_transfer_config

  mkdir -p "$point_dir"
  kv_transfer_config=$(printf \
    '{"kv_connector":"OffloadingConnector","kv_role":"kv_both","kv_load_failure_policy":"fail","kv_connector_extra_config":{"spec_name":"TieringOffloadingSpec","cpu_bytes_to_use":%s,"block_size":%s,"eviction_policy":"lru","offload_prompt_only":true,"secondary_tiers":[]}}' \
    "$PRIMARY_BYTES" "$BLOCK_SIZE")

  echo "启动 none server：concurrency=$concurrency，日志=$server_log"
  (
    cd "$VLLM_REPO"
    exec env \
      PYTHONHASHSEED=0 \
      PYTHONPATH="$VLLM_REPO" \
      HF_HUB_OFFLINE=1 \
      TRANSFORMERS_OFFLINE=1 \
      VLLM_SERVER_DEV_MODE=1 \
      VLLM_TIERING_MONITOR_MODE=async_jsonl \
      VLLM_TIERING_MONITOR_PATH="$monitor_path" \
      taskset -c "$CPU_AFFINITY" \
      "$VLLM_PYTHON" -m vllm.entrypoints.cli.main serve \
        "$MODEL_SNAPSHOT" \
        --port "$PORT" \
        --served-model-name "$SERVED_MODEL_NAME" \
        --max-model-len "$MAX_MODEL_LEN" \
        --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION" \
        --dtype float16 \
        --block-size "$BLOCK_SIZE" \
        --enable-prefix-caching \
        --max-num-seqs "$MAX_NUM_SEQS" \
        --max-num-batched-tokens "$MAX_NUM_BATCHED_TOKENS" \
        --tensor-parallel-size 1 \
        --pipeline-parallel-size 1 \
        --kv-transfer-config "$kv_transfer_config"
  ) >"$server_log" 2>&1 &

  SERVER_PID=$!
  SERVER_POINT_DIR="$point_dir"
  SERVER_LOG="$server_log"
  printf '%s\n' "$SERVER_PID" >"$point_dir/server.pid"
}

wait_until_server_ready() {
  local concurrency="$1"
  local deadline=$((SECONDS + SERVER_READY_TIMEOUT_SECONDS))

  while (( SECONDS < deadline )); do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
      echo "none server 在就绪前退出：concurrency=$concurrency" >&2
      tail -n 100 "$SERVER_LOG" >&2 || true
      return 1
    fi
    if curl --fail --silent --show-error \
      "http://127.0.0.1:$PORT/health" >/dev/null; then
      if ! curl --fail --silent --show-error \
        "http://127.0.0.1:$PORT/openapi.json" | grep -q tiering_monitor; then
        echo "server 已启动，但 tiering monitor 接口未注册；请检查 VLLM_SERVER_DEV_MODE" >&2
        return 1
      fi
      echo "none server 已就绪：concurrency=$concurrency，pid=$SERVER_PID"
      return 0
    fi
    sleep 1
  done

  echo "等待 none server 超时：${SERVER_READY_TIMEOUT_SECONDS}s" >&2
  tail -n 100 "$SERVER_LOG" >&2 || true
  return 1
}

stop_none_server() {
  local concurrency="$1"
  local attempt

  [[ -n "${SERVER_PID:-}" ]] || return 0
  if kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID"
    for attempt in {1..30}; do
      kill -0 "$SERVER_PID" 2>/dev/null || break
      sleep 1
    done
  fi
  if kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "none server 未在 30s 内正常退出：pid=$SERVER_PID" >&2
    return 1
  fi

  wait "$SERVER_PID" 2>/dev/null || true
  echo "none server 已停止：concurrency=$concurrency"
  SERVER_PID=""
}


# ---------- Workload 准备 ----------

prepare_prompts() {
  env \
    PYTHONHASHSEED=0 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    "$VLLM_PYTHON" "$PROMPT_GENERATOR" \
      --model-snapshot "$MODEL_SNAPSHOT" \
      --output "$PROMPTS_PATH" \
      --count "$PROMPT_COUNT" \
      --tokens "$PROMPT_TOKENS" \
      --block-size "$BLOCK_SIZE"

  local actual_count
  actual_count=$(wc -l <"$PROMPTS_PATH")
  [[ "$actual_count" -eq "$PROMPT_COUNT" ]] || {
    echo "prompt JSONL 行数错误：expected=$PROMPT_COUNT, actual=$actual_count" >&2
    return 1
  }
  echo "固定 prompt 集合已就绪：$PROMPTS_PATH"
}

run_warmup() {
  local concurrency="$1"
  run_closed_loop_phase "$concurrency" warmup
}

reset_before_measurement() {
  local concurrency="$1"

  # run_warmup 返回时所有客户端请求均已结束；external reset 同时清本地
  # GPU prefix cache 和 connector 管理的 CPU primary。
  curl --fail --silent --show-error \
    --request POST \
    "http://127.0.0.1:$PORT/reset_prefix_cache?reset_external=true" \
    >/dev/null
  echo "预热缓存已清理：concurrency=$concurrency"
}


# ---------- 独立监控窗口 ----------

begin_measurement_window() {
  local concurrency="$1"
  local window_id="none-c$concurrency"

  curl --fail --silent --show-error \
    --request POST \
    "http://127.0.0.1:$PORT/tiering_monitor/start_window?window_id=$window_id" \
    >/dev/null
  printf '%s\n' "$window_id" >"$SERVER_POINT_DIR/window-id.txt"
  echo "正式监控窗口已开启：window_id=$window_id"
}

sample_server_metrics() {
  # TODO：正式请求运行时读取 server 已维护的 waiting/running、GPU KV 和 CPU 指标。
  return 0
}

end_measurement_window() {
  local concurrency="$1"
  local summary="$SERVER_POINT_DIR/server-window-summary.json"
  local temporary="$summary.tmp"

  if ! curl --fail --silent --show-error \
    --request POST \
    "http://127.0.0.1:$PORT/tiering_monitor/end_window" \
    --output "$temporary"; then
    unlink "$temporary" 2>/dev/null || true
    return 1
  fi
  if ! "$VLLM_PYTHON" -c \
    'import json, sys; data=json.load(open(sys.argv[1], encoding="utf-8")); assert data["window_id"] == sys.argv[2]' \
    "$temporary" "none-c$concurrency"; then
    unlink "$temporary" 2>/dev/null || true
    return 1
  fi
  mv "$temporary" "$summary"
  echo "正式监控窗口已结束：concurrency=$concurrency，汇总=$summary"
}


# ---------- 正式闭环测试 ----------

run_closed_loop_phase() {
  local concurrency="$1"
  local phase="$2"
  local output="$SERVER_POINT_DIR/client-$phase.jsonl"
  local summary="$SERVER_POINT_DIR/client-$phase-summary.json"

  env PYTHONHASHSEED=0 \
    "$VLLM_PYTHON" "$CLOSED_LOOP_CLIENT" \
      --base-url "http://127.0.0.1:$PORT" \
      --model "$SERVED_MODEL_NAME" \
      --prompts "$PROMPTS_PATH" \
      --output "$output" \
      --concurrency "$concurrency" \
      --max-tokens "$OUTPUT_TOKENS" \
      --timeout-seconds "$CLIENT_TIMEOUT_SECONDS" \
      --phase "$phase" >"$summary"

  echo "闭环请求完成：phase=$phase，concurrency=$concurrency，结果=$output"
}

run_one_concurrency() {
  local concurrency="$1"
  run_closed_loop_phase "$concurrency" measurement
}

run_concurrency_sweep() {
  # TODO：依次执行并发 1、2、4、8；每个点使用干净的初始缓存状态。
  return 0
}


# ---------- 收尾与资格判断 ----------

finalize_results() {
  local archive="${RESULTS_ROOT}.tar.gz"
  local results_parent
  local results_name
  results_parent=$(dirname "$RESULTS_ROOT")
  results_name=$(basename "$RESULTS_ROOT")

  {
    printf 'run_id=%s\n' "$RUN_ID"
    printf 'model_snapshot=%s\n' "$MODEL_SNAPSHOT"
    printf 'served_model_name=%s\n' "$SERVED_MODEL_NAME"
    printf 'prompt_count=%s\n' "$PROMPT_COUNT"
    printf 'prompt_tokens=%s\n' "$PROMPT_TOKENS"
    printf 'output_tokens=%s\n' "$OUTPUT_TOKENS"
    printf 'primary_bytes=%s\n' "$PRIMARY_BYTES"
    printf 'block_size=%s\n' "$BLOCK_SIZE"
    printf 'concurrencies=1,2,4,8\n'
    printf 'vllm_commit=%s\n' "$(git -C "$VLLM_REPO" rev-parse HEAD)"
  } >"$RESULTS_ROOT/configuration.txt"

  (
    cd "$RESULTS_ROOT"
    find . -type f ! -name SHA256SUMS -print0 \
      | sort -z \
      | xargs -0 sha256sum >SHA256SUMS
  )
  tar -C "$results_parent" -czf "$archive" "$results_name"
  echo "结果已归档：$archive"
}

check_none_qualification() {
  "$VLLM_PYTHON" "$QUALIFICATION_TOOL" \
    --results "$RESULTS_ROOT" \
    --concurrencies 1 2 4 8 \
    --prompt-tokens "$PROMPT_TOKENS" \
    --block-size "$BLOCK_SIZE"

  "$VLLM_PYTHON" -c \
    'import json, sys; raise SystemExit(0 if json.load(open(sys.argv[1], encoding="utf-8"))["passed"] else 1)' \
    "$RESULTS_ROOT/qualification.json"
}


cleanup_server_on_exit() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    stop_none_server "${CURRENT_CONCURRENCY:-unknown}" || true
  fi
}


main() {
  trap cleanup_server_on_exit EXIT
  configure_experiment
  prepare_prompts

  local concurrency
  for concurrency in 1 2 4 8; do
    CURRENT_CONCURRENCY="$concurrency"
    # 每个并发点使用全新的 vLLM 进程，避免继承前一点的运行时和缓存状态。
    start_none_server "$concurrency"
    wait_until_server_ready "$concurrency"

    # 在当前新进程内先用正式内容预热，再清掉 GPU prefix cache 和 CPU primary。
    run_warmup "$concurrency"
    reset_before_measurement "$concurrency"

    # server 内部监控始终存在；这里只划定正式窗口并读取它已经维护的数据。
    begin_measurement_window "$concurrency"
    run_one_concurrency "$concurrency"
    end_measurement_window "$concurrency"

    stop_none_server "$concurrency"
  done

  local qualification_status=0
  check_none_qualification || qualification_status=$?
  finalize_results
  return "$qualification_status"
}

main "$@"
