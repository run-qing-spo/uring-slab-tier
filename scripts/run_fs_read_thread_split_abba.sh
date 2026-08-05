#!/usr/bin/env bash

set -Eeuo pipefail

# 诊断FS纯读线程池效应：默认16+16与仅16个read-priority线程紧邻配对。

QPS="${QPS:-100}"
PAIR_COUNT="${PAIR_COUNT:-8}"
REPO="${REPO:-/home/adminz/uring-slab-experiments/repos/uring-slab-tier}"
ARM_RUNNER="${ARM_RUNNER:-$REPO/scripts/run_tier_qps_arm.sh}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
RESULTS_ROOT="${RESULTS_ROOT:-/home/adminz/uring-slab-experiments/results/fs-read-thread-split-$RUN_ID}"
ORDER_FILE="$RESULTS_ROOT/execution-order.tsv"

[[ -x "$ARM_RUNNER" ]] || { echo "找不到单臂脚本：$ARM_RUNNER" >&2; exit 1; }
mkdir -p "$RESULTS_ROOT"
printf 'sequence\tpair\tposition\tmode\tarm_id\tstatus\n' >"$ORDER_FILE"

sequence=0
for pair in $(seq 1 "$PAIR_COUNT"); do
  if (( pair % 2 == 1 )); then modes=(r16w16 r16w0); else modes=(r16w0 r16w16); fi
  position=0
  for mode in "${modes[@]}"; do
    sequence=$((sequence + 1)); position=$((position + 1))
    arm_id=$(printf 's%02d-p%02d-%s' "$sequence" "$pair" "$mode")
    printf '%s\t%s\t%s\t%s\t%s\tstarted\n' \
      "$sequence" "$pair" "$position" "$mode" "$arm_id" >>"$ORDER_FILE"
    if [[ "$mode" == "r16w16" ]]; then
      FS_READ_THREADS=16 FS_WRITE_THREADS=16 \
        "$ARM_RUNNER" fs "$QPS" "$arm_id" "$RESULTS_ROOT"
    else
      FS_READ_THREADS=16 FS_WRITE_THREADS=0 \
        "$ARM_RUNNER" fs "$QPS" "$arm_id" "$RESULTS_ROOT"
    fi
    printf '%s\t%s\t%s\t%s\t%s\tcompleted\n' \
      "$sequence" "$pair" "$position" "$mode" "$arm_id" >>"$ORDER_FILE"
  done
done

printf 'run_id=%s\nqps=%s\npairs=%s\narms=%s\norder=ABBA\nA=r16w16\nB=r16w0\nworkload=steady_pure_read_fs_thread_diagnostic\n' \
  "$RUN_ID" "$QPS" "$PAIR_COUNT" "$((PAIR_COUNT * 2))" >"$RESULTS_ROOT/experiment.txt"
echo "FS纯读线程诊断完成：$RESULTS_ROOT"
