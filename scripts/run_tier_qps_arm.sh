#!/usr/bin/env bash

set -Eeuo pipefail

# 运行一个独立的 FS 或 uring-slab 固定 QPS 实验臂。
# 每个实验臂都启动全新 server，并在同一进程内完成预热、清理和正式测试。

BACKEND="${1:?用法: run_tier_qps_arm.sh <fs|uring_slab> <qps> <arm_id> [results_root]}"
QPS="${2:?缺少 qps}"
ARM_ID="${3:?缺少 arm_id}"
RESULTS_ROOT="${4:-/home/adminz/uring-slab-experiments/results/clean-qps-$(date -u +%Y%m%dT%H%M%SZ)}"

case "$BACKEND" in
  fs|uring_slab) ;;
  *) echo "backend 必须是 fs 或 uring_slab" >&2; exit 2 ;;
esac

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
FS_READ_THREADS="${FS_READ_THREADS:-16}"
FS_WRITE_THREADS="${FS_WRITE_THREADS:-16}"
MONITOR_MODE="${VLLM_TIERING_MONITOR_MODE:-memory}"

ARM_DIR="$RESULTS_ROOT/qps-$QPS/$ARM_ID-$BACKEND"
PROMPTS_PATH="$RESULTS_ROOT/prompts.jsonl"
SERVER_LOG="$ARM_DIR/server.log"
FS_ROOT="$ARM_DIR/fs-data"
SLAB_PATH="$ARM_DIR/uring-slab.bin"
MONITOR_PATH="${VLLM_TIERING_MONITOR_PATH:-$ARM_DIR/tiering-monitor.jsonl}"
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

if [[ ! -f "$PROMPTS_PATH" ]]; then
  mkdir -p "$RESULTS_ROOT"
  env HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONHASHSEED=0 \
    "$PYTHON" "$REPO/scripts/generate_none_prompts.py" \
      --model-snapshot "$MODEL" --output "$PROMPTS_PATH" \
      --count "$PROMPT_COUNT" --tokens "$PROMPT_TOKENS" \
      --block-size "$BLOCK_SIZE"
fi

if [[ "$BACKEND" == "fs" ]]; then
  mkdir -p "$FS_ROOT"
  EXTRA_CONFIG=$(printf '%s' \
    '{"spec_name":"TieringOffloadingSpec","cpu_bytes_to_use":'"$PRIMARY_BYTES"',"block_size":'"$BLOCK_SIZE"',"eviction_policy":"lru","offload_prompt_only":true,"secondary_tiers":[{"type":"fs","root_dir":"'"$FS_ROOT"'","n_read_threads":'"$FS_READ_THREADS"',"n_write_threads":'"$FS_WRITE_THREADS"'}]}')
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
    VLLM_SERVER_DEV_MODE=1 VLLM_TIERING_MONITOR_MODE="$MONITOR_MODE" \
    VLLM_TIERING_MONITOR_PATH="$MONITOR_PATH" \
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
  "$PYTHON" "$REPO/scripts/run_open_loop_client.py" \
    --base-url "http://127.0.0.1:$PORT" --model "$MODEL_NAME" \
    --prompts "$PROMPTS_PATH" --output "$ARM_DIR/client-$phase.jsonl" \
    --qps "$QPS" --max-tokens "$OUTPUT_TOKENS" --phase "$phase" \
    >"$ARM_DIR/client-$phase-summary.json"
}

run_client warmup
curl --fail --silent --show-error --request POST \
  "http://127.0.0.1:$PORT/reset_prefix_cache?reset_external=true" >/dev/null

# 升频预热：measurement 前先跑一段丢弃的负载，把 4090 的 GPU 时钟顶到高频，
# 然后再清一次缓存。目的是消除 reset→measurement 空档里 GPU 掉回低频 P-state
# 造成的冷时钟瞬态（曾在 u08/u16 的 measurement 开头观察到 ~1.28x 的高台，
# 持续约 48/72 个请求后才落回基线）。第二次 reset 很快，时钟来不及掉。
run_client reclock
curl --fail --silent --show-error --request POST \
  "http://127.0.0.1:$PORT/reset_prefix_cache?reset_external=true" >/dev/null

WINDOW_ID="${ARM_ID}-${BACKEND}-qps-${QPS}"
curl --fail --silent --show-error --request POST \
  "http://127.0.0.1:$PORT/tiering_monitor/start_window?window_id=$WINDOW_ID" \
  >/dev/null
run_client measurement
curl --fail --silent --show-error --request POST \
  "http://127.0.0.1:$PORT/tiering_monitor/end_window" \
  >"$ARM_DIR/server-window-summary.json"

printf 'backend=%s\nqps=%s\narm_id=%s\nfs_read_threads=%s\nfs_write_threads=%s\n' \
  "$BACKEND" "$QPS" "$ARM_ID" "$FS_READ_THREADS" "$FS_WRITE_THREADS" \
  >"$ARM_DIR/configuration.txt"

kill "$SERVER_PID"
wait "$SERVER_PID" 2>/dev/null || true
SERVER_PID=""
# slab 内容不是实验结果；每臂汇总完成后释放预分配空间。
if [[ "$BACKEND" == "uring_slab" ]]; then
  rm -f -- "$SLAB_PATH"
fi
echo "$ARM_DIR"
