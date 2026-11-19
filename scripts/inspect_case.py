#!/usr/bin/env python3
"""inspect_case.py — 预检：读取 /input/case.json 与 /input/task.md，生成 /app/work/brief.json。

用法: python3 inspect_case.py [--input-dir /input] [--work-dir /app/work]

只做读取与结构化，不做任何推断性补全。所有不确定项写入 issues。
"""
import argparse, json, os, re, sys

def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:
        return None, f"{path}: {e}"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", default="/input")
    ap.add_argument("--work-dir", default="/app/work")
    args = ap.parse_args()

    os.makedirs(args.work_dir, exist_ok=True)
    issues = []

    case_path = os.path.join(args.input_dir, "case.json")
    task_path = os.path.join(args.input_dir, "task.md")
    case, err = read_json(case_path)
    if err:
        issues.append(f"case.json 读取失败: {err}")
        case = {}
    task_text = ""
    if os.path.exists(task_path):
        with open(task_path, "r", encoding="utf-8", errors="replace") as f:
            task_text = f.read()
    else:
        issues.append("task.md 不存在")

    # case_id：兼容常见键名
    case_id = (case.get("case_id") or case.get("id") or case.get("caseId")
               or case.get("task_id") or os.path.basename(args.input_dir.rstrip("/")) or "unknown")
    contract = case.get("output_contract") or case.get("outputContract") or case.get("outputs") or {}

    # 任务约束的保守提取：只记录"题面中出现过的要求原文片段"，不自行发明规则
    task_lower = task_text
    hints = {
        "naming_template": None, "directory_scheme": None, "dry_run": None,
        "language": None, "library_root": None, "extra_rules": [],
    }
    m = re.search(r"(?:命名|rename|文件\s*名)[^\n]{0,200}", task_lower)
    if m:
        hints["naming_template"] = m.group(0).strip()
    m = re.search(r"(?:目录|分类|归入|organiz)[^\n]{0,200}", task_lower)
    if m:
        hints["directory_scheme"] = m.group(0).strip()
    if re.search(r"(?:dry[- ]?run|试运行|只生成计划|不真正移动|不改动)", task_lower, re.I):
        hints["dry_run"] = True
    m = re.search(r"(?:中文|英文|English|Chinese)", task_lower)
    if m:
        hints["language"] = m.group(0).strip()

    # 输入材料盘点（不含 case.json / task.md 本身）
    material_files = []
    for root, dirs, files in os.walk(args.input_dir):
        dirs.sort()
        for fn in sorted(files):
            if fn in ("case.json", "task.md"):
                continue
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, args.input_dir)
            try:
                st = os.lstat(p)
                material_files.append({
                    "path": p, "rel_path": rel, "size": st.st_size,
                    "readable": os.access(p, os.R_OK),
                })
            except OSError as e:
                issues.append(f"无法 stat {rel}: {e}")
    if not material_files:
        issues.append("除 case.json/task.md 外未发现材料文件；检查是否材料在其它路径")

    brief = {
        "case_id": case_id,
        "output_contract": contract,
        "output_contract_keys": list(contract.keys()) if isinstance(contract, dict) else contract,
        "task_path": task_path,
        "task_length_chars": len(task_text),
        "task_hints": hints,
        "input_dir": args.input_dir,
        "material_count": len(material_files),
        "material_files_preview": material_files[:50],
        "issues": issues,
    }
    out = os.path.join(args.work_dir, "brief.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(brief, f, ensure_ascii=False, indent=2)
    print(f"[inspect_case] case_id={case_id} materials={len(material_files)} issues={len(issues)}")
    print(f"[inspect_case] contract keys: {brief['output_contract_keys']}")
    print(f"[inspect_case] brief -> {out}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
