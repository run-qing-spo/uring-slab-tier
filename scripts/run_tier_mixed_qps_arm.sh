#!/usr/bin/env bash

set -Eeuo pipefail

# 单个50:50稳态混合臂：读固定secondary数据，同时写入从未出现的新数据。

BACKEND="${1:?用法: run_tier_mixed_qps_arm.sh <fs|uring_slab> <total_qps> <arm_id> [results_root]}"
TOTAL_QPS="${2:?缺少total_qps}"
ARM_ID="${3:?缺少arm_id}"
RESULTS_ROOT="${4:-/home/adminz/uring-slab-experiments/results/mixed-qps-$(date -u +%Y%m%dT%H%M%SZ)}"

case "$BACKEND" in
  fs|uring_slab) ;;
  *) echo "backend必须是fs或uring_slab" >&2; exit 2 ;;
esac
[[ "$TOTAL_QPS" == "100" || "$TOTAL_QPS" == "100.0" ]] || {
  echo "当前50:50实验固定要求total_qps=100" >&2; exit 2;
}

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
READ_QPS=50
WRITE_QPS=50
WRITE_OFFSET_SECONDS=0.01

ARM_DIR="$RESULTS_ROOT/qps-$TOTAL_QPS/$ARM_ID-$BACKEND"
PROMPTS_READ="$RESULTS_ROOT/prompts-read.jsonl"
PROMPTS_STORE_CONDITIONING="$RESULTS_ROOT/prompts-store-conditioning.jsonl"
PROMPTS_WRITE="$RESULTS_ROOT/prompts-write.jsonl"
SERVER_LOG="$ARM_DIR/server.log"
FS_ROOT="$ARM_DIR/fs-data"
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
  [[ -f "$output" ]] && return 0
  env HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONHASHSEED=0 \
    "$PYTHON" "$REPO/scripts/generate_none_prompts.py" \
      --model-snapshot "$MODEL" --output "$output" \
      --count "$PROMPT_COUNT" --start-index "$start_index" \
      --tokens "$PROMPT_TOKENS" --block-size "$BLOCK_SIZE"
}

generate_prompts "$PROMPTS_READ" 0
generate_prompts "$PROMPTS_STORE_CONDITIONING" "$PROMPT_COUNT"
generate_prompts "$PROMPTS_WRITE" "$((PROMPT_COUNT * 2))"

if [[ "$BACKEND" == "fs" ]]; then
  mkdir -p "$FS_ROOT"
  EXTRA_CONFIG=$(printf '%s' \
    '{"spec_name":"TieringOffloadingSpec","cpu_bytes_to_use":'"$PRIMARY_BYTES"',"block_size":'"$BLOCK_SIZE"',"eviction_policy":"lru","offload_prompt_only":true,"secondary_tiers":[{"type":"fs","root_dir":"'"$FS_ROOT"'","n_read_threads":16,"n_write_threads":16}]}')
else
  EXTRA_CONFIG=$(printf '%s' \
    '{"spec_name":"UringSlabOffloadingSpec","spec_module_path":"uring_slab_tier.vllm_spec","cpu_bytes_to_use":'"$PRIMARY_BYTES"',"block_size":'"$BLOCK_SIZE"',"eviction_policy":"lru","offload_prompt_only":true,"secondary_tiers":[{"type":"uring_slab","disk_bytes_to_use":'"$DISK_BYTES"',"slab_path":"'"$SLAB_PATH"'","total_qd":128,"pending_capacity":4096}]}')
fi
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

run_single_client() {
  local phase="$1"
  local prompts="$2"
  "$PYTHON" "$REPO/scripts/run_open_loop_client.py" \
    --base-url "http://127.0.0.1:$PORT" --model "$MODEL_NAME" \
    --prompts "$prompts" --output "$ARM_DIR/client-$phase.jsonl" \
    --qps "$TOTAL_QPS" --max-tokens "$OUTPUT_TOKENS" --phase "$phase" \
    >"$ARM_DIR/client-$phase-summary.json"
}

reset_and_drain() {
  curl --fail --silent --show-error --request POST \
    "http://127.0.0.1:$PORT/reset_prefix_cache?reset_external=true" >/dev/null
}

# R先落入secondary，再完成一次不计入结果的read；S单独预热store路径。
run_single_client seed-store-read-set "$PROMPTS_READ"
reset_and_drain
run_single_client conditioning-read "$PROMPTS_READ"
run_single_client conditioning-store "$PROMPTS_STORE_CONDITIONING"
reset_and_drain

WINDOW_ID="$ARM_ID-$BACKEND-mixed-qps-$TOTAL_QPS"
curl --fail --silent --show-error --request POST \
  "http://127.0.0.1:$PORT/tiering_monitor/start_window?window_id=$WINDOW_ID" >/dev/null
"$PYTHON" "$REPO/scripts/run_mixed_open_loop_client.py" \
  --base-url "http://127.0.0.1:$PORT" --model "$MODEL_NAME" \
  --read-prompts "$PROMPTS_READ" --write-prompts "$PROMPTS_WRITE" \
  --read-output "$ARM_DIR/client-measurement-read.jsonl" \
  --write-output "$ARM_DIR/client-measurement-write.jsonl" \
  --read-qps "$READ_QPS" --write-qps "$WRITE_QPS" \
  --write-offset-seconds "$WRITE_OFFSET_SECONDS" --max-tokens "$OUTPUT_TOKENS" \
  >"$ARM_DIR/client-measurement-mixed-summary.json"
# 等两个客户端完成仍不代表异步store完成；正式窗口内同步drain。
reset_and_drain
curl --fail --silent --show-error --request POST \
  "http://127.0.0.1:$PORT/tiering_monitor/end_window" \
  >"$ARM_DIR/server-window-summary.json"

printf 'backend=%s\ntotal_qps=%s\nread_qps=%s\nwrite_qps=%s\nwrite_offset_seconds=%s\narm_id=%s\nworkload=steady_mixed_50_50\n' \
  "$BACKEND" "$TOTAL_QPS" "$READ_QPS" "$WRITE_QPS" "$WRITE_OFFSET_SECONDS" \
  "$ARM_ID" >"$ARM_DIR/configuration.txt"

kill "$SERVER_PID"
wait "$SERVER_PID" 2>/dev/null || true
SERVER_PID=""
[[ "$BACKEND" != "uring_slab" ]] || rm -f -- "$SLAB_PATH"
echo "$ARM_DIR"
