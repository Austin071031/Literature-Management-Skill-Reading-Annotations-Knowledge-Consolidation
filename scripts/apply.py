#!/usr/bin/env python3
"""apply.py — 执行整理计划：复制（非剪切）到 /app/output，全程审计日志，可回滚。

设计原则:
1. 源文件（/input）绝不修改；输出写入 /app/output/<library_root>。
2. 幂等：目标已存在且 sha256 相同 → 记 already_applied，不重复操作。
3. 冲突不覆盖：目标存在但内容不同 → 追加 sha 短后缀改名，记 conflict。
4. 每个操作写 audit/operations.jsonl；rollback.sh 可一键撤销全部复制。
5. 单文件失败不中断；失败进入 unresolved。

用法: python3 apply.py [--work-dir /app/work] [--output-dir /app/output]
"""
import argparse, datetime, hashlib, json, os, shutil, sys

def sha256_of(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def log_op(f, rec):
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    f.flush()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", default="/app/work")
    ap.add_argument("--output-dir", default="/app/output")
    args = ap.parse_args()

    wd, outdir = args.work_dir, args.output_dir
    with open(os.path.join(wd, "plan.json"), "r", encoding="utf-8") as f:
        plan = json.load(f)
    os.makedirs(outdir, exist_ok=True)
    audit_dir = os.path.join(outdir, "audit")
    os.makedirs(audit_dir, exist_ok=True)

    ops_path = os.path.join(audit_dir, "operations.jsonl")
    rollback_ops = []
    stats = {"placed": 0, "archived": 0, "already_applied": 0,
             "conflict_renamed": 0, "skipped": 0, "failed": 0}
    unresolved, conflicts = [], []
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with open(ops_path, "a", encoding="utf-8") as ops:
        log_op(ops, {"ts": now, "action": "run_start", "case_id": plan.get("case_id")})
        for item in plan["items"]:
            rec = {
                "ts": now, "src": item["rel_path"], "dst": item.get("target"),
                "action": item["action"], "sha256": item.get("sha256"),
                "status": None, "note": item.get("reason"),
            }
            src_abs = item["abs_path"]
            if item["action"] == "skip" or not item.get("target"):
                rec["status"] = "skipped"
                stats["skipped"] += 1
                log_op(ops, rec)
                continue
            dst_abs = os.path.join(outdir, item["target"])
            os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
            try:
                if os.path.exists(dst_abs):
                    same = (sha256_of(dst_abs) == item.get("sha256"))
                    if same:
                        rec["status"] = "already_applied"
                        stats["already_applied"] += 1
                        log_op(ops, rec)
                        continue
                    # 冲突：不覆盖，改唯一名
                    base, ext = os.path.splitext(dst_abs)
                    short = (item.get("sha256") or "x")[:8]
                    cand = f"{base}__{short}{ext}"
                    i = 2
                    while os.path.exists(cand):
                        cand = f"{base}__{short}_{i}{ext}"
                        i += 1
                    conflicts.append({
                        "original_target": item["target"],
                        "resolved_target": os.path.relpath(cand, outdir),
                        "reason": "目标已存在且内容不同（同名不同版本），未覆盖",
                    })
                    item["target"] = os.path.relpath(cand, outdir)
                    dst_abs = cand
                    rec["dst"] = item["target"]
                    rec["status"] = "conflict_renamed"
                    stats["conflict_renamed"] += 1
                shutil.copy2(src_abs, dst_abs)
                rollback_ops.append(os.path.relpath(dst_abs, outdir))
                if rec["status"] is None:
                    rec["status"] = "done"
                    if item["action"] == "place":
                        stats["placed"] += 1
                    else:
                        stats["archived"] += 1
                else:
                    stats["placed"] += 1  # conflict_renamed 的文件也已落地
            except OSError as e:
                rec["status"] = "failed"
                rec["note"] = f"{item.get('reason')} | 复制失败: {e}"
                stats["failed"] += 1
                unresolved.append({"rel_path": item["rel_path"], "reason": str(e)})
            log_op(ops, rec)
        log_op(ops, {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                     "action": "run_end", "stats": stats})

    # 回滚脚本：从 operations.jsonl 全历史生成，覆盖历次运行的全部落盘文件
    rb_targets = []
    try:
        with open(ops_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    op = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if op.get("dst") and op.get("status") in ("done", "conflict_renamed", "already_applied"):
                    if op["dst"] not in rb_targets and not op["dst"].startswith("audit/"):
                        rb_targets.append(op["dst"])
    except OSError:
        rb_targets = rollback_ops
    rb_path = os.path.join(audit_dir, "rollback.sh")
    with open(rb_path, "w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env bash\n# 自动生成的回滚脚本：撤销 apply.py 的全部复制操作。\n")
        f.write("set -euo pipefail\nOUTPUT_DIR=\"${1:-%s}\"\n" % outdir)
        for rel in rb_targets:
            f.write("rm -f -- \"$OUTPUT_DIR/%s\"\n" % rel.replace('"', '\\"'))
        f.write("find \"$OUTPUT_DIR\" -type d -empty -delete || true\n")
    os.chmod(rb_path, 0o755)

    report = {"stats": stats, "conflicts": conflicts, "unresolved": unresolved,
              "operations_log": os.path.relpath(ops_path, outdir),
              "rollback_script": os.path.relpath(rb_path, outdir),
              "items": plan["items"]}
    with open(os.path.join(wd, "apply_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[apply] placed={stats['placed']} archived={stats['archived']} "
          f"already={stats['already_applied']} conflict_renamed={stats['conflict_renamed']} "
          f"skipped={stats['skipped']} failed={stats['failed']}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
