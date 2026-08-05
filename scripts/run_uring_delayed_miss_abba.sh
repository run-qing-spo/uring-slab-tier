#!/usr/bin/env bash

set -Eeuo pipefail

QPS="${QPS:-100}"
PAIR_COUNT="${PAIR_COUNT:-8}"
REPO="${REPO:-/home/adminz/uring-slab-experiments/repos/uring-slab-tier}"
ARM_RUNNER="${ARM_RUNNER:-$REPO/scripts/run_tier_write_qps_arm.sh}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
RESULTS_ROOT="${RESULTS_ROOT:-/home/adminz/uring-slab-experiments/results/uring-delayed-miss-abba-$RUN_ID}"
ORDER_FILE="$RESULTS_ROOT/execution-order.tsv"

mkdir -p "$RESULTS_ROOT"
printf 'sequence\tpair\tposition\tbackend\tarm_id\tstatus\n' >"$ORDER_FILE"

sequence=0
for pair in $(seq 1 "$PAIR_COUNT"); do
  if (( pair % 2 == 1 )); then
    backends=(uring_slab uring_slab_delayed_miss)
  else
    backends=(uring_slab_delayed_miss uring_slab)
  fi
  position=0
  for backend in "${backends[@]}"; do
    sequence=$((sequence + 1))
    position=$((position + 1))
    arm_id=$(printf 's%02d-p%02d' "$sequence" "$pair")
    printf '%s\t%s\t%s\t%s\t%s\tstarted\n' \
      "$sequence" "$pair" "$position" "$backend" "$arm_id" >>"$ORDER_FILE"
    bash "$ARM_RUNNER" "$backend" "$QPS" "$arm_id" "$RESULTS_ROOT"
    printf '%s\t%s\t%s\t%s\t%s\tcompleted\n' \
      "$sequence" "$pair" "$position" "$backend" "$arm_id" >>"$ORDER_FILE"
  done
done

printf 'run_id=%s\nqps=%s\npairs=%s\narms=%s\norder=ABBA\nworkload=steady_pure_write\n' \
  "$RUN_ID" "$QPS" "$PAIR_COUNT" "$((PAIR_COUNT * 2))" >"$RESULTS_ROOT/experiment.txt"
echo "delayed-miss ABBA完成：$RESULTS_ROOT"
