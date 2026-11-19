#!/usr/bin/env python3
"""inventory.py — 输入盘点：哈希、类型嗅探、权限、PDF 页数/文本层/加密检测。

用法: python3 inventory.py [--work-dir /app/work]
依赖 brief.json（inspect_case.py 产出）。仅读取输入，不改动任何文件。
输出: /app/work/inventory.json, /app/work/inventory.csv
"""
import argparse, csv, hashlib, json, os, stat, subprocess, sys

def sha256_of(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def md5_of(path, chunk=1 << 20):
    h = hashlib.md5()  # 仅用于与常见工具交叉核对，非安全用途
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def sniff_type(path):
    """用文件魔数做类型嗅探；不依赖扩展名。"""
    try:
        with open(path, "rb") as f:
            head = f.read(2048)
    except OSError:
        return "unreadable"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return "zip"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"RAR!"):
        return "rar"
    if head.startswith(b"\x00\x00\x00\x18") or head.startswith(b"\x00\x00\x00\x20"):
        return "mp4"
    # 文本启发：高比例可打印/UTF-8 解码
    try:
        head.decode("utf-8")
        return "text"
    except UnicodeDecodeError:
        pass
    return "binary"

def run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -2, "", "command not found"

def pdf_probe(path):
    """返回 pages/encrypted/text_chars/info_error；全部失败时给出原因。"""
    info = {"pages": None, "encrypted": None, "text_chars_first2p": None, "error": None}
    rc, out, err = run(["pdfinfo", path])
    if rc != 0:
        info["error"] = (err or out).strip()[:300] or f"pdfinfo rc={rc}"
        # 加密 pdfinfo 也可能报错，尝试 pdftotext 进一步区分
        rc2, out2, err2 = run(["pdftotext", "-f", "1", "-l", "2", path, "-"])
        if rc2 != 0 and ("password" in (err2 or "").lower() or "encrypt" in (err2 or "").lower()):
            info["encrypted"] = True
        return info
    for line in out.splitlines():
        if line.startswith("Pages:"):
            try:
                info["pages"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        if line.startswith("Encrypted:"):
            info["encrypted"] = line.split(":", 1)[1].strip().lower() == "yes"
    rc2, out2, err2 = run(["pdftotext", "-f", "1", "-l", "2", path, "-"])
    if rc2 == 0:
        info["text_chars_first2p"] = len((out2 or "").strip())
    else:
        info["error"] = info["error"] or ((err2 or out2).strip()[:300] or f"pdftotext rc={rc2}")
    return info

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", default="/app/work")
    args = ap.parse_args()
    brief_path = os.path.join(args.work_dir, "brief.json")
    if not os.path.exists(brief_path):
        print("[inventory] 缺少 brief.json，请先运行 inspect_case.py", file=sys.stderr)
        return 2
    with open(brief_path, "r", encoding="utf-8") as f:
        brief = json.load(f)

    input_dir = brief.get("input_dir", "/input")
    records, errors = [], []
    for root, dirs, files in os.walk(input_dir):
        dirs.sort()
        for fn in sorted(files):
            if fn in ("case.json", "task.md"):
                continue
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, input_dir)
            rec = {
                "rel_path": rel, "abs_path": p, "size_bytes": None,
                "sha256": None, "md5": None, "mime_sniff": None,
                "extension": os.path.splitext(fn)[1].lower().lstrip("."),
                "readable": os.access(p, os.R_OK), "writable": os.access(p, os.W_OK),
                "perm_mode": None, "mtime": None,
                "pdf_pages": None, "pdf_encrypted": None,
                "pdf_text_chars_first2p": None, "error": None,
            }
            try:
                st = os.lstat(p)
                rec["size_bytes"] = st.st_size
                rec["perm_mode"] = oct(st.st_mode & 0o777)
                rec["mtime"] = st.st_mtime
            except OSError as e:
                rec["error"] = f"stat failed: {e}"
                errors.append(rec)
                continue
            if not rec["readable"]:
                rec["error"] = "permission denied (not readable)"
                errors.append(rec)
                continue
            try:
                rec["sha256"] = sha256_of(p)
                rec["md5"] = md5_of(p)
            except OSError as e:
                rec["error"] = f"hash failed: {e}"
                errors.append(rec)
                continue
            rec["mime_sniff"] = sniff_type(p)
            if rec["mime_sniff"] == "pdf":
                probe = pdf_probe(p)
                rec["pdf_pages"] = probe["pages"]
                rec["pdf_encrypted"] = probe["encrypted"]
                rec["pdf_text_chars_first2p"] = probe["text_chars_first2p"]
                if probe["error"]:
                    rec["error"] = f"pdf probe: {probe['error']}"
                    errors.append(rec)
            records.append(rec)

    # 精确重复（同 sha256）
    by_hash = {}
    for r in records:
        if r["sha256"]:
            by_hash.setdefault(r["sha256"], []).append(r["rel_path"])
    exact_dup_groups = [v for v in by_hash.values() if len(v) > 1]

    summary = {
        "total_files": len(records),
        "by_sniff_type": {},
        "exact_duplicate_groups": exact_dup_groups,
        "unreadable_or_broken": [e["rel_path"] for e in errors],
        "encrypted_pdfs": [r["rel_path"] for r in records if r["pdf_encrypted"] is True],
        "scanned_candidates": [r["rel_path"] for r in records
                               if r["mime_sniff"] == "pdf"
                               and (r["pdf_text_chars_first2p"] or 0) == 0
                               and r["pdf_encrypted"] is not True
                               and r["error"] is None],
    }
    for r in records:
        t = r["mime_sniff"] or "unknown"
        summary["by_sniff_type"][t] = summary["by_sniff_type"].get(t, 0) + 1

    out = {"case_id": brief.get("case_id"), "input_dir": input_dir,
           "summary": summary, "records": records}
    with open(os.path.join(args.work_dir, "inventory.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    cols = ["rel_path", "size_bytes", "sha256", "md5", "mime_sniff", "extension",
            "readable", "writable", "perm_mode", "pdf_pages", "pdf_encrypted",
            "pdf_text_chars_first2p", "error"]
    with open(os.path.join(args.work_dir, "inventory.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in records:
            w.writerow(r)
    print(f"[inventory] files={len(records)} dup_groups={len(exact_dup_groups)} "
          f"errors={len(errors)} scanned={len(summary['scanned_candidates'])}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
