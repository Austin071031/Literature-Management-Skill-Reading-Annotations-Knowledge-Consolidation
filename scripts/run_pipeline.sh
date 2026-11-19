#!/usr/bin/env bash
# run_pipeline.sh — 一键执行整理流水线（含失败保底：任何一步失败仍生成 partial 交付）。
# 用法: bash /skills/weekly-skill/scripts/run_pipeline.sh [INPUT_DIR=/input] [WORK_DIR=/app/work] [OUTPUT_DIR=/app/output] [PLAN_CONFIG=]
set -uo pipefail

INPUT_DIR="${1:-/input}"
WORK_DIR="${2:-/app/work}"
OUTPUT_DIR="${3:-/app/output}"
PLAN_CONFIG="${4:-}"
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$WORK_DIR/run_log.md"

mkdir -p "$WORK_DIR" "$OUTPUT_DIR"
{
  echo "# 运行日志 $(date -u +%FT%TZ)"
  echo "- input=$INPUT_DIR work=$WORK_DIR output=$OUTPUT_DIR"
} > "$LOG"

step() {
  local name="$1"; shift
  echo "## $name $(date -u +%H:%M:%S)" >> "$LOG"
  if "$@" >> "$LOG" 2>&1; then
    echo "  ✔ $name" ; echo "  结果: 成功" >> "$LOG"
    return 0
  else
    echo "  ✘ $name (详见 $LOG)" ; echo "  结果: 失败，进入保底分支" >> "$LOG"
    return 1
  fi
}

FAILED=""
step inspect_case python3 "$SKILL_DIR/scripts/inspect_case.py" --input-dir "$INPUT_DIR" --work-dir "$WORK_DIR" || FAILED="inspect"
step inventory    python3 "$SKILL_DIR/scripts/inventory.py"    --work-dir "$WORK_DIR" || FAILED="inventory"
step extract_refs python3 "$SKILL_DIR/scripts/extract_refs.py" --work-dir "$WORK_DIR" || FAILED="extract"
if [ -n "$PLAN_CONFIG" ] && [ -f "$PLAN_CONFIG" ]; then
  step plan       python3 "$SKILL_DIR/scripts/plan.py"         --work-dir "$WORK_DIR" --config "$PLAN_CONFIG" || FAILED="plan"
else
  step plan       python3 "$SKILL_DIR/scripts/plan.py"         --work-dir "$WORK_DIR" || FAILED="plan"
fi
step apply        python3 "$SKILL_DIR/scripts/apply.py"        --work-dir "$WORK_DIR" --output-dir "$OUTPUT_DIR" || FAILED="apply"

if [ -z "$FAILED" ]; then
  step report     python3 "$SKILL_DIR/scripts/report.py"       --work-dir "$WORK_DIR" --output-dir "$OUTPUT_DIR" || true
else
  # 保底：即使中途失败也生成 partial 结果并如实注明
  step report     python3 "$SKILL_DIR/scripts/report.py"       --work-dir "$WORK_DIR" --output-dir "$OUTPUT_DIR" --partial-reason "pipeline step failed: $FAILED" || true
fi
step validate     python3 "$SKILL_DIR/scripts/validate_output.py" --work-dir "$WORK_DIR" --output-dir "$OUTPUT_DIR" || true

echo "日志: $LOG"
echo "结果: $OUTPUT_DIR/result.json"
[ -f "$OUTPUT_DIR/result.json" ] && exit 0 || exit 1
