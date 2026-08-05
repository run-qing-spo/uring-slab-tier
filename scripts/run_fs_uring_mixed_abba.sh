#!/usr/bin/env bash

set -Eeuo pipefail

# 50:50稳态混合负载：8个紧邻配对、16个独立server，按ABBA换序。

TOTAL_QPS="${TOTAL_QPS:-100}"
PAIR_COUNT="${PAIR_COUNT:-8}"
REPO="${REPO:-/home/adminz/uring-slab-experiments/repos/uring-slab-tier}"
ARM_RUNNER="${ARM_RUNNER:-$REPO/scripts/run_tier_mixed_qps_arm.sh}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
RESULTS_ROOT="${RESULTS_ROOT:-/home/adminz/uring-slab-experiments/results/fs-uring-mixed-abba-$RUN_ID}"
ORDER_FILE="$RESULTS_ROOT/execution-order.tsv"

[[ -x "$ARM_RUNNER" ]] || { echo "找不到单臂脚本：$ARM_RUNNER" >&2; exit 1; }
mkdir -p "$RESULTS_ROOT"
printf 'sequence\tpair\tposition\tbackend\tarm_id\tstatus\n' >"$ORDER_FILE"

sequence=0
for pair in $(seq 1 "$PAIR_COUNT"); do
  if (( pair % 2 == 1 )); then
    backends=(fs uring_slab)
  else
    backends=(uring_slab fs)
  fi
  position=0
  for backend in "${backends[@]}"; do
    sequence=$((sequence + 1))
    position=$((position + 1))
    arm_id=$(printf 's%02d-p%02d' "$sequence" "$pair")
    printf '%s\t%s\t%s\t%s\t%s\tstarted\n' \
      "$sequence" "$pair" "$position" "$backend" "$arm_id" >>"$ORDER_FILE"
    "$ARM_RUNNER" "$backend" "$TOTAL_QPS" "$arm_id" "$RESULTS_ROOT"
    printf '%s\t%s\t%s\t%s\t%s\tcompleted\n' \
      "$sequence" "$pair" "$position" "$backend" "$arm_id" >>"$ORDER_FILE"
  done
done

printf 'run_id=%s\ntotal_qps=%s\nread_qps=50\nwrite_qps=50\npairs=%s\narms=%s\norder=ABBA\nworkload=steady_mixed_50_50\n' \
  "$RUN_ID" "$TOTAL_QPS" "$PAIR_COUNT" "$((PAIR_COUNT * 2))" \
  >"$RESULTS_ROOT/experiment.txt"
echo "混合负载$((PAIR_COUNT * 2))个arm全部完成：$RESULTS_ROOT"
