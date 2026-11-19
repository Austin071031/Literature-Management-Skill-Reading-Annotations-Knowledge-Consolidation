#!/usr/bin/env python3
"""extract_refs.py — 元数据提取：标题/作者/年份/期刊/DOI/arXiv/关键词。

铁律：只提取原文或侧车文件中真实存在的字段；提取不到的字段为 null 并注明原因，
绝不根据文件名或常识"补造"作者、年份、DOI。每个字段携带 source 与 confidence。

用法: python3 extract_refs.py [--work-dir /app/work]
输出: /app/work/refs.json
"""
import argparse, json, os, re, subprocess, sys

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
ARXIV_NEW_RE = re.compile(r"\b(\d{4}\.\d{4,5})(v\d+)?\b")
ARXIV_OLD_RE = re.compile(r"\b([a-z-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?\b")
YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-6]\d)\b")
KEYWORDS_RE = re.compile(r"(?im)^\s*(keywords?|索引词|关键词|關鍵詞)\s*[:：.]\s*(.+)$")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

JOURNAL_HINT = re.compile(r"(?i)\b(journal|transactions|proceedings|conference|review|"
                          r"letters|annals|quarterly|bulletin|学报|杂志|研究)\b")
THESIS_HINT = re.compile(r"(?i)(thesis|dissertation|学位论文|博士|硕士|本科毕业论文)")
PREPRINT_HINT = re.compile(r"(?i)\b(preprint|arxiv|biorxiv|chemrxiv|ssrn|预印本)\b")
SUPPL_HINT = re.compile(r"(?i)(supplement|supporting\s+information|appendix|补充材料|附录)")

def run(cmd, timeout=90):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -2, "", "command not found"

def field(value, source, confidence, evidence=""):
    return {"value": value, "source": source, "confidence": confidence, "evidence": evidence[:160]}

NULL_FIELD = field(None, "not_found", "none", "")

def norm_doi(d):
    if not d:
        return None
    d = d.strip().rstrip(".,;)")
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d, flags=re.I)
    return d.lower()

def first_author_surname(authors):
    if not authors:
        return None
    a0 = authors[0].strip()
    # "Last, First" 或 "First Last"
    if "," in a0:
        return a0.split(",", 1)[0].strip() or None
    parts = a0.split()
    if parts:
        return parts[-1]
    return None

def parse_bibtex(text):
    """极简 BibTeX 解析：字段级提取，不追求完整语法覆盖。"""
    fields = {}
    entry = re.search(r"@\w+\s*\{", text)
    if not entry:
        return fields
    body = text[entry.end():]
    m = re.finditer(r"(\w+)\s*=\s*[\{\"](.+?)[\}\"]\s*,?\s*(?=\n\s*\w+\s*=|\n?\})", body, re.S)
    for k, v in ((g.group(1).lower(), g.group(2)) for g in m):
        fields[k] = re.sub(r"\s+", " ", v).strip().strip("{}")
    return fields

def parse_ris(text):
    fields = {}
    for line in text.splitlines():
        m = re.match(r"([A-Z][A-Z1-9])\s+-\s+(.*)", line)
        if not m:
            continue
        tag, val = m.group(1), m.group(2).strip()
        if tag == "TI":
            fields["title"] = val
        elif tag in ("AU", "A1"):
            fields.setdefault("author", []).append(val)
        elif tag in ("JO", "JF", "JA"):
            fields.setdefault("journal", val)
        elif tag == "PY":
            fields["year"] = val
        elif tag == "DO":
            fields["doi"] = val
    if isinstance(fields.get("author"), list):
        fields["author"] = " and ".join(fields["author"])
    return fields

def parse_sidecar(stem_dir, stem, ext):
    """查找同名侧车元数据文件 (.bib/.ris/.enw/.json/.txt 元数据)。"""
    for sc_ext, kind in ((".bib", "bibtex"), (".ris", "ris"), (".enw", "ris")):
        p = os.path.join(stem_dir, stem + sc_ext)
        if os.path.exists(p) and os.path.getsize(p) > 0:
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            return kind, text, p
    return None, None, None

def guess_title_from_text(first_lines):
    """从 PDF 首页文本猜标题：取最靠前的、非页眉噪声的较长行（可跨 1-2 行）。
    只作为 low confidence 兜底，调用方需标注来源。"""
    cand = []
    for i, line in enumerate(first_lines[:30]):
        s = line.strip()
        if not s or len(s) < 8:
            continue
        low = s.lower()
        if (EMAIL_RE.search(s) or DOI_RE.search(s) or "arxiv" in low
                or low.startswith(("www.", "http")) or "university" in low
                or re.match(r"^\d+\s*$", s) or "©" in s or low.startswith("vol.")):
            continue
        cand.append((i, s))
        if len(s) >= 20 and not s.endswith((":", ";")):
            # 可能是标题首行，尝试拼接下一行（标题常折行）
            if i + 1 < len(first_lines):
                nxt = first_lines[i + 1].strip()
                if (nxt and len(nxt) >= 8 and not EMAIL_RE.search(nxt)
                        and not DOI_RE.search(nxt)
                        and not re.search(r"(?i)\b(abstract|摘要|keywords)\b", nxt)
                        and nxt[0].isupper() is False or nxt.isupper()):
                    pass  # 保守：不跨行拼接，避免把作者行并入标题
            break
    return cand[0][1] if cand else None

def guess_authors_from_text(first_lines, title_idx_hint=None):
    """极保守的作者猜测：多行逗号分隔的人名模式。仅 low confidence。"""
    name_pat = re.compile(r"^[A-Z][a-zA-Z'’\-\.]+(\s+[A-Z]\.?)?(\s+[A-Z][a-zA-Z'’\-]+)+$")
    for line in first_lines[:40]:
        s = line.strip().rstrip(",;")
        if not s or len(s) < 6 or EMAIL_RE.search(s) or "university" in s.lower():
            continue
        parts = [p.strip() for p in re.split(r",|\band\b", s) if p.strip()]
        if len(parts) >= 1 and all(name_pat.match(p) for p in parts) and len(parts) <= 30:
            # 排除单个大写单词（可能是标题）
            if any(" " in p for p in parts):
                return parts
    return None

def extract_pdf(abs_path):
    out = {
        "title": None, "authors": None, "year": None, "venue": None,
        "doi": None, "arxiv": None, "keywords": None, "doc_hint": None,
        "text_chars": 0, "extract_notes": [],
    }
    rc, txt, err = run(["pdftotext", "-f", "1", "-l", "3", abs_path, "-"])
    if rc != 0 or not txt.strip():
        out["extract_notes"].append(f"pdftotext 无文本输出: {(err or '').strip()[:120] or 'rc=%d' % rc}")
        return out
    out["text_chars"] = len(txt.strip())
    lines = txt.splitlines()
    joined = "\n".join(lines[:60])

    m = DOI_RE.search(txt[:4000])
    if m:
        out["doi"] = field(norm_doi(m.group(0)), "pdf_text", "high", m.group(0))
    m = re.search(r"(?i)arxiv[:.\s]*(\d{4}\.\d{4,5})(v\d+)?", txt[:4000])
    if not m:
        m = re.search(r"(?i)arxiv:([a-z-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?", txt[:4000])
    if m:
        ver = m.group(2) or ""
        out["arxiv"] = field(m.group(1) + ver, "pdf_text", "high", m.group(0))

    title = guess_title_from_text(lines)
    if title:
        out["title"] = field(title, "pdf_text_heuristic", "low", title)
    authors = guess_authors_from_text(lines)
    if authors:
        out["authors"] = field(authors, "pdf_text_heuristic", "low", lines[:40] and authors[0])
    # 年份：从头部区域取第一个合理年份（与 © / Received / published 邻近优先）
    head = joined
    ym = re.search(r"(?i)(?:©|copyright|received|accepted|published|vol\.?|no\.?)[^\n]{0,60}?\b(19[5-9]\d|20[0-6]\d)\b", head)
    if not ym:
        ym = YEAR_RE.search(head)
    if ym:
        out["year"] = field(int(ym.group(1)), "pdf_text", "medium", ym.group(0)[:80])
    # 期刊/会议线索
    for line in lines[:25]:
        if JOURNAL_HINT.search(line) and len(line.strip()) >= 10 and not DOI_RE.search(line):
            out["venue"] = field(line.strip(), "pdf_text_hint", "low", line.strip())
            break
    km = KEYWORDS_RE.search(txt)
    if km:
        kws = [k.strip().strip(".,;") for k in re.split(r"[;,;、，]", km.group(2)) if k.strip()]
        kws = [k for k in kws if 2 <= len(k) <= 40][:10]
        if kws:
            out["keywords"] = field(kws, "pdf_text", "medium", km.group(2)[:120])

    low = txt[:3000].lower()
    if PREPRINT_HINT.search(low):
        out["doc_hint"] = field("preprint", "pdf_text_hint", "medium", "arxiv/preprint 字样")
    elif THESIS_HINT.search(low):
        out["doc_hint"] = field("thesis", "pdf_text_hint", "low", "thesis/dissertation/学位 字样")
    elif SUPPL_HINT.search(os.path.basename(abs_path)):
        out["doc_hint"] = field("supplement", "filename_hint", "low", "文件名含 supplement/附录")
    return out

def extract_text_file(abs_path):
    out = {"title": None, "authors": None, "year": None, "venue": None,
           "doi": None, "arxiv": None, "keywords": None, "doc_hint": None,
           "text_chars": 0, "extract_notes": []}
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read(20000)
    except OSError as e:
        out["extract_notes"].append(f"read failed: {e}")
        return out
    out["text_chars"] = len(txt)
    m = DOI_RE.search(txt)
    if m:
        out["doi"] = field(norm_doi(m.group(0)), "file_text", "high", m.group(0))
    m = re.search(r"(?i)arxiv[:.\s]*(\d{4}\.\d{4,5})(v\d+)?", txt)
    if m:
        out["arxiv"] = field(m.group(1) + (m.group(2) or ""), "file_text", "high", m.group(0))
    m = YEAR_RE.search(txt)
    if m:
        out["year"] = field(int(m.group(1)), "file_text", "low", m.group(0))
    km = KEYWORDS_RE.search(txt)
    if km:
        kws = [k.strip() for k in re.split(r"[;,;、，]", km.group(2)) if k.strip()][:10]
        if kws:
            out["keywords"] = field(kws, "file_text", "medium", km.group(2)[:120])
    return out

def merge(sidecar_fields, extracted):
    """侧车（.bib/.ris）优先于启发式；保留双方证据。"""
    merged = dict(extracted)
    if not sidecar_fields:
        return merged
    sc = {k.lower(): v for k, v in sidecar_fields.items()}
    if sc.get("title"):
        merged["title"] = field(sc["title"], "sidecar", "high", sc["title"])
    if sc.get("author"):
        authors = [a.strip() for a in re.split(r"\s+and\s+", sc["author"]) if a.strip()]
        merged["authors"] = field(authors, "sidecar", "high", sc["author"])
    if sc.get("year"):
        y = re.search(r"\d{4}", str(sc["year"]))
        if y:
            merged["year"] = field(int(y.group(0)), "sidecar", "high", str(sc["year"]))
    if sc.get("journal") or sc.get("booktitle"):
        merged["venue"] = field(sc.get("journal") or sc.get("booktitle"), "sidecar", "high",
                                sc.get("journal") or sc.get("booktitle"))
    if sc.get("doi"):
        merged["doi"] = field(norm_doi(sc["doi"]), "sidecar", "high", sc["doi"])
    if sc.get("keywords"):
        kws = [k.strip() for k in re.split(r"[;,;、，]", sc["keywords"]) if k.strip()][:10]
        merged["keywords"] = field(kws, "sidecar", "high", sc["keywords"])
    if sc.get("type") or sc.get("entry_type"):
        merged["doc_hint"] = field(sc.get("type") or sc.get("entry_type"), "sidecar", "high",
                                   str(sc.get("type") or sc.get("entry_type")))
    return merged

def normalize_ref(rec, merged):
    ref = {}
    for key in ("title", "authors", "year", "venue", "doi", "arxiv", "keywords", "doc_hint"):
        ref[key] = merged.get(key) or NULL_FIELD
    ref["first_author_surname"] = None
    if ref["authors"]["value"]:
        ref["first_author_surname"] = first_author_surname(ref["authors"]["value"])
    ref["normalized_title"] = None
    if ref["title"]["value"]:
        ref["normalized_title"] = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "",
                                         ref["title"]["value"].lower())
    ref["text_chars"] = merged.get("text_chars", 0)
    ref["extract_notes"] = merged.get("extract_notes", [])
    return ref

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", default="/app/work")
    args = ap.parse_args()
    inv_path = os.path.join(args.work_dir, "inventory.json")
    if not os.path.exists(inv_path):
        print("[extract_refs] 缺少 inventory.json", file=sys.stderr)
        return 2
    with open(inv_path, "r", encoding="utf-8") as f:
        inv = json.load(f)

    refs = {}
    for rec in inv["records"]:
        rel = rec["rel_path"]
        p = rec["abs_path"]
        sniff = rec["mime_sniff"]
        stem_dir = os.path.dirname(p)
        stem = os.path.splitext(os.path.basename(p))[0]

        sc_kind, sc_text, sc_path = parse_sidecar(stem_dir, stem, rec["extension"])
        sidecar_fields = {}
        if sc_kind == "bibtex":
            sidecar_fields = parse_bibtex(sc_text)
        elif sc_kind == "ris":
            sidecar_fields = parse_ris(sc_text)

        if sniff == "pdf" and rec["pdf_encrypted"] is not True:
            extracted = extract_pdf(p)
        elif sniff == "text":
            extracted = extract_text_file(p)
        else:
            extracted = {"text_chars": 0, "extract_notes": ["非文本/PDF 类型，仅依赖侧车与文件名"]}
            for k in ("title", "authors", "year", "venue", "doi", "arxiv", "keywords", "doc_hint"):
                extracted[k] = None

        merged = merge(sidecar_fields, extracted)
        ref = normalize_ref(rec, merged)
        ref["sidecar_path"] = sc_path
        # 年份降级：从新式 arXiv 号 (YYMM.NNNNN) 推导，标注来源
        if not ref["year"]["value"] and ref["arxiv"]["value"]:
            m = re.match(r"(\d{2})(\d{2})\.\d{4,5}", ref["arxiv"]["value"])
            if m and int(m.group(1)) <= 26:
                ref["year"] = field(2000 + int(m.group(1)), "arxiv_id_derived", "medium",
                                    ref["arxiv"]["value"])
        refs[rel] = ref

    with open(os.path.join(args.work_dir, "refs.json"), "w", encoding="utf-8") as f:
        json.dump(refs, f, ensure_ascii=False, indent=2)
    n_doi = sum(1 for r in refs.values() if r["doi"]["value"])
    n_title = sum(1 for r in refs.values() if r["title"]["value"])
    print(f"[extract_refs] files={len(refs)} with_doi={n_doi} with_title={n_title}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
