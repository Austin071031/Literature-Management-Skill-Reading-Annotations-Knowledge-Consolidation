#!/usr/bin/env python3
"""report.py — 汇总交付：result.json（含 output_contract 全字段 + case_id）、清单、README。

用法: python3 report.py [--work-dir /app/work] [--output-dir /app/output] [--partial-reason TEXT]
"""
import argparse, csv, datetime, json, os, sys

def load(wd, name, default=None):
    p = os.path.join(wd, name)
    if not os.path.exists(p):
        return default
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def tree_lines(root, max_lines=200):
    lines = []
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for fn in sorted(files):
            rel = os.path.relpath(os.path.join(base, fn), root)
            lines.append(rel)
            if len(lines) >= max_lines:
                lines.append("... (truncated)")
                return lines
    return lines

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", default="/app/work")
    ap.add_argument("--output-dir", default="/app/output")
    ap.add_argument("--partial-reason", default=None)
    args = ap.parse_args()
    wd, outdir = args.work_dir, args.output_dir
    os.makedirs(outdir, exist_ok=True)

    brief = load(wd, "brief.json", {}) or {}
    inv = load(wd, "inventory.json", {}) or {}
    refs = load(wd, "refs.json", {}) or {}
    plan = load(wd, "plan.json", {}) or {}
    arep = load(wd, "apply_report.json", {}) or {}

    case_id = brief.get("case_id") or plan.get("case_id") or "unknown"
    items = arep.get("items") or plan.get("items") or []
    stats = arep.get("stats") or {}
    conflicts = arep.get("conflicts", [])
    unresolved = arep.get("unresolved", [])

    # —— 六类要素逐一交代 ——
    elements = {
        "file_types": (inv.get("summary") or {}).get("by_sniff_type", {}),
        "metadata_fields": {
            "fields": ["title", "authors", "year", "venue", "doi", "arxiv", "keywords"],
            "policy": "仅提取原文/侧车真实存在的字段；缺失字段为 null 并注明 not_found，不补造",
            "coverage": {k: sum(1 for r in refs.values() if r.get(k, {}).get("value"))
                         for k in ("title", "authors", "year", "venue", "doi", "arxiv")},
            "total": len(refs),
        },
        "directory_levels": "library/<doc_type>/<year>/（受 plan.config.directory_scheme 控制，可被 task.md 规则覆盖）",
        "duplicates": plan.get("exact_duplicates", []),
        "version_relations": plan.get("version_groups", []),
        "permissions_and_sources": {
            "unreadable": (inv.get("summary") or {}).get("unreadable_or_broken", []),
            "encrypted_pdfs": (inv.get("summary") or {}).get("encrypted_pdfs", []),
            "scanned_candidates": (inv.get("summary") or {}).get("scanned_candidates", []),
            "source_policy": "源目录只读，所有改动发生在 /app/output；操作留审计日志与回滚脚本",
        },
    }

    skipped = [i["rel_path"] for i in items if i["action"] == "skip"]
    status = "success"
    if unresolved or skipped or args.partial_reason or not arep or brief.get("issues"):
        status = "partial"

    exceptions = {
        "skipped": skipped,
        "failed": unresolved,
        "conflicts": conflicts,
        "partial_reason": args.partial_reason,
        "issues_from_preflight": brief.get("issues", []),
    }

    uncertainty = []
    for r in (refs or {}).values():
        if r.get("title", {}).get("confidence") == "low":
            uncertainty.append(f"{r and ''}低置信标题启发（需人工复核）: 见 manifest")
            break
    if elements["permissions_and_sources"]["scanned_candidates"]:
        uncertainty.append("存在无文本层 PDF（疑似扫描件），元数据缺失较多，已如实标注")
    if elements["permissions_and_sources"]["encrypted_pdfs"]:
        uncertainty.append("存在加密 PDF，无法提取文本，已跳过并记录")

    # —— manifest ——
    manifest_rows = []
    for i in items:
        ref = refs.get(i["rel_path"], {})
        row = {
            "original_path": i["rel_path"], "sha256": i.get("sha256"),
            "action": i["action"], "target_path": i.get("target"),
            "doc_type": i.get("doc_type"), "tags": ";".join(i.get("tags", [])),
            "title": (ref.get("title") or {}).get("value"),
            "title_source": (ref.get("title") or {}).get("source"),
            "first_author": (ref.get("authors") or {}).get("value", [None])[0]
                            if (ref.get("authors") or {}).get("value") else None,
            "year": (ref.get("year") or {}).get("value"),
            "venue": (ref.get("venue") or {}).get("value"),
            "doi": (ref.get("doi") or {}).get("value"),
            "arxiv": (ref.get("arxiv") or {}).get("value"),
            "version_role": i.get("version_role"),
            "reason": i.get("reason"),
        }
        manifest_rows.append(row)
    with open(os.path.join(outdir, "manifest.csv"), "w", encoding="utf-8", newline="") as f:
        cols = list(manifest_rows[0].keys()) if manifest_rows else ["original_path"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in manifest_rows:
            w.writerow(r)
    with open(os.path.join(outdir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest_rows, f, ensure_ascii=False, indent=2)

    # —— output_contract 字段逐一满足；按键名启发自动映射到真实交付物，其余留给 Agent 填充 ——
    contract = brief.get("output_contract") or {}
    AUTO_MAP = [
        (("library", "organiz", "director", "目录", "library_root"),
         lambda: plan.get("config", {}).get("library_root", "library")),
        (("manifest", "metadata", "清单", "元数据", "catalog"),
         lambda: "manifest.csv"),
        (("inventory", "file_list", "filelist", "清单", "盘点", "文件"),
         lambda: {"inventory_csv": "../app/work/inventory.csv", "manifest_json": "manifest.json"}),
        (("dedup", "duplicat", "重复", "去重"),
         lambda: plan.get("exact_duplicates", [])),
        (("version", "版本"),
         lambda: plan.get("version_groups", [])),
        (("log", "audit", "操作", "日志"),
         lambda: "audit/operations.jsonl"),
        (("rollback", "回滚", "recover"),
         lambda: "audit/rollback.sh"),
        (("readme", "summary", "摘要", "报告"),
         lambda: "README.md"),
        (("tag", "标签"),
         lambda: {i["rel_path"]: i.get("tags", []) for i in items}),
        (("hash", "sha", "指纹"),
         lambda: "manifest.json 的 sha256 列（每个文件逐字节计算，可复跑核对）"),
        (("permiss", "权限", "来源", "provenance"),
         lambda: elements.get("permissions_and_sources")),
        (("status", "状态"),
         lambda: status),
    ]

    def auto_fill(key):
        kl = key.lower()
        for keys, fn in AUTO_MAP:
            if any(t in kl for t in keys):
                val = fn()
                return {"delivered": val, "note": "由流水线自动交付（按字段名启发映射）"}
        return {"delivered": None,
                "note": "未识别到该字段的自动交付路径，请 Agent 按 task.md 手工填充"}

    contract_out = {}
    if isinstance(contract, dict):
        for k, spec in contract.items():
            entry = auto_fill(k)
            entry["spec"] = spec
            contract_out[k] = entry
    elif isinstance(contract, list):
        for k in contract:
            contract_out[str(k)] = auto_fill(str(k))

    result = {
        "case_id": case_id,
        "skill": "research-library-curator",
        "skill_version": "1.0.0",
        "status": status,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "inputs": {
            "input_dir": brief.get("input_dir"),
            "material_count": brief.get("material_count"),
            "inventory_csv": "../app/work/inventory.csv",
        },
        "outputs": {
            "library_root": plan.get("config", {}).get("library_root", "library"),
            "manifest_csv": "manifest.csv",
            "manifest_json": "manifest.json",
            "audit_operations": "audit/operations.jsonl",
            "rollback_script": "audit/rollback.sh",
        },
        "statistics": stats,
        "elements": elements,
        "sources": "字段证据见 manifest.json 的 *_source 列与 /app/work/refs.json（source/confidence/evidence）",
        "uncertainty": uncertainty,
        "change_log": "全部变更见 audit/operations.jsonl；一键回滚: bash audit/rollback.sh",
        "exceptions": exceptions,
        "output_contract": contract_out,
    }
    with open(os.path.join(outdir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # —— README ——
    lib_root = os.path.join(outdir, plan.get("config", {}).get("library_root", "library"))
    lines = tree_lines(lib_root) if os.path.isdir(lib_root) else ["(library 尚未生成)"]
    readme = f"""# 整理结果摘要 — case {case_id}

- 状态: {status}
- 输入文件: {brief.get('material_count')} 个；已放置 {stats.get('placed', 0)}、归档 {stats.get('archived', 0)}、
  幂等跳过 {stats.get('already_applied', 0)}、冲突改名 {stats.get('conflict_renamed', 0)}、
  失败 {stats.get('failed', 0)}、跳过 {stats.get('skipped', 0)}
- 去重: {len(plan.get('exact_duplicates', []))} 组精确重复（归档于 _archive/duplicates）
- 版本组: {len(plan.get('version_groups', []))} 组（归档于 _archive/versions）

## 目录结构（前 200 条）

```
{chr(10).join(lines)}
```

## 复核入口

| 文件 | 内容 |
|-|-|
| manifest.csv / manifest.json | 每份输入文件的归属、元数据与字段来源 |
| audit/operations.jsonl | 全部文件操作的审计日志（可复跑核对） |
| audit/rollback.sh | 一键回滚全部复制操作 |
| result.json | 结构化结果（含 output_contract 字段、不确定性、异常清单） |

## 未解决项

{chr(10).join('- ' + u['rel_path'] + ': ' + u['reason'] for u in unresolved) or '无'}

## 异常与冲突

{chr(10).join('- ' + c['original_target'] + ' → ' + c['resolved_target'] for c in conflicts) or '无'}
"""
    with open(os.path.join(outdir, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme)
    print(f"[report] status={status} contract_keys={len(contract_out)} -> {outdir}/result.json")
    return 0

if __name__ == "__main__":
    sys.exit(main())
