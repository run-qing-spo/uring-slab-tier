#!/usr/bin/env bash

set -Eeuo pipefail

# 16 个独立 server、8 个紧邻配对。
# A=fs，B=uring_slab；相邻配对交替采用 AB、BA，形成 ABBA 顺序。

QPS="${QPS:-100}"
PAIR_COUNT="${PAIR_COUNT:-8}"
REPO="${REPO:-/home/adminz/uring-slab-experiments/repos/uring-slab-tier}"
ARM_RUNNER="${ARM_RUNNER:-$REPO/scripts/run_tier_qps_arm.sh}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
RESULTS_ROOT="${RESULTS_ROOT:-/home/adminz/uring-slab-experiments/results/fs-uring-abba-$RUN_ID}"
ORDER_FILE="$RESULTS_ROOT/execution-order.tsv"

[[ -x "$ARM_RUNNER" ]] || {
  echo "找不到单臂实验脚本：$ARM_RUNNER" >&2
  exit 1
}
[[ "$PAIR_COUNT" -eq 8 ]] || {
  echo "正式设计固定为 8 个配对、16 个独立 server" >&2
  exit 1
}

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
      "$sequence" "$pair" "$position" "$backend" "$arm_id" \
      >>"$ORDER_FILE"

    # 单臂脚本内部启动全新 server，并完成：
    # seed/store → reset → 重复读取（丢弃）→ reset → 正式重复读取。
    "$ARM_RUNNER" "$backend" "$QPS" "$arm_id" "$RESULTS_ROOT"

    printf '%s\t%s\t%s\t%s\t%s\tcompleted\n' \
      "$sequence" "$pair" "$position" "$backend" "$arm_id" \
      >>"$ORDER_FILE"
  done
done

printf 'run_id=%s\nqps=%s\npairs=%s\narms=%s\norder=ABBA\nA=fs\nB=uring_slab\n' \
  "$RUN_ID" "$QPS" "$PAIR_COUNT" "$((PAIR_COUNT * 2))" \
  >"$RESULTS_ROOT/experiment.txt"

echo "16 个实验臂全部完成：$RESULTS_ROOT"
