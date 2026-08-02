#!/usr/bin/env bash

PYTHONHASHSEED=0 \
PYTHONPATH=/home/adminz/uring-slab-experiments/repos/vllm-0.24.0:/home/adminz/uring-slab-experiments/repos/uring-slab-tier \
VLLM_TIERING_MONITOR_MODE=async_jsonl \
VLLM_TIERING_MONITOR_PATH="${VLLM_TIERING_MONITOR_PATH:-/tmp/vllm-tiering-slab}" \
/home/adminz/uring-slab-experiments/venvs/vllm024-cu129-clean/bin/vllm serve \
  "${MODEL:-facebook/opt-125m}" \
  --port "${PORT:-8000}" \
  --served-model-name "${SERVED_MODEL_NAME:-benchmark-model}" \
  --max-model-len 2048 \
  --gpu-memory-utilization 0.90 \
  --dtype float16 \
  --block-size 16 \
  --enable-prefix-caching \
  --max-num-seqs 256 \
  --max-num-batched-tokens 2048 \
  --tensor-parallel-size 1 \
  --pipeline-parallel-size 1 \
  --kv-transfer-config '{
    "kv_connector": "OffloadingConnector",
    "kv_role": "kv_both",
    "kv_load_failure_policy": "fail",
    "kv_connector_extra_config": {
      "spec_name": "UringSlabOffloadingSpec",
      "spec_module_path": "uring_slab_tier.vllm_spec",
      "cpu_bytes_to_use": 67108864,
      "block_size": 16,
      "eviction_policy": "lru",
      "offload_prompt_only": true,
      "secondary_tiers": [{
        "type": "uring_slab",
        "disk_bytes_to_use": 137438953472,
        "slab_path": "/tmp/uring-slab-benchmark.slab",
        "total_qd": 128,
        "pending_capacity": 4096
      }]
    }
  }'
