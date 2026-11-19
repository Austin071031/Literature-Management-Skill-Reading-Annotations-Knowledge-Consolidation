#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
annotation-knowledge-sink (aks) pipeline
=========================================
阅读批注与知识沉淀流水线：库存盘点 -> 批注提取(带锚点校验) -> 笔记结构化
-> 可解释关联/版本归并 -> SQLite 可检索索引 -> 证据与日志 -> result.json 草稿。

设计原则
--------
1. 确定性优先：全部重型工作由本脚本完成，Agent 只做审阅与必要的人工判断。
2. 诚实记录：任何失败、跳过、低置信度都写入 run.log 与 result.json，绝不把未完成写成完成。
3. 环境自适应：PDF 引擎按 PyMuPDF -> pypdf+poppler -> poppler-only 降级；
   OCR 依赖 tesseract（可选，缺失时明确标注）。
4. 可复现：记录脚本自身 sha256、依赖版本、完整命令行与每个文件的 sha256。

用法
----
    python3 pipeline.py --case /input/case.json \
        --workdir /app/work --outdir /app/output

退出码：0 = 完成（可能含部分失败，见 result.json 的 status/failures）；
        2 = 输入缺失等致命错误（仍会尽力写出诊断信息）。
"""
import argparse
import datetime
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback

PIPELINE_VERSION = "1.0.0"
SCRIPT_PATH = os.path.abspath(__file__) if "__file__" in globals() else "<embedded>"

# ---------------------------------------------------------------- 常量与词表
PDF_EXTS = {".pdf", ".pdfa"}
TEXT_EXTS = {".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".xml", ".bib", ".ris"}
HTML_EXTS = {".html", ".htm", ".mhtml", ".mht"}
DOCX_EXTS = {".docx"}
IMG_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp", ".webp", ".pgm", ".ppm"}

TYPE_MAP = {
    "highlight": "highlight", "underline": "underline", "squiggly": "squiggly",
    "strikeout": "strikeout", "freetext": "margin_note", "text": "sticky_note",
    "caret": "caret", "ink": "handwritten_mark", "stamp": "stamp",
    "widget": "form_field", "line": "line_mark", "polyline": "line_mark",
    "square": "shape", "circle": "shape", "polygon": "shape",
    "fileattachment": "attachment", "redact": "redaction", "trapnet": "other",
    "watermark": "other", "3d": "other", "printersmark": "other",
    "sound": "media", "movie": "media", "screen": "media", "richmedia": "media",
}
PYPDF_SUBTYPE_MAP = {
    "/Highlight": "highlight", "/Underline": "underline", "/Squiggly": "squiggly",
    "/StrikeOut": "strikeout", "/Strikeout": "strikeout", "/FreeText": "margin_note",
    "/Text": "sticky_note", "/Caret": "caret", "/Ink": "handwritten_mark",
    "/Stamp": "stamp", "/Widget": "form_field", "/Line": "line_mark",
    "/PolyLine": "line_mark", "/Square": "shape", "/Circle": "shape",
    "/Polygon": "shape", "/FileAttachment": "attachment", "/Popup": "popup",
    "/Redact": "redaction", "/Sound": "media", "/Movie": "media",
    "/Screen": "media", "/RichMedia": "media", "/PrinterMark": "other",
    "/TrapNet": "other", "/Watermark": "other", "/3D": "other",
}

CATEGORY_RULES = [
    ("question", ["question", "why", "how come", "unclear", "疑问", "问题", "为什么", "为何", "如何解释", "不清楚", "不懂"]),
    ("method", ["method", "approach", "algorithm", "protocol", "workflow", "pipeline", "方法", "算法", "流程", "协议", "步骤", "模型结构", "实现"]),
    ("conclusion", ["conclusion", "result", "finding", "show that", "demonstrate", "achieve", "outperform", "结论", "结果表明", "结果显示", "发现", "证明", "表明", "优于", "达到"]),
    ("doubt", ["todo", "check", "verify", "suspicious", "really?", "doubt", "疑点", "待验证", "核实", "怀疑", "存疑", "注意核对", "真？", "真的吗"]),
    ("reusable", ["reusable", "reuse", "important", "key idea", "takeaway", "可复用", "要点", "关键", "重要", "借鉴", "引用价值", "值得"]),
]
DEFAULT_CATEGORY = "keypoint"

METHOD_LEXICON = [
    "density functional theory", "dft", "dftb", "hartree-fock", "hf", "mp2", "ccsd(t)",
    "monte carlo", "kinetic monte carlo", "kmc", "molecular dynamics", "md simulation",
    "ab initio molecular dynamics", "aimd", "reactive molecular dynamics", "reaxff",
    "finite element", "fem", "bayesian optimization", "bayesian inference", "markov chain",
    "metropolis", "umbrella sampling", "metadynamics", "replica exchange", "remd",
    "machine learning", "deep learning", "graph neural network", "gnn", "cnn", "rnn",
    "lstm", "transformer", "attention", "autoencoder", "generative model", "diffusion model",
    "random forest", "support vector machine", "svm", "gradient boosting", "xgboost",
    "active learning", "reinforcement learning", "transfer learning", "federated learning",
    "principal component analysis", "pca", "clustering", "k-means", "gaussian process",
    "neural network potential", "machine learning potential", "deep potential", "dpmd",
    "molecular docking", "md simulation", "free energy perturbation", "fep", "mm/gb sa",
    "crystal structure prediction", "structure prediction", "global optimization",
    "genetic algorithm", "simulated annealing", "particle swarm", "bayesian network",
    "ordinary differential equation", "partial differential equation", "pde",
]

RE_DOI = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>；;，,]+")
RE_ARXIV = re.compile(r"\b(?:arXiv[:：\s]*)?(\d{4}\.\d{4,5})(?:v\d+)?\b", re.IGNORECASE)
RE_URL = re.compile(r"https?://[^\s\"'<>）)]+")
RE_CITATION = re.compile(r"\(([A-Z][A-Za-zÀ-ÿ'’\-]+(?:\s+et\s+al\.?|\s+and\s+[A-Z][A-Za-zÀ-ÿ'’\-]+)?,?\s+(?:19|20)\d{2}[a-z]?(?:;\s*[^)]+)*)\)")
RE_QUANTITY = re.compile(r"-?\d+(?:\.\d+)?\s?(?:K|MPa|GPa|kbar|bar|eV|meV|kcal/mol|kJ/mol|J/mol|kJ·mol|nm|μm|um|Å|angstrom|fs|ps|ns|μs|ms|s)\b")
RE_VERSION_TOKEN = re.compile(
    r"[\s_\-.]*(?:\(?((?<![A-Za-z0-9])v?\d+(?:\.\d+){0,3}(?![A-Za-z0-9]))\)?|"
    r"\(?(19|20)\d{2}[-_.]?\d{2}[-_.]?\d{2}\)?|"
    r"\(?((?<![A-Za-z0-9])rev\d*|r\d+|final\d*|vfinal|draft\d*|preprint|accepted|published|submitted|"
    r"old|new|latest|copy|副本|复件|打印|扫描|版本\d*)\)?)", re.IGNORECASE)
RE_SOURCE_REF = re.compile(
    r"([\w\-.一-鿿 ()（）]+\.(?:pdf|docx?|html?|md|txt))\s*[,:：]?\s*(?:p\.?\s*(\d+)|第\s*(\d+)\s*页|page\s*(\d+))?",
    re.IGNORECASE)

WORD_RE_BBOX = re.compile(
    r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>')
PAGE_RE_BBOX = re.compile(r'<page width="([\d.]+)" height="([\d.]+)"')


# ---------------------------------------------------------------- 工具函数
def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def sha256_file(path, events=None, tag=None):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except PermissionError:
        _ev(events, "failure", path, "permission denied while hashing")
        return None
    except OSError as e:
        _ev(events, "failure", path, "hash error: %s" % e)
        return None


def collapse_ws(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _ev(events, level, event, file=None, detail=None):
    rec = {"ts": now_iso(), "level": level, "event": event}
    if file:
        rec["file"] = file
    if detail:
        rec["detail"] = str(detail)[:2000]
    events.append(rec)
    try:
        line = json.dumps(rec, ensure_ascii=False)
        print("[aks:%s] %s %s" % (level.upper(), event, (file or "")), file=sys.stderr)
    except Exception:
        pass
    return rec


def norm_quote(s):
    return collapse_ws(s).lower()


def order_quad(points):
    """将 4 个角点排成凸多边形顺序（PDF QuadPoints 常见顺序为 UL,UR,LL,LR，非凸序）。"""
    import math
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    return sorted(points, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))


def point_in_quad(cx, cy, quad):
    """quad: 4 (x,y) points of a convex quadrilateral (任意顺序，内部重排)。"""
    quad = order_quad(quad)
    sign = None
    n = 4
    for i in range(n):
        x1, y1 = quad[i]
        x2, y2 = quad[(i + 1) % n]
        cross = (x2 - x1) * (cy - y1) - (y2 - y1) * (cx - x1)
        if sign is None:
            sign = cross
        elif (cross < 0) != (sign < 0):
            return False
    return True


def pdfdate(s):
    if not s:
        return None
    s = str(s)
    m = re.match(r"D?:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?([+-Z])?(\d{2})?'?(\d{2})?", s)
    if not m:
        return s
    y, mo, d, h, mi, se = (m.group(i) or "0" for i in range(1, 7))
    return "%04d-%02d-%02dT%02d:%02d:%02d" % (int(y), max(1, int(mo)), max(1, int(d)),
                                              int(h), int(mi), int(se))


def rgb_to_hex(rgb):
    if not rgb or len(rgb) < 3:
        return None
    try:
        return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c * 255)))) for c in rgb[:3])
    except Exception:
        return None


def _pip_install(pkg, events=None):
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                            "--disable-pip-version-check", pkg],
                           capture_output=True, timeout=150)
        return r.returncode == 0, (r.stderr or b"").decode("utf-8", errors="replace")[-300:]
    except Exception as e:
        return False, str(e)


def detect_engine(preferred="auto", events=None):
    if preferred in ("fitz", "auto"):
        try:
            import fitz  # noqa
            return "fitz"
        except Exception:
            if preferred == "fitz":
                return None
    if preferred in ("pypdf", "auto"):
        try:
            import pypdf  # noqa
            return "pypdf"
        except Exception:
            if preferred == "pypdf":
                return None
            # 自修复：尝试安装纯 Python 的 pypdf（无需编译），失败则降级 poppler
            ok, msg = _pip_install("pypdf", events)
            _ev(events, "info" if ok else "warning", None,
                "auto-install pypdf: %s %s" % ("ok" if ok else "failed", "" if ok else msg))
            if ok:
                try:
                    import pypdf  # noqa
                    return "pypdf"
                except Exception:
                    pass
    return "poppler" if shutil.which("pdftotext") else None


def has_tesseract():
    if shutil.which("tesseract"):
        return "cli"
    try:
        import pytesseract  # noqa
        return "py"
    except Exception:
        return None


# ---------------------------------------------------------------- PDF 文本工具（poppler）
_bbox_cache = {}


def poppler_page_info(pdf_path, pno, events, workdir):
    """返回 (width, height, words[(x0,y0,x1,y1,text)], page_text)。pno 为 0-based。"""
    key = (os.path.abspath(pdf_path), pno)
    if key in _bbox_cache:
        return _bbox_cache[key]
    tmp = os.path.join(workdir, "bbox_tmp_%d.html" % os.getpid())
    cmd = ["pdftotext", "-f", str(pno + 1), "-l", str(pno + 1), "-bbox", pdf_path, tmp]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        with open(tmp, "r", encoding="utf-8", errors="replace") as f:
            xml = f.read()
    except Exception as e:
        _ev(events, "failure", pdf_path, "pdftotext -bbox page %d failed: %s" % (pno + 1, e))
        _bbox_cache[key] = (None, None, [], "")
        return _bbox_cache[key]
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    pm = PAGE_RE_BBOX.search(xml)
    width = float(pm.group(1)) if pm else None
    height = float(pm.group(2)) if pm else None
    words = [(float(a), float(b), float(c), float(d), html.unescape(t))
             for a, b, c, d, t in WORD_RE_BBOX.findall(xml)]
    try:
        r = subprocess.run(["pdftotext", "-f", str(pno + 1), "-l", str(pno + 1), pdf_path, "-"],
                           check=True, capture_output=True, timeout=120)
        page_text = r.stdout.decode("utf-8", errors="replace")
    except Exception as e:
        _ev(events, "failure", pdf_path, "pdftotext page %d failed: %s" % (pno + 1, e))
        page_text = ""
    _bbox_cache[key] = (width, height, words, page_text)
    return _bbox_cache[key]


def words_in_quad(words, quad):
    out = []
    for (x0, y0, x1, y1, t) in words:
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        if point_in_quad(cx, cy, quad):
            out.append((x0, y0, x1, y1, t))
    # 按阅读顺序排序：先 y（行），再 x
    out.sort(key=lambda w: (round(w[1], 1), w[0]))
    lines = []
    cur_y = None
    cur = []
    for w in out:
        if cur_y is None or abs(w[1] - cur_y) <= 2.0:
            cur.append(w[4])
            cur_y = w[1] if cur_y is None else (cur_y + w[1]) / 2
        else:
            lines.append(" ".join(cur))
            cur = [w[4]]
            cur_y = w[1]
    if cur:
        lines.append(" ".join(cur))
    return "\n".join(lines)


def render_region(pdf_path, pno, rect, out_png, dpi=200, events=None):
    x0, y0, x1, y1 = [max(0.0, float(v)) for v in rect]
    scale = dpi / 72.0
    prefix = out_png[:-4] if out_png.endswith(".png") else out_png
    cmd = ["pdftoppm", "-png", "-f", str(pno + 1), "-l", str(pno + 1), "-r", str(dpi),
           "-x", str(int(x0 * scale)), "-y", str(int(y0 * scale)),
           "-W", str(max(8, int((x1 - x0) * scale))), "-H", str(max(8, int((y1 - y0) * scale))),
           pdf_path, prefix]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        produced = prefix + "-%d.png" % (pno + 1)
        if os.path.exists(produced) and produced != out_png:
            os.replace(produced, out_png)
        return os.path.exists(out_png)
    except Exception as e:
        _ev(events, "failure", pdf_path, "pdftoppm region render failed p%d: %s" % (pno + 1, e))
        return False


def ocr_image(img_path, events=None, src=None):
    mode = has_tesseract()
    if not mode:
        return None, "tesseract not available"
    try:
        if mode == "cli":
            r = subprocess.run(["tesseract", img_path, "stdout", "-l", "eng+chi_sim"],
                               capture_output=True, timeout=120)
            return r.stdout.decode("utf-8", errors="replace").strip(), "tesseract-cli"
        import pytesseract
        from PIL import Image
        return pytesseract.image_to_string(Image.open(img_path), lang="eng+chi_sim").strip(), "pytesseract"
    except Exception as e:
        _ev(events, "warning", src, "OCR failed: %s" % e)
        return None, "ocr error: %s" % e


# ---------------------------------------------------------------- 锚点校验
def verify_anchor(quote, page_text, method="text-layer"):
    """返回 {verified, match, char_start, char_end, ratio}。定位 quote 在页文本中的位置。"""
    res = {"verified": False, "match": None, "char_start": None, "char_end": None, "ratio": 0.0}
    if not quote or not page_text:
        return res
    q = collapse_ws(quote)
    t = collapse_ws(page_text)
    idx = t.find(q)
    if idx >= 0:
        res.update(verified=True, match="exact", char_start=idx, char_end=idx + len(q), ratio=1.0)
        return res
    # 去连字符/换行断词再试
    q2 = q.replace("- ", "").replace("‐ ", "")
    t2 = t.replace("- ", "").replace("‐ ", "")
    idx = t2.find(q2)
    if idx >= 0:
        res.update(verified=True, match="exact-dehyphen", char_start=idx, char_end=idx + len(q2), ratio=1.0)
        return res
    # 模糊匹配（OCR 漂移等）：滑动窗口比对
    import difflib
    qlen = len(q)
    if qlen < 8:
        return res
    best, best_r = 0, 0.0
    sm = difflib.SequenceMatcher(None, q, t, autojunk=False)
    for m in sm.get_matching_blocks():
        pass
    # 简化：在页文本各句子窗口中找最优
    step = max(4, qlen // 4)
    for start in range(0, max(1, len(t) - qlen), step):
        window = t[start:start + qlen + step]
        r = difflib.SequenceMatcher(None, q, window, autojunk=False).ratio()
        if r > best_r:
            best_r, best = r, start
        if r >= 0.98:
            break
    if best_r >= 0.85:
        res.update(verified=True, match="fuzzy", char_start=best,
                   char_end=best + qlen, ratio=round(best_r, 3))
    else:
        res.update(match="none", ratio=round(best_r, 3))
    return res


# ---------------------------------------------------------------- 引擎 A：PyMuPDF
def extract_fitz(pdf_path, file_id, events, workdir):
    import fitz
    doc = fitz.open(pdf_path)
    if doc.needs_pass:
        if not doc.authenticate(""):
            _ev(events, "failure", pdf_path, "encrypted PDF; empty-password authentication failed")
            return None
        _ev(events, "warning", pdf_path, "encrypted PDF opened with empty password")
    meta = dict(doc.metadata or {})
    rec = {
        "engine": "pymupdf", "pymupdf_version": getattr(fitz, "__version__", None),
        "page_count": doc.page_count,
        "metadata": {k: v for k, v in meta.items() if v},
        "pages": [],
    }
    popup_contents = {}
    for pno in range(doc.page_count):
        page = doc[pno]
        try:
            text = page.get_text("text") or ""
            label = page.get_label()
        except Exception as e:
            _ev(events, "failure", pdf_path, "page %d text read failed: %s" % (pno + 1, e))
            text, label = "", str(pno + 1)
        page_rec = {"page": pno + 1, "page_label": str(label), "text_chars": len(text.strip()),
                    "annotations": [], "_page_text": text}
        # 兼容不同 PyMuPDF 版本：优先 page.annots() 迭代器
        annot_list = []
        try:
            annot_list = list(page.annots())
        except Exception:
            try:
                a0 = page.first_annot
                while a0 is not None:
                    annot_list.append(a0)
                    try:
                        a0 = a0.next
                    except Exception:
                        break
            except Exception:
                annot_list = []
        for seq, annot in enumerate(annot_list, 1):
            try:
                tname = str(annot.type[1])
                key = tname.lower()
                info = annot.info or {}
                a = {
                    "annotation_id": "%s#p%da%d" % (file_id, pno + 1, seq),
                    "xref": annot.xref,
                    "page": pno + 1, "page_label": str(label),
                    "raw_type": tname, "type": TYPE_MAP.get(key, "other"),
                    "rect": [round(v, 1) for v in (annot.rect.x0, annot.rect.y0, annot.rect.x1, annot.rect.y1)],
                    "color_rgb": annot.colors.get("stroke") or annot.colors.get("fill"),
                    "color_hex": rgb_to_hex(annot.colors.get("stroke") or annot.colors.get("fill")),
                    "author": info.get("title") or None,
                    "content": info.get("content") or None,
                    "subject": info.get("subject") or None,
                    "created": pdfdate(info.get("creationDate")),
                    "modified": pdfdate(info.get("modDate")),
                    "opacity": getattr(annot, "opacity", None),
                }
                if key == "popup":
                    parent = None
                    try:
                        t, val = page.xref_get_key(annot.xref, "Parent")
                        if t == "xref":
                            parent = int(val.split()[0])
                    except Exception:
                        pass
                    c = info.get("content")
                    if parent and c:
                        popup_contents[parent] = c
                    continue
                if key in ("highlight", "underline", "squiggly", "strikeout") and getattr(annot, "vertices", None):
                    verts = list(annot.vertices)
                    if verts and isinstance(verts[0], (int, float)):
                        quads = [verts[i:i + 8] for i in range(0, len(verts), 8)]
                    else:  # 点对象序列
                        quads = [[(p.x, p.y) for p in verts[i:i + 4]] for i in range(0, len(verts), 4)]
                    spans = []
                    for q in quads:
                        r = fitz.Quad(q).rect
                        spans.append(page.get_text("text", clip=r).strip())
                    quote = "\n".join(s for s in spans if s)
                    a["quote"] = quote
                    a["quads"] = [[round(v, 1) for v in q] for q in quads]
                    a["method"] = "pymupdf-clip"
                elif key in ("freetext", "text", "caret"):
                    a["note_text"] = info.get("content") or None
                    a["method"] = "annotation-content"
                elif key == "ink":
                    a["handwritten"] = True
                    a["method"] = "ink-region"
                    ink_dir = os.path.join(workdir, "ink_pngs")
                    os.makedirs(ink_dir, exist_ok=True)
                    png = os.path.join(ink_dir, "%s_p%d_a%d.png" % (file_id, pno + 1, seq))
                    try:
                        pix = page.get_pixmap(clip=annot.rect, dpi=200)
                        pix.save(png)
                        a["ink_png"] = os.path.relpath(png, workdir)
                        txt, how = ocr_image(png, events, pdf_path)
                        if txt:
                            a["ocr_text"] = txt
                            a["ocr_method"] = how
                    except Exception as e:
                        _ev(events, "warning", pdf_path, "ink render failed p%d: %s" % (pno + 1, e))
                elif key == "widget":
                    try:
                        a["form_field"] = {"name": annot.widget_name, "value": annot.widget_value,
                                           "field_type": str(annot.widget_field_type)}
                    except Exception:
                        pass
                    a["method"] = "widget"
                else:
                    a["method"] = "annotation-geometry"
                page_rec["annotations"].append(a)
            except Exception as e:
                _ev(events, "failure", pdf_path, "annotation parse failed p%d: %s" % (pno + 1, e))
        # 合并 popup 内容到父批注
        for a in page_rec["annotations"]:
            if a.get("xref") in popup_contents:
                a["content"] = (a.get("content") or "") + ("\n" if a.get("content") else "") + popup_contents[a["xref"]]
        rec["pages"].append(page_rec)
    doc.close()
    return rec


# ---------------------------------------------------------------- 引擎 B：pypdf + poppler
def extract_pypdf(pdf_path, file_id, events, workdir):
    from pypdf import PdfReader
    reader = PdfReader(pdf_path, strict=False)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            pass
        try:
            _ = len(reader.pages)
        except Exception:
            _ev(events, "failure", pdf_path, "encrypted PDF; empty-password decryption failed")
            return None
        _ev(events, "warning", pdf_path, "encrypted PDF opened with empty password")
    try:
        labels = reader.page_labels
    except Exception:
        labels = None
    rec = {
        "engine": "pypdf", "pypdf_version": __import__("pypdf").__version__,
        "page_count": len(reader.pages),
        "metadata": {k: str(v) for k, v in (reader.metadata or {}).items() if v is not None},
        "pages": [],
    }
    for pno, page in enumerate(reader.pages):
        label = labels[pno] if labels and pno < len(labels) else str(pno + 1)
        page_rec = {"page": pno + 1, "page_label": str(label), "text_chars": None,
                    "annotations": []}
        annots = page.get("/Annots", []) or []
        for seq, aref in enumerate(annots, 1):
            try:
                obj = aref.get_object()
                subtype = str(obj.get("/Subtype", ""))
                key = subtype.lower()
                atype = PYPDF_SUBTYPE_MAP.get(subtype, TYPE_MAP.get(key.strip("/"), "other"))
                rect_obj = obj.get("/Rect")
                rect = [float(v) for v in rect_obj] if rect_obj else None
                color = obj.get("/C")
                rgb = [float(v) for v in color] if color else None
                a = {
                    "annotation_id": "%s#p%da%d" % (file_id, pno + 1, seq),
                    "xref": getattr(aref, "idnum", None),
                    "page": pno + 1, "page_label": str(label),
                    "raw_type": subtype.lstrip("/"), "type": atype,
                    "rect": [round(v, 1) for v in rect] if rect else None,
                    "color_rgb": rgb,
                    "color_hex": rgb_to_hex(rgb),
                    "author": str(obj.get("/T")) if obj.get("/T") else None,
                    "content": str(obj.get("/Contents")) if obj.get("/Contents") else None,
                    "subject": str(obj.get("/Subj")) if obj.get("/Subj") else None,
                    "created": pdfdate(obj.get("/CreationDate")),
                    "modified": pdfdate(obj.get("/M")),
                    "opacity": float(obj["/CA"]) if "/CA" in obj else None,
                }
                if subtype == "/Popup":
                    continue
                if subtype in ("/Highlight", "/Underline", "/Squiggly", "/StrikeOut", "/Strikeout"):
                    qp = obj.get("/QuadPoints")
                    if qp and rect:
                        quads = [[float(v) for v in qp[i:i + 8]] for i in range(0, len(qp), 8)]
                        _, _, words, _ = poppler_page_info(pdf_path, pno, events, workdir)
                        spans = []
                        for q in quads:
                            quad_pts = [(q[j], q[j + 1]) for j in range(0, 8, 2)]
                            spans.append(words_in_quad(words, quad_pts))
                        a["quote"] = "\n".join(s for s in spans if s)
                        a["quads"] = [[round(v, 1) for v in q] for q in quads]
                        a["method"] = "pypdf-quad+pdftotext-bbox"
                    else:
                        a["method"] = "annotation-geometry(no-quads)"
                elif subtype in ("/FreeText", "/Text", "/Caret"):
                    a["note_text"] = a["content"]
                    a["method"] = "annotation-content"
                elif subtype == "/Ink":
                    a["handwritten"] = True
                    a["method"] = "ink-region"
                    if rect:
                        ink_dir = os.path.join(workdir, "ink_pngs")
                        os.makedirs(ink_dir, exist_ok=True)
                        png = os.path.join(ink_dir, "%s_p%d_a%d.png" % (file_id, pno + 1, seq))
                        if render_region(pdf_path, pno, rect, png, events=events):
                            a["ink_png"] = os.path.relpath(png, workdir)
                            txt, how = ocr_image(png, events, pdf_path)
                            if txt:
                                a["ocr_text"] = txt
                                a["ocr_method"] = how
                elif subtype == "/Widget":
                    a["form_field"] = {"name": str(obj.get("/T")) if obj.get("/T") else None,
                                       "value": str(obj.get("/V")) if obj.get("/V") else None,
                                       "field_type": str(obj.get("/FT")) if obj.get("/FT") else None}
                    a["method"] = "widget"
                else:
                    a["method"] = "annotation-geometry"
                page_rec["annotations"].append(a)
            except Exception as e:
                _ev(events, "failure", pdf_path, "annotation parse failed p%d: %s" % (pno + 1, e))
        # 文本层长度与锚点校验需要的页文本
        try:
            _, _, _, ptext = poppler_page_info(pdf_path, pno, events, workdir)
            page_rec["text_chars"] = len(ptext.strip())
            page_rec["_page_text"] = ptext
        except Exception:
            page_rec["text_chars"] = None
            page_rec["_page_text"] = ""
        rec["pages"].append(page_rec)
    return rec


# ---------------------------------------------------------------- 引擎 C：poppler-only（无批注枚举能力）
def extract_poppler_only(pdf_path, file_id, events, workdir):
    _ev(events, "warning", pdf_path,
        "no annotation-capable Python library (pymupdf/pypdf); only text inventory available")
    try:
        r = subprocess.run(["pdfinfo", pdf_path], capture_output=True, timeout=60)
        info = r.stdout.decode("utf-8", errors="replace")
        pages = int(re.search(r"Pages:\s*(\d+)", info).group(1)) if re.search(r"Pages:\s*(\d+)", info) else None
    except Exception:
        pages = None
    rec = {"engine": "poppler-only", "page_count": pages, "metadata": {}, "pages": [],
           "annotations_not_extractable": True}
    if pages:
        for pno in range(pages):
            _, _, _, ptext = poppler_page_info(pdf_path, pno, events, workdir)
            rec["pages"].append({"page": pno + 1, "page_label": str(pno + 1),
                                 "text_chars": len(ptext.strip()), "annotations": [],
                                 "_page_text": ptext})
    return rec


# ---------------------------------------------------------------- 库存盘点
def inventory(root, events, follow_case_files=()):
    files = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            st = None
            try:
                st = os.lstat(full)
            except PermissionError:
                _ev(events, "failure", rel, "permission denied (lstat)")
            except OSError as e:
                _ev(events, "failure", rel, "lstat error: %s" % e)
            islink = os.path.islink(full)
            symlink_target = os.readlink(full) if islink else None
            broken = False
            size = None
            sha = None
            if st is not None:
                size = st.st_size
                if islink:
                    if not os.path.exists(full):
                        broken = True
                        _ev(events, "warning", rel, "broken symlink -> %s" % symlink_target)
                    else:
                        try:
                            size = os.path.getsize(full)
                            sha = sha256_file(full, events, rel)
                        except OSError as e:
                            _ev(events, "failure", rel, "symlink target unreadable: %s" % e)
                else:
                    sha = sha256_file(full, events, rel) if size > 0 else "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            ext = os.path.splitext(fn)[1].lower()
            if ext in PDF_EXTS:
                category = "pdf"
            elif ext in TEXT_EXTS:
                category = "text"
            elif ext in HTML_EXTS:
                category = "html"
            elif ext in DOCX_EXTS:
                category = "docx"
            elif ext in IMG_EXTS:
                category = "image"
            else:
                category = "other"
            files.append({
                "file_id": "F%04d" % (len(files) + 1),
                "path": rel,
                "name": fn,
                "ext": ext,
                "category": category,
                "size_bytes": size,
                "sha256": sha,
                "mtime": datetime.datetime.fromtimestamp(st.st_mtime, datetime.timezone.utc).isoformat(timespec="seconds") if st else None,
                "permissions": oct(st.st_mode & 0o777) if st else None,
                "is_symlink": islink,
                "symlink_target": symlink_target,
                "symlink_broken": broken or None,
                "hidden": fn.startswith("."),
            })
    return files


def group_versions(files, events):
    """重复文件（sha256 相同）与版本链（规范化文件名相同）。"""
    by_sha = {}
    for f in files:
        if f["sha256"]:
            by_sha.setdefault(f["sha256"], []).append(f["file_id"])
    dup_groups = {sha: ids for sha, ids in by_sha.items() if len(ids) > 1}
    for sha, ids in dup_groups.items():
        for fid in ids[1:]:
            f = next(x for x in files if x["file_id"] == fid)
            f["is_duplicate_of"] = ids[0]
        _ev(events, "info", None, "duplicate group sha256=%s... files=%s" % (sha[:12], ids))

    def normalize_name(name):
        base = os.path.splitext(name)[0].lower()
        base = RE_VERSION_TOKEN.sub("", base)
        base = re.sub(r"[\s_\-.()（）\[\]{}]+", "", base)
        return base

    by_norm = {}
    for f in files:
        if f["category"] in ("pdf", "docx", "text", "html") and not f["is_symlink"]:
            by_norm.setdefault(normalize_name(f["name"]), []).append(f)
    version_groups = []
    for norm, group in by_norm.items():
        if len(group) > 1 and norm:
            group.sort(key=lambda x: (x["mtime"] or ""))
            vg = {"version_group": "VG%03d" % (len(version_groups) + 1),
                  "normalized_name": norm,
                  "canonical_file": group[0]["file_id"],
                  "members": [{"file_id": g["file_id"], "name": g["name"], "mtime": g["mtime"],
                               "sha256": g["sha256"],
                               "relation": ("canonical" if i == 0 else
                                            "duplicate" if g["sha256"] == group[0]["sha256"] else "version_of")}
                              for i, g in enumerate(group)],
                  "rationale": "normalized filenames equal after stripping version tokens; "
                               "ordered by mtime; sha256 equality checked"}
            version_groups.append(vg)
            for m in vg["members"]:
                fx = next(x for x in files if x["file_id"] == m["file_id"])
                fx["version_group"] = vg["version_group"]
                if m["relation"] == "canonical":
                    fx["is_canonical"] = True
    return dup_groups, version_groups


# ---------------------------------------------------------------- 实体抽取
def extract_entities(text):
    ents = []
    if not text:
        return ents
    for m in RE_DOI.finditer(text):
        ents.append({"type": "doi", "value": m.group(0).rstrip(".)],")})
    for m in RE_ARXIV.finditer(text):
        ents.append({"type": "arxiv", "value": m.group(0)})
    for m in RE_URL.finditer(text):
        ents.append({"type": "url", "value": m.group(0).rstrip(".)]，,")})
    for m in RE_CITATION.finditer(text):
        ents.append({"type": "citation", "value": m.group(0)})
    for m in RE_QUANTITY.finditer(text):
        ents.append({"type": "quantity", "value": m.group(0)})
    low = text.lower()
    for kw in METHOD_LEXICON:
        if re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", low):
            ents.append({"type": "method", "value": kw})
    # 去重
    seen, out = set(), []
    for e in ents:
        k = (e["type"], e["value"].lower())
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


def classify_category(text):
    low = (text or "").lower()
    for cat, kws in CATEGORY_RULES:
        for kw in kws:
            if kw in low:
                return cat
    return DEFAULT_CATEGORY


# ---------------------------------------------------------------- 笔记结构化
def build_notes(files, pdf_records, stray_note_files, events):
    notes = []
    for f in files:
        rec = pdf_records.get(f["file_id"])
        if not rec:
            continue
        for page in rec.get("pages", []):
            ptext = page.get("_page_text")
            for a in page.get("annotations", []):
                if a.get("type") in ("popup", "other") and not a.get("quote") and not a.get("content"):
                    continue
                note_id = "N%04d" % (len(notes) + 1)
                quote = a.get("quote")
                user_text = a.get("content") or a.get("note_text") or a.get("ocr_text")
                verified = a.get("_anchor", {})
                if user_text and quote:
                    claim = user_text.strip()
                    category = classify_category(user_text) if classify_category(user_text) != DEFAULT_CATEGORY else classify_category(quote)
                elif user_text:
                    claim = user_text.strip()
                    category = classify_category(user_text)
                elif quote:
                    q_flat = collapse_ws(quote)
                    claim = (q_flat[:237] + "…") if len(q_flat) > 240 else q_flat
                    category = classify_category(quote)
                else:
                    claim = "[%s mark without recoverable text]" % a.get("type")
                    category = DEFAULT_CATEGORY
                ent_text = " ".join(x for x in [quote, user_text] if x)
                entities = extract_entities(ent_text)
                tags = sorted({e["value"] for e in entities if e["type"] == "method"})
                notes.append({
                    "note_id": note_id,
                    "category": category,
                    "claim": claim,
                    "source": {
                        "file_id": f["file_id"],
                        "file_name": f["name"],
                        "file_sha256": f["sha256"],
                        "page": a.get("page"),
                        "page_label": a.get("page_label"),
                        "char_start": verified.get("char_start"),
                        "char_end": verified.get("char_end"),
                        "quote": quote,
                        "bbox": a.get("rect"),
                        "annotation_id": a.get("annotation_id"),
                        "anchor_verified": verified.get("verified"),
                        "anchor_match": verified.get("match"),
                    },
                    "annotation_type": a.get("type"),
                    "color_hex": a.get("color_hex"),
                    "author": a.get("author"),
                    "created": a.get("created"),
                    "modified": a.get("modified"),
                    "tags": tags,
                    "entities": entities,
                    "confidence": a.get("confidence"),
                    "derived_from": "pdf_annotation",
                })
    # 散落笔记文件（用户写在别处的句子）：逐行结构化，解析来源引用
    for sf in stray_note_files:
        f = sf["file_record"]
        for i, line in enumerate(sf["lines"], 1):
            if not line.strip():
                continue
            ref = RE_SOURCE_REF.search(line)
            src_file_id = None
            src_page = None
            if ref:
                name_guess = ref.group(1).strip()
                src_page = next((g for g in ref.groups()[1:] if g), None)
                for cand in files:
                    if cand["name"] == os.path.basename(name_guess) or cand["name"] == name_guess:
                        src_file_id = cand["file_id"]
                        break
            note_id = "N%04d" % (len(notes) + 1)
            entities = extract_entities(line)
            notes.append({
                "note_id": note_id,
                "category": classify_category(line),
                "claim": line.strip()[:500],
                "source": {
                    "file_id": src_file_id or f["file_id"],
                    "file_name": (next((c["name"] for c in files if c["file_id"] == src_file_id), None)
                                  if src_file_id else f["name"]),
                    "file_sha256": (next((c["sha256"] for c in files if c["file_id"] == src_file_id), None)
                                    if src_file_id else f["sha256"]),
                    "page": int(src_page) if src_page else None,
                    "line": i,
                    "note_file": f["name"],
                    "anchor_verified": None,
                    "anchor_match": "reference-parsed" if ref else "no-source-ref",
                },
                "annotation_type": "stray_note",
                "tags": sorted({e["value"] for e in entities if e["type"] == "method"}),
                "entities": entities,
                "confidence": 0.7 if ref else 0.5,
                "derived_from": "note_file",
            })
            if ref and not src_file_id:
                _ev(events, "warning", f["name"], "line %d references unknown file '%s'" % (i, name_guess))
    return notes


# ---------------------------------------------------------------- 关联与版本归并
def build_links(notes, files, version_groups, events, max_per_node=12):
    links = []
    # 实体文档频率
    df = {}
    for n in notes:
        for e in n.get("entities", []):
            key = (e["type"], e["value"].lower())
            df[key] = df.get(key, 0) + 1
    notes_with_ents = [(n, {(e["type"], e["value"].lower()): e["value"] for e in n.get("entities", [])}) for n in notes]
    for i in range(len(notes_with_ents)):
        n1, e1 = notes_with_ents[i]
        for j in range(i + 1, len(notes_with_ents)):
            n2, e2 = notes_with_ents[j]
            if not e1 or not e2:
                continue
            shared = set(e1) & set(e2)
            if not shared:
                continue
            same_ref = any(k[0] in ("doi", "arxiv") for k in shared)
            distinctive = [k for k in shared if df.get(k, 99) <= 3 and k[0] not in ("url",)]
            same_doc = n1["source"].get("file_id") == n2["source"].get("file_id")
            if same_ref:
                ltype, strength = "same_reference", 1.0
            elif len(distinctive) >= 1 or len(shared) >= 2:
                ltype, strength = "related", min(1.0, 0.4 + 0.2 * len(shared))
            elif same_doc:
                ltype, strength = "co_mention", 0.3
            else:
                continue
            links.append({
                "link_id": "L%04d" % (len(links) + 1),
                "src": n1["note_id"], "dst": n2["note_id"],
                "type": ltype, "strength": round(strength, 2),
                "rationale": ("shared entity %s" % ", ".join(sorted({"%s:%s" % (k[0], e1[k]) for k in shared}))
                              + ("; same file" if same_doc else "")),
                "shared_entities": sorted({e1[k] for k in shared}),
            })
    # 控制过度连接：每节点保留最强 k 条
    by_node = {}
    for l in links:
        by_node.setdefault(l["src"], []).append(l)
        by_node.setdefault(l["dst"], []).append(l)
    dropped = 0
    keep = set()
    for node, ls in by_node.items():
        ls.sort(key=lambda x: -x["strength"])
        for l in ls[:max_per_node]:
            keep.add(l["link_id"])
    links = [l for l in links if l["link_id"] in keep]
    dropped = sum(len(v) for v in by_node.values()) - len(keep) * 1
    if dropped > 0:
        _ev(events, "info", None, "link pruning: dropped %d weak links beyond per-node cap %d" % (dropped, max_per_node))

    # 文件级版本/重复关联
    file_links = []
    for vg in version_groups:
        canon = vg["canonical_file"]
        for m in vg["members"]:
            if m["file_id"] == canon:
                continue
            same = m["sha256"] == next(x["sha256"] for x in vg["members"] if x["file_id"] == canon)
            file_links.append({
                "link_id": "FL%04d" % (len(file_links) + 1),
                "src": m["file_id"], "dst": canon,
                "type": "same_content_duplicate" if same else "version_of",
                "strength": 1.0 if same else 0.8,
                "rationale": ("sha256 identical" if same else
                              "filename normalized equal (version tokens stripped); "
                              "content differs -> treated as another version, annotations merged below"),
            })
    # 跨版本批注归并：同版本组内相同规范化引文的批注聚成簇
    clusters = {}
    for n in notes:
        q = norm_quote(n["source"].get("quote"))
        vg = next((f.get("version_group") for f in files if f["file_id"] == n["source"].get("file_id")), None)
        if q and len(q) >= 10 and vg:
            clusters.setdefault((vg, q), []).append(n["note_id"])
    cluster_map = {}
    multi = [(vg, q, ids) for (vg, q), ids in clusters.items() if len(ids) > 1]
    for i, (vg, q, ids) in enumerate(multi, 1):
        cid = "C%03d" % i
        for nid in ids:
            cluster_map[nid] = cid
    return links, file_links, cluster_map


# ---------------------------------------------------------------- SQLite 索引
def build_index(outdir, files, notes, links, file_links, events):
    db = os.path.join(outdir, "index.sqlite3")
    if os.path.exists(db):
        os.remove(db)
    con = sqlite3.connect(db)
    cur = con.cursor()
    cur.executescript("""
    CREATE TABLE files(file_id TEXT PRIMARY KEY, path TEXT, name TEXT, ext TEXT, category TEXT,
        size_bytes INTEGER, sha256 TEXT, mtime TEXT, permissions TEXT,
        is_symlink INTEGER, symlink_target TEXT, is_duplicate_of TEXT, version_group TEXT);
    CREATE TABLE annotations(annotation_id TEXT, note_id TEXT PRIMARY KEY, file_id TEXT, file_name TEXT,
        page INTEGER, page_label TEXT, annotation_type TEXT, category TEXT, color_hex TEXT, author TEXT,
        created TEXT, modified TEXT,
        quote TEXT, char_start INTEGER, char_end INTEGER, bbox TEXT, anchor_verified INTEGER,
        anchor_match TEXT, confidence REAL, cluster_id TEXT);
    CREATE TABLE links(link_id TEXT PRIMARY KEY, src TEXT, dst TEXT, type TEXT, strength REAL, rationale TEXT);
    CREATE TABLE file_links(link_id TEXT PRIMARY KEY, src TEXT, dst TEXT, type TEXT, strength REAL, rationale TEXT);
    CREATE TABLE events(ts TEXT, level TEXT, event TEXT, file TEXT, detail TEXT);
    """)
    fts_ok = False
    try:
        cur.execute("CREATE VIRTUAL TABLE notes_fts USING fts5(claim, quote, tags)")
        fts_ok = True
    except sqlite3.Error as e:
        _ev(events, "warning", None, "FTS5 unavailable (%s); LIKE fallback will be used" % e)
    for f in files:
        cur.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f["file_id"], f["path"], f["name"], f["ext"], f["category"], f["size_bytes"],
                     f["sha256"], f["mtime"], f["permissions"], int(bool(f["is_symlink"])),
                     f["symlink_target"], f.get("is_duplicate_of"), f.get("version_group")))
    for n in notes:
        s = n["source"]
        cur.execute("INSERT INTO annotations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (s.get("annotation_id"), n["note_id"], s.get("file_id"), s.get("file_name"),
                     s.get("page"), s.get("page_label"), n.get("annotation_type"), n.get("category"),
                     n.get("color_hex"), n.get("author"), n.get("created"), n.get("modified"),
                     s.get("quote"), s.get("char_start"),
                     s.get("char_end"), json.dumps(s.get("bbox"), ensure_ascii=False),
                     None if s.get("anchor_verified") is None else int(bool(s.get("anchor_verified"))),
                     s.get("anchor_match"), n.get("confidence"),
                     n.get("cluster_id")))
        if fts_ok:
            cur.execute("INSERT INTO notes_fts(rowid, claim, quote, tags) VALUES(?,?,?,?)",
                        (int(n["note_id"][1:]), n["claim"], s.get("quote") or "",
                         " ".join(n.get("tags") or [])))
    for l in links:
        cur.execute("INSERT INTO links VALUES(?,?,?,?,?,?)",
                    (l["link_id"], l["src"], l["dst"], l["type"], l["strength"], l["rationale"]))
    for l in file_links:
        cur.execute("INSERT INTO file_links VALUES(?,?,?,?,?,?)",
                    (l["link_id"], l["src"], l["dst"], l["type"], l["strength"], l["rationale"]))
    for e in events:
        cur.execute("INSERT INTO events VALUES(?,?,?,?,?)",
                    (e.get("ts"), e.get("level"), e.get("event"), e.get("file"), e.get("detail")))
    con.commit()
    return db, fts_ok


# ---------------------------------------------------------------- 预览渲染
TYPE_COLORS = {
    "highlight": "#ffff00", "underline": "#00aa00", "squiggly": "#ff8800",
    "strikeout": "#ff0000", "margin_note": "#0088ff", "sticky_note": "#cc00cc",
    "handwritten_mark": "#994400", "form_field": "#666666", "shape": "#888888",
    "line_mark": "#00aaaa", "stamp": "#000000", "caret": "#0000aa",
    "attachment": "#555555", "redaction": "#000000", "media": "#aa00aa", "other": "#999999",
}


def render_previews(pdf_path, file_id, rec, outdir, events, max_pages=40):
    if rec.get("engine") not in ("pymupdf", "pypdf"):
        return []
    pages = [p for p in rec.get("pages", []) if p.get("annotations")]
    if not pages:
        return []
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        _ev(events, "warning", pdf_path, "PIL unavailable; previews skipped")
        return []
    prev_dir = os.path.join(outdir, "previews", file_id)
    os.makedirs(prev_dir, exist_ok=True)
    made = []
    for p in pages[:max_pages]:
        pno = p["page"] - 1
        png = os.path.join(prev_dir, "page-%s.png" % p.get("page_label", p["page"]))
        prefix = png[:-4]
        ok = False
        try:
            subprocess.run(["pdftoppm", "-png", "-f", str(pno + 1), "-l", str(pno + 1),
                            "-r", "110", pdf_path, prefix],
                           check=True, capture_output=True, timeout=120)
            produced = "%s-%d.png" % (prefix, pno + 1)
            if os.path.exists(produced) and produced != png:
                os.replace(produced, png)
            ok = os.path.exists(png)
        except Exception as e:
            _ev(events, "warning", pdf_path, "page render failed p%d: %s" % (p["page"], e))
        if not ok:
            continue
        try:
            img = Image.open(png).convert("RGB")
            _, _, words, _ = poppler_page_info(pdf_path, pno, events, outdir)
            pm_w, pm_h = img.size
            width = height = None
            tmp_html = os.path.join(outdir, "_pg.html")
            # 用 bbox 头拿页尺寸（pt）
            tmp_html = os.path.join(outdir, "_pg.html")
            subprocess.run(["pdftotext", "-f", str(pno + 1), "-l", str(pno + 1), "-bbox", pdf_path, tmp_html],
                           check=True, capture_output=True, timeout=120)
            with open(tmp_html, encoding="utf-8", errors="replace") as fh:
                m = PAGE_RE_BBOX.search(fh.read())
            if m:
                width, height = float(m.group(1)), float(m.group(2))
            sx, sy = pm_w / width, pm_h / height
            dr = ImageDraw.Draw(img)
            for a in p["annotations"]:
                if not a.get("rect"):
                    continue
                col = TYPE_COLORS.get(a.get("type"), "#999999")
                x0, y0, x1, y1 = [a["rect"][0] * sx, a["rect"][1] * sy, a["rect"][2] * sx, a["rect"][3] * sy]
                dr.rectangle([x0, y0, x1, y1], outline=col, width=3)
                lbl = "%s%s" % (a.get("type", "?"), ":" + (a.get("content") or "")[:24] if a.get("content") else "")
                try:
                    dr.text((x0, max(0, y0 - 12)), lbl, fill=col)
                except Exception:
                    pass
            img.save(png)
            made.append(os.path.relpath(png, outdir))
        except Exception as e:
            _ev(events, "warning", pdf_path, "preview overlay failed p%d: %s" % (p["page"], e))
        finally:
            try:
                os.remove(tmp_html)
            except OSError:
                pass
    if len(pages) > max_pages:
        _ev(events, "info", pdf_path, "previews limited to first %d annotated pages of %d" % (max_pages, len(pages)))
    return made


# ---------------------------------------------------------------- 非 PDF 文本源
def read_text_source(path, events):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in DOCX_EXTS:
            import zipfile
            with zipfile.ZipFile(path) as z:
                xml = z.read("word/document.xml").decode("utf-8", errors="replace")
            xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
            xml = re.sub(r"</w:p>", "\n", xml)
            return re.sub(r"<[^>]+>", "", xml)
        if ext in HTML_EXTS:
            with open(path, encoding="utf-8", errors="replace") as f:
                t = f.read()
            t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", t, flags=re.S | re.I)
            t = re.sub(r"<[^>]+>", " ", t)
            return html.unescape(t)
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception as e:
        _ev(events, "failure", path, "text source read failed: %s" % e)
        return None


# ---------------------------------------------------------------- 输入定位
def load_case(case_path, events):
    case = {}
    if case_path and os.path.exists(case_path):
        try:
            with open(case_path, encoding="utf-8") as f:
                case = json.load(f)
        except Exception as e:
            _ev(events, "failure", case_path, "case.json parse failed: %s" % e)
    return case


def find_materials(case, explicit, events):
    if explicit:
        if os.path.isdir(explicit):
            return os.path.abspath(explicit)
        _ev(events, "failure", explicit, "--materials dir does not exist")
    candidates = []
    for key in ("materials_dir", "materials", "data_dir", "input_dir", "files_dir", "workdir_materials"):
        v = case.get(key)
        if isinstance(v, str):
            candidates.append(v)
        elif isinstance(v, list):
            candidates.extend(x for x in v if isinstance(x, str) and os.path.isdir(x))
    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and os.path.isdir(v) and k not in ("case_id",):
                    candidates.append(v)
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
    walk(case)
    for c in candidates:
        c = c if os.path.isabs(c) else c
        if os.path.isdir(c):
            return os.path.abspath(c)
    for d in ("/input/materials", "/input/files", "/input/data", "/input", "./materials", "./input/materials"):
        if os.path.isdir(d):
            if d == "/input" and not any(os.scandir(d)):
                continue
            return os.path.abspath(d)
    return None


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="annotation-knowledge-sink pipeline v%s" % PIPELINE_VERSION)
    ap.add_argument("--case", default="/input/case.json")
    ap.add_argument("--materials", default=None)
    ap.add_argument("--workdir", default="/app/work")
    ap.add_argument("--outdir", default="/app/output")
    ap.add_argument("--engine", default="auto", choices=["auto", "fitz", "pypdf"])
    ap.add_argument("--no-previews", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    events = []
    os.makedirs(args.workdir, exist_ok=True)
    os.makedirs(args.outdir, exist_ok=True)
    _ev(events, "info", None, "pipeline v%s start: argv=%s" % (PIPELINE_VERSION, " ".join(sys.argv)))

    script_sha = sha256_file(SCRIPT_PATH) if os.path.exists(SCRIPT_PATH) else None
    case = load_case(args.case, events)
    case_id = (case.get("case_id") or case.get("id") or case.get("caseId")
               or os.environ.get("CASE_ID") or "unknown-case")
    materials = find_materials(case, args.materials, events)
    if not materials:
        _ev(events, "failure", None, "no materials directory found")
        result = {
            "case_id": case_id, "status": "failed",
            "generated_at": now_iso(), "pipeline_version": PIPELINE_VERSION,
            "failures": [e for e in events if e["level"] == "failure"],
            "error": "materials directory not found; checked case.json keys and /input* defaults",
        }
        with open(os.path.join(args.outdir, "result.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        return 2

    engine = detect_engine(args.engine, events)
    if not engine:
        _ev(events, "failure", None, "no PDF engine available (pymupdf/pypdf/poppler)")
    _ev(events, "info", None, "materials=%s engine=%s tesseract=%s" % (materials, engine, has_tesseract()))

    # 1. 库存
    files = inventory(materials, events)
    files = [f for f in files if os.path.basename(f["path"]) not in ("case.json", "task.md")]
    dup_groups, version_groups = group_versions(files, events)
    with open(os.path.join(args.outdir, "inventory.json"), "w", encoding="utf-8") as f:
        json.dump({"materials_dir": materials, "case_id": case_id, "files": files,
                   "duplicate_groups": {k: v for k, v in dup_groups.items()},
                   "version_groups": version_groups}, f, ensure_ascii=False, indent=2)

    # 2. 批注提取
    pdf_records = {}
    for f in files:
        if f["category"] != "pdf" or f["is_symlink"] or f["symlink_broken"]:
            continue
        full = os.path.join(materials, f["path"])
        try:
            if engine == "fitz":
                rec = extract_fitz(full, f["file_id"], events, args.workdir)
            elif engine == "pypdf":
                rec = extract_pypdf(full, f["file_id"], events, args.workdir)
            else:
                rec = extract_poppler_only(full, f["file_id"], events, args.workdir)
            if rec is None:
                f["processing_status"] = "failed"
                continue
            f["processing_status"] = "ok"
            f["page_count"] = rec.get("page_count")
            f["annotation_count"] = sum(len(p.get("annotations", [])) for p in rec.get("pages", []))
            for p in rec.get("pages", []):
                if p.get("text_chars") is not None and p["text_chars"] < 20:
                    p["no_text_layer"] = True
                    _ev(events, "warning", f["path"], "page %s has no text layer (scanned); anchors on this page are OCR/low-confidence" % p.get("page_label"))
            pdf_records[f["file_id"]] = rec
        except Exception as e:
            f["processing_status"] = "failed"
            _ev(events, "failure", f["path"],
                "PDF processing failed: %s\n%s" % (e, traceback.format_exc()[-600:]))

    # 3. 锚点校验 + 置信度
    for f in files:
        rec = pdf_records.get(f["file_id"])
        if not rec:
            continue
        for p in rec.get("pages", []):
            ptext = p.get("_page_text")
            for a in p.get("annotations", []):
                if a.get("quote"):
                    v = verify_anchor(a["quote"], ptext or "", a.get("method", ""))
                    a["_anchor"] = v
                    if v["verified"] and v["match"] == "exact":
                        a["confidence"] = 1.0
                    elif v["verified"]:
                        a["confidence"] = 0.8
                    else:
                        a["confidence"] = 0.3
                        _ev(events, "warning", f["path"],
                            "anchor NOT verified: p%s type=%s quote=%r" % (a.get("page_label"), a.get("type"), a.get("quote")[:80]))
                elif a.get("ocr_text"):
                    a["confidence"] = 0.4
                elif a.get("note_text"):
                    a["confidence"] = 0.9
                elif a.get("type") == "form_field":
                    a["confidence"] = 0.9
                else:
                    a["confidence"] = 0.2

    # 4. 散落笔记文件
    stray = []
    for f in files:
        if f["category"] in ("text", "docx", "html") and re.search(
                r"(note|笔记|批注|reading|摘抄|scratch|clip|剪藏|webclip)", f["name"], re.I):
            txt = read_text_source(os.path.join(materials, f["path"]), events)
            if txt:
                stray.append({"file_record": f, "lines": txt.splitlines()})
                f["processing_status"] = "parsed-as-note-file"

    # 5. 笔记
    notes = build_notes(files, pdf_records, stray, events)

    # 6. 关联 + 版本归并簇
    links, file_links, cluster_map = build_links(notes, files, version_groups, events)
    for n in notes:
        if n["note_id"] in cluster_map:
            n["cluster_id"] = cluster_map[n["note_id"]]
    with open(os.path.join(args.outdir, "notes.json"), "w", encoding="utf-8") as fh:
        json.dump(notes, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(args.outdir, "links.json"), "w", encoding="utf-8") as fh:
        json.dump({"note_links": links, "file_links": file_links}, fh, ensure_ascii=False, indent=2)

    # 7. 索引
    db, fts_ok = build_index(args.outdir, files, notes, links, file_links, events)

    # 8. 预览
    previews = []
    if not args.no_previews:
        for f in files:
            rec = pdf_records.get(f["file_id"])
            if rec and f.get("annotation_count"):
                full = os.path.join(materials, f["path"])
                previews.extend(render_previews(full, f["file_id"], rec, args.outdir, events))

    # 9. 校验汇总
    total_ann = verified = 0
    for f in files:
        rec = pdf_records.get(f["file_id"])
        if not rec:
            continue
        for p in rec.get("pages", []):
            for a in p.get("annotations", []):
                if a.get("quote"):
                    total_ann += 1
                    if a.get("_anchor", {}).get("verified"):
                        verified += 1
    verification = {
        "annotations_with_quote": total_ann,
        "anchors_verified": verified,
        "anchor_verification_rate": round(verified / total_ann, 3) if total_ann else None,
        "notes": len(notes),
        "note_links": len(links),
        "file_links": len(file_links),
        "merged_annotation_clusters": len(set(cluster_map.values())) if cluster_map else 0,
        "engine": engine, "fts5": fts_ok,
        "ocr_available": bool(has_tesseract()),
        "elapsed_seconds": round(time.time() - t0, 1),
    }

    # 10. 日志与交付清单
    with open(os.path.join(args.outdir, "run.log"), "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    failures = [e for e in events if e["level"] == "failure"]
    warnings = [e for e in events if e["level"] == "warning"]

    deliverables = {
        "result_json": "result.json",
        "inventory": "inventory.json",
        "notes": "notes.json",
        "links": "links.json",
        "index": "index.sqlite3",
        "run_log": "run.log",
        "verification": "verification_report.md",
        "digest": "digest.md",
        "previews_dir": "previews/" if previews else None,
    }

    # 11. result.json（含 output_contract 全字段映射草稿）
    contract = case.get("output_contract") or case.get("outputs_contract") or case.get("outputs") or {}
    contract_filled = {}
    if isinstance(contract, dict):
        for k, v in contract.items():
            contract_filled[k] = {"status": "pending_agent_mapping", "contract_spec": v}
    elif isinstance(contract, list):
        for k in contract:
            contract_filled[str(k)] = {"status": "pending_agent_mapping"}

    status = "success"
    if failures:
        status = "partial" if any(f.get("processing_status") == "ok" for f in files) or notes else "failed"
    result = {
        "case_id": case_id,
        "status": status,
        "generated_at": now_iso(),
        "pipeline": {
            "name": "annotation-knowledge-sink",
            "version": PIPELINE_VERSION,
            "script_sha256": script_sha,
            "command": " ".join(sys.argv),
            "engine": engine,
            "pymupdf_available": engine == "fitz",
            "pypdf_version": __import__("pypdf").__version__ if engine == "pypdf" else None,
            "fts5": fts_ok, "ocr": bool(has_tesseract()),
        },
        "inputs": {
            "case_json": os.path.abspath(args.case) if os.path.exists(args.case) else None,
            "task_md": "/input/task.md",
            "materials_dir": materials,
            "file_count": len(files),
            "pdf_count": sum(1 for f in files if f["category"] == "pdf"),
        },
        "summary": {
            "files": len(files),
            "pdfs_processed": sum(1 for f in files if f.get("processing_status") == "ok"),
            "pdfs_failed": sum(1 for f in files if f.get("processing_status") == "failed"),
            "annotations": sum(f.get("annotation_count") or 0 for f in files),
            "notes": len(notes),
            "note_links": len(links),
            "file_version_links": len(file_links),
            "duplicate_groups": len(dup_groups),
            "version_groups": len(version_groups),
        },
        "verification": verification,
        "deliverables": {k: v for k, v in deliverables.items() if v},
        "output_contract": contract_filled or {"status": "no_output_contract_in_case_json"},
        "sources": [{"file_id": f["file_id"], "path": f["path"], "sha256": f["sha256"],
                     "size_bytes": f["size_bytes"], "permissions": f["permissions"]} for f in files],
        "uncertainty": [
            "批注颜色(color_hex)仅忠实记录 RGB 值；其既有含义（如黄色=重要）未在材料中给出图例，需使用者自行映射。" if any(
                f.get("annotation_count") for f in files) else "未发现批注。",
            "无文本层扫描页的锚点依赖 OCR 或几何位置，置信度较低（见 notes.json 的 confidence 字段）。",
            "手写批注(Ink)经区域渲染 + OCR 识别，准确率受扫描质量限制。" if any(
                any(a.get("type") == "handwritten_mark" for p in (pdf_records.get(f["file_id"]) or {}).get("pages", []) for a in p.get("annotations", []))
                for f in files) else "未发现手写批注。",
            "版本分组基于文件名规范化 + sha256 判定，未做全文相似度比对，可能漏掉改名较大的版本。",
        ],
        "failures": failures,
        "warnings": warnings[:50],
        "changes": events[-20:],
        "reproduction": {
            "rerun": "python3 /skills/weekly-skill/scripts/pipeline.py --case %s --materials %s --workdir /app/work --outdir /app/output" % (args.case, materials),
            "search_example": "python3 /skills/weekly-skill/scripts/search.py --index %s --q '<query>' --format md" % db,
            "expected_determinism": "同一输入文件与同一引擎下输出仅时间戳不同；引擎/库版本记录在 pipeline 字段。",
        },
    }
    # 合并策略：若 /app/output/result.json 已存在且由另一条工作流（文档库整理）产出，
    # 则把本次批注工作流结果挂到其 "annotations_workflow" 键下，保留原契约映射；
    # 同时始终写出独立的 result_annotations.json 便于核对。
    result_path = os.path.join(args.outdir, "result.json")
    merged = False
    if os.path.exists(result_path):
        try:
            with open(result_path, encoding="utf-8") as f:
                existing = json.load(f)
            if isinstance(existing, dict) and existing.get("pipeline", {}).get("name") != "annotation-knowledge-sink":
                existing["annotations_workflow"] = result
                if existing.get("case_id") in (None, "unknown") and case_id:
                    existing["case_id"] = case_id
                if existing.get("status") == "success" and status != "success":
                    existing["status"] = "partial"
                existing.setdefault("uncertainty", [])
                # 回填契约中批注/笔记/检索类字段的自动交付路径
                A_TERMS = ("annot", "highlight", "note", "index", "search", "retriev",
                           "批注", "笔记", "检索", "索引", "高亮")
                A_MAP = [
                    (("annot", "highlight", "批注", "高亮"),
                     {"deliverable": "notes.json + index.sqlite3 的 annotations 表",
                      "count": verification["annotations_with_quote"],
                      "anchor_verification_rate": verification["anchor_verification_rate"]}),
                    (("note", "笔记"), {"deliverable": "notes.json", "count": len(notes)}),
                    (("index", "search", "retriev", "检索", "索引"),
                     {"deliverable": "index.sqlite3（FTS5）+ search.py；召回报告 retrieval_report.md"}),
                ]
                contract = existing.get("output_contract")
                if isinstance(contract, dict):
                    for k, entry in contract.items():
                        if not isinstance(entry, dict) or entry.get("delivered") not in (None, "", {}):
                            continue
                        kl = str(k).lower()
                        for terms, fill in A_MAP:
                            if any(t in kl for t in terms):
                                entry["delivered"] = fill
                                entry["note"] = "由批注工作流（annotations_workflow）自动交付"
                                break
                result = existing
                merged = True
        except Exception as e:
            _ev(events, "warning", None, "result.json merge skipped: %s" % e)
    with open(os.path.join(args.outdir, "result_annotations.json"), "w", encoding="utf-8") as f:
        json.dump(result if not merged else result["annotations_workflow"], f, ensure_ascii=False, indent=2)
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # 12. 人类可读摘要与校验报告
    lines = ["# 校验报告 (verification_report)", "",
             "- case_id: %s" % case_id,
             "- status: %s" % status,
             "- engine: %s | FTS5: %s | OCR: %s" % (engine, fts_ok, bool(has_tesseract())),
             "- 锚点校验: %d/%d 条带引文批注验证通过 (rate=%s)" % (
                 verified, total_ann, verification["anchor_verification_rate"]),
             "- 笔记: %d | 笔记关联: %d | 文件版本/重复关联: %d | 跨版本批注簇: %s" % (
                 len(notes), len(links), len(file_links), verification["merged_annotation_clusters"]),
             "- 用时: %ss" % verification["elapsed_seconds"], "",
             "## 失败 (%d)" % len(failures), ""]
    lines += ["- `%s` %s — %s" % (e.get("ts"), e.get("file"), e.get("detail")) for e in failures] or ["- 无"]
    lines += ["", "## 警告 (%d，截断至 50)" % len(warnings), ""]
    lines += ["- `%s` %s — %s" % (e.get("ts"), e.get("file"), e.get("detail")) for e in warnings[:50]] or ["- 无"]
    with open(os.path.join(args.outdir, "verification_report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    dl = ["# 结果摘要 (digest)", "",
          "case_id: %s | status: %s | %s" % (case_id, status, now_iso()), "",
          "## 文件", "",
          "| file_id | 文件 | 类型 | 大小 | sha256(前12) | 批注数 | 状态 |", "|-|-|-|-|-:|-|-:|-|"]
    for f in files:
        dl.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            f["file_id"], f["path"], f["category"], f["size_bytes"],
            (f["sha256"] or "")[:12], f.get("annotation_count", ""), f.get("processing_status", "")))
    dl += ["", "## 笔记（前 40 条）", "",
           "| note | 类别 | 论断(截断) | 来源 |", "|-|-|-|-|"]
    for n in notes[:40]:
        s = n["source"]
        dl.append("| %s | %s | %s | %s p.%s a=%s |" % (
            n["note_id"], n["category"], n["claim"][:60].replace("|", "/"),
            s.get("file_name"), s.get("page_label"), s.get("anchor_verified")))
    if len(notes) > 40:
        dl.append("\n… 共 %d 条，详见 notes.json" % len(notes))
    with open(os.path.join(args.outdir, "digest.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(dl))

    print(json.dumps({"status": status, "case_id": case_id, "summary": result["summary"],
                      "verification_rate": verification["anchor_verification_rate"],
                      "elapsed_s": verification["elapsed_seconds"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
