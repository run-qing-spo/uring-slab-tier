#!/usr/bin/env bash

set -Eeuo pipefail

# 用 A 组预热 secondary load 路径，再正式测量从未读过的 B 组。
# 每个实验臂使用全新 server，A/B 各 128 条且彼此唯一。

QPS="${1:?用法: run_uring_disjoint_conditioning_arm.sh <qps> <arm_id> [results_root]}"
ARM_ID="${2:?缺少 arm_id}"
RESULTS_ROOT="${3:-/home/adminz/uring-slab-experiments/results/uring-disjoint-$(date -u +%Y%m%dT%H%M%SZ)}"

REPO="${REPO:-/home/adminz/uring-slab-experiments/repos/uring-slab-tier}"
VLLM_REPO="${VLLM_REPO:-/home/adminz/uring-slab-experiments/repos/vllm-0.24.0}"
PYTHON="${VLLM_PYTHON:-/home/adminz/uring-slab-experiments/venvs/vllm024-cu129-clean/bin/python}"
MODEL="${MODEL_SNAPSHOT:-/home/adminz/.cache/huggingface/hub/models--facebook--opt-125m/snapshots/27dcfa74d334bc871f3234de431e71c6eeba5dd6}"
PORT="${PORT:-8000}"
MODEL_NAME="${SERVED_MODEL_NAME:-benchmark-model}"
CPU_AFFINITY="${CPU_AFFINITY:-0-15}"
PROMPT_COUNT="${PROMPT_COUNT:-128}"
PROMPT_TOKENS="${PROMPT_TOKENS:-128}"
OUTPUT_TOKENS="${OUTPUT_TOKENS:-1}"
PRIMARY_BYTES="${PRIMARY_BYTES:-67108864}"
BLOCK_SIZE="${BLOCK_SIZE:-16}"
DISK_BYTES="${DISK_BYTES:-137438953472}"
READY_TIMEOUT="${SERVER_READY_TIMEOUT_SECONDS:-240}"

ARM_DIR="$RESULTS_ROOT/qps-$QPS/$ARM_ID-uring_slab"
PROMPTS_A="$RESULTS_ROOT/prompts-a.jsonl"
PROMPTS_B="$RESULTS_ROOT/prompts-b.jsonl"
SERVER_LOG="$ARM_DIR/server.log"
SLAB_PATH="$ARM_DIR/uring-slab.bin"
SERVER_PID=""
mkdir -p "$ARM_DIR"

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    for _ in {1..30}; do
      kill -0 "$SERVER_PID" 2>/dev/null || break
      sleep 1
    done
  fi
  [[ -z "$SERVER_PID" ]] || wait "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT

generate_prompts() {
  local output="$1"
  local start_index="$2"
  env HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONHASHSEED=0 \
    "$PYTHON" "$REPO/scripts/generate_none_prompts.py" \
      --model-snapshot "$MODEL" --output "$output" \
      --count "$PROMPT_COUNT" --start-index "$start_index" \
      --tokens "$PROMPT_TOKENS" --block-size "$BLOCK_SIZE"
}

[[ -f "$PROMPTS_A" ]] || generate_prompts "$PROMPTS_A" 0
[[ -f "$PROMPTS_B" ]] || generate_prompts "$PROMPTS_B" "$PROMPT_COUNT"

EXTRA_CONFIG=$(printf '%s' \
  '{"spec_name":"UringSlabOffloadingSpec","spec_module_path":"uring_slab_tier.vllm_spec","cpu_bytes_to_use":'"$PRIMARY_BYTES"',"block_size":'"$BLOCK_SIZE"',"eviction_policy":"lru","offload_prompt_only":true,"secondary_tiers":[{"type":"uring_slab","disk_bytes_to_use":'"$DISK_BYTES"',"slab_path":"'"$SLAB_PATH"'","total_qd":128,"pending_capacity":4096}]}')
KV_CONFIG=$(printf '%s' \
  '{"kv_connector":"OffloadingConnector","kv_role":"kv_both","kv_load_failure_policy":"fail","kv_connector_extra_config":'"$EXTRA_CONFIG"'}')

(
  cd "$VLLM_REPO"
  exec env PYTHONHASHSEED=0 \
    PYTHONPATH="$VLLM_REPO:$REPO" \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    VLLM_SERVER_DEV_MODE=1 VLLM_TIERING_MONITOR_MODE=memory \
    taskset -c "$CPU_AFFINITY" "$PYTHON" -m vllm.entrypoints.cli.main serve \
      "$MODEL" --port "$PORT" --served-model-name "$MODEL_NAME" \
      --max-model-len 2048 --gpu-memory-utilization 0.90 --dtype float16 \
      --block-size "$BLOCK_SIZE" --enable-prefix-caching \
      --max-num-seqs 256 --max-num-batched-tokens 2048 \
      --tensor-parallel-size 1 --pipeline-parallel-size 1 \
      --kv-transfer-config "$KV_CONFIG"
) >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!
printf '%s\n' "$SERVER_PID" >"$ARM_DIR/server.pid"

deadline=$((SECONDS + READY_TIMEOUT))
until curl --fail --silent "http://127.0.0.1:$PORT/health" >/dev/null; do
  if ! kill -0 "$SERVER_PID" 2>/dev/null || (( SECONDS >= deadline )); then
    tail -n 120 "$SERVER_LOG" >&2 || true
    exit 1
  fi
  sleep 1
done

run_client() {
  local phase="$1"
  local prompts="$2"
  "$PYTHON" "$REPO/scripts/run_open_loop_client.py" \
    --base-url "http://127.0.0.1:$PORT" --model "$MODEL_NAME" \
    --prompts "$prompts" --output "$ARM_DIR/client-$phase.jsonl" \
    --qps "$QPS" --max-tokens "$OUTPUT_TOKENS" --phase "$phase" \
    >"$ARM_DIR/client-$phase-summary.json"
}

reset_primary() {
  curl --fail --silent --show-error --request POST \
    "http://127.0.0.1:$PORT/reset_prefix_cache?reset_external=true" >/dev/null
}

start_window() {
  curl --fail --silent --show-error --request POST \
    "http://127.0.0.1:$PORT/tiering_monitor/start_window?window_id=$1" >/dev/null
}

end_window() {
  curl --fail --silent --show-error --request POST \
    "http://127.0.0.1:$PORT/tiering_monitor/end_window" >"$1"
}

# 首先把 A、B 都写入 secondary；reset 会同步 drain 所有 pending I/O。
run_client seed-a "$PROMPTS_A"
run_client seed-b "$PROMPTS_B"
reset_primary

# A 只负责让 load 路径进入稳定状态，并单独保留监控证据。
start_window "$ARM_ID-conditioning-a-qps-$QPS"
run_client conditioning-a "$PROMPTS_A"
end_window "$ARM_DIR/server-conditioning-window-summary.json"
reset_primary

# B 在此之前从未被读取；正式窗口仍是首次数据读取。
start_window "$ARM_ID-measurement-b-qps-$QPS"
run_client measurement-b "$PROMPTS_B"
end_window "$ARM_DIR/server-measurement-window-summary.json"

printf 'backend=uring_slab\nqps=%s\narm_id=%s\nconditioning=A\nmeasurement=B\n' \
  "$QPS" "$ARM_ID" >"$ARM_DIR/configuration.txt"

kill "$SERVER_PID"
wait "$SERVER_PID" 2>/dev/null || true
SERVER_PID=""
rm -f -- "$SLAB_PATH"
echo "$ARM_DIR"
