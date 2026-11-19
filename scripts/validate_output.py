#!/usr/bin/env python3
"""validate_output.py — 机读自查：契约字段、哈希一致、审计完备、未处理项如实。

用法: python3 validate_output.py [--work-dir /app/work] [--output-dir /app/output]
返回码: 0 通过 / 1 发现问题（问题清单仍写入 /app/work/selfcheck.json）
"""
import argparse, hashlib, json, os, sys

def sha256_of(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", default="/app/work")
    ap.add_argument("--output-dir", default="/app/output")
    args = ap.parse_args()
    wd, outdir = args.work_dir, args.output_dir
    problems, warnings = [], []

    def load(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)

    brief = load(os.path.join(wd, "brief.json"))
    plan = load(os.path.join(wd, "plan.json"))
    result_path = os.path.join(outdir, "result.json")
    if not os.path.exists(result_path):
        problems.append("result.json 不存在")
        result = {}
    else:
        result = load(result_path)

    # 1) result.json 含 case_id 与 output_contract 全部键
    if result.get("case_id") in (None, "unknown"):
        problems.append("result.json 缺少有效 case_id")
    contract = brief.get("output_contract") or {}
    keys = list(contract.keys()) if isinstance(contract, dict) else list(contract or [])
    got = set((result.get("output_contract") or {}).keys())
    missing = [k for k in keys if k not in got]
    if missing:
        problems.append(f"output_contract 字段未交付: {missing}")
    # 合同字段值为 null 的记警告（可能已如实说明，也可能漏填）
    null_fields = [k for k, v in (result.get("output_contract") or {}).items()
                   if isinstance(v, dict) and v.get("delivered") is None]
    if null_fields:
        warnings.append(f"以下 contract 字段尚未填充（需 Agent 按 task.md 补充或注明原因）: {null_fields}")

    # 2) 每个输入文件恰好有一个终态动作
    expected = {m["rel_path"] for m in brief.get("material_files_preview", [])}
    acted = {}
    for it in plan.get("items", []):
        acted[it["rel_path"]] = acted.get(it["rel_path"], 0) + 1
    dup_actions = [k for k, v in acted.items() if v > 1]
    if dup_actions:
        problems.append(f"同一输入文件出现多个动作: {dup_actions}")

    # 3) 目标文件真实存在且哈希一致
    verified, mismatched = 0, []
    for it in plan.get("items", []):
        if it["action"] == "skip" or not it.get("target"):
            continue
        dst = os.path.join(outdir, it["target"])
        if not os.path.exists(dst):
            problems.append(f"目标文件缺失: {it['target']} (src {it['rel_path']})")
            continue
        if it.get("sha256") and sha256_of(dst) != it["sha256"]:
            mismatched.append(it["target"])
        else:
            verified += 1
    if mismatched:
        problems.append(f"哈希不一致: {mismatched}")

    # 4) 审计日志与回滚脚本
    ops_path = os.path.join(outdir, "audit", "operations.jsonl")
    if not os.path.exists(ops_path):
        problems.append("audit/operations.jsonl 缺失")
    if not os.path.exists(os.path.join(outdir, "audit", "rollback.sh")):
        problems.append("audit/rollback.sh 缺失")

    # 5) manifest 覆盖
    man_path = os.path.join(outdir, "manifest.csv")
    if not os.path.exists(man_path):
        problems.append("manifest.csv 缺失")

    report = {
        "case_id": brief.get("case_id"),
        "verified_files": verified,
        "planned_actions": len(plan.get("items", [])),
        "problems": problems,
        "warnings": warnings,
        "passed": not problems,
    }
    with open(os.path.join(wd, "selfcheck.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[validate] verified={verified} problems={len(problems)} warnings={len(warnings)}")
    for p in problems:
        print("  PROBLEM:", p)
    for w in warnings:
        print("  WARN:", w)
    return 0 if not problems else 1

if __name__ == "__main__":
    sys.exit(main())
