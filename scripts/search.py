#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
annotation-knowledge-sink (aks) 检索入口
=========================================
对 pipeline 产出的 index.sqlite3 执行检索，支持：

  --q          全文关键词（FTS5，失败自动回退 LIKE）
  --category   按笔记类别（question/method/conclusion/doubt/reusable/keypoint）
  --tag        按标签（方法词表实体）
  --entity     按实体值（doi/arxiv/method/quantity/citation/url 的精确值）
  --source     按来源文件名（模糊）
  --page       按页码
  --since/--until  按批注创建时间（ISO 日期）
  --topk       返回条数（默认 20）
  --format     json | md
  --explain    对每条结果给出命中理由（召回依据）
  --report PATH  将一组 --demo-queries 的检索结果写成召回报告（Markdown），
                 报告中同时标注召回不足（结果数 < topk/2）与可能误召回（低置信度）

示例：
  python3 search.py --index /app/output/index.sqlite3 --q "machine learning" --format md --explain
  python3 search.py --index /app/output/index.sqlite3 --category method --since 2024-01-01
  python3 search.py --index /app/output/index.sqlite3 --report /app/output/retrieval_report.md \
      --demo-queries "体系的动力学模拟用了什么方法" "作者如何验证模型的正确性"
"""
import argparse
import datetime
import json
import os
import sqlite3
import sys

FTS_STOPWORDS = {"的", "了", "和", "是", "在", "与", "or", "and", "the", "a", "of", "to", "如何", "什么", "怎么"}


def tokenize(q):
    toks = []
    for t in q.replace('"', " ").split():
        if t.lower() in FTS_STOPWORDS:
            continue
        toks.append(t)
    return toks


def cjk_tokens(q):
    """CJK 感知分词：拉丁/数字按词，中文按二元组（单字保留）。"""
    import re as _re
    toks = []
    for part in _re.findall(r"[A-Za-z0-9]+|[一-鿿]+", q):
        if _re.match(r"[一-鿿]", part):
            if len(part) == 1:
                toks.append(part)
            else:
                toks.extend(part[i:i + 2] for i in range(len(part) - 1))
        else:
            toks.append(part.lower())
    return [t for t in toks if t.lower() not in FTS_STOPWORDS]


def like_query(cur, q, topk):
    toks = cjk_tokens(q)
    if not toks:
        return []
    cond = " OR ".join("(claim LIKE ? OR quote LIKE ? OR tags LIKE ?)" for _ in toks)
    args = []
    for t in toks:
        args += ["%" + t + "%"] * 3
    rows = cur.execute(
        "SELECT rowid, claim, quote, tags FROM notes_fts WHERE %s LIMIT 2000" % cond, args).fetchall()
    scored = []
    for r in rows:
        blob = " ".join(x or "" for x in r[1:]).lower()
        score = sum(1 for t in toks if t.lower() in blob)
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return [(r[0], {"claim": r[1], "quote": r[2], "tags": r[3], "snippet": None,
                    "score": score, "match": "cjk-bigram-like"}) for score, r in scored[:topk]]


def fts_query(cur, q, topk):
    toks = tokenize(q)
    if not toks:
        return []
    phrase = " OR ".join('"%s"' % t.replace('"', "") for t in toks)
    try:
        rows = cur.execute(
            "SELECT n.rowid, n.claim, n.quote, n.tags, snippet(notes_fts, 0, '[', ']', '…', 12) AS snip, "
            "bm25(notes_fts) AS score FROM notes_fts n WHERE notes_fts MATCH ? ORDER BY score LIMIT ?",
            (phrase, topk)).fetchall()
        return [(r[0], {"claim": r[1], "quote": r[2], "tags": r[3], "snippet": r[4],
                        "score": round(r[5], 3), "match": "fts5"}) for r in rows]
    except sqlite3.Error:
        return []


def structured_filter(cur, args):
    sql = ("SELECT a.note_id, a.file_name, a.page, a.page_label, a.annotation_type, a.category, "
           "a.quote, a.anchor_verified, a.confidence, a.cluster_id, a.created FROM annotations a WHERE 1=1")
    params = []
    if args.category:
        sql += " AND a.category = ?"
        params.append(args.category)
    if args.source:
        sql += " AND a.file_name LIKE ?"
        params.append("%" + args.source + "%")
    if args.page:
        sql += " AND a.page = ?"
        params.append(args.page)
    if args.since:
        sql += " AND a.created >= ?"
        params.append(args.since)
    if args.until:
        sql += " AND a.created <= ?"
        params.append(args.until + "\uffff")
    return sql, params


def enrich(cur, hits):
    out = []
    for rowid, meta in hits:
        note_id = "N%04d" % int(rowid)
        r = cur.execute(
            "SELECT a.note_id, a.file_id, a.file_name, a.page, a.page_label, a.annotation_type, "
            "a.category, a.anchor_verified, a.confidence, a.cluster_id, f.sha256 "
            "FROM annotations a LEFT JOIN files f ON f.file_id = a.file_id "
            "WHERE a.note_id = ? LIMIT 1",
            (note_id,)).fetchone()
        if not r:
            continue
        item = {
            "note_id": r[0], "file_id": r[1], "file_name": r[2],
            "page": r[3], "page_label": r[4], "annotation_type": r[5],
            "category": r[6], "anchor_verified": bool(r[7]) if r[7] is not None else None,
            "confidence": r[8], "cluster_id": r[9], "file_sha256": r[10],
            "claim": meta.get("claim"), "quote": meta.get("quote"),
            "snippet": meta.get("snippet"), "match": meta.get("match"),
            "score": meta.get("score"),
        }
        links = cur.execute(
            "SELECT link_id, src, dst, type, strength, rationale FROM links WHERE src = ? OR dst = ? LIMIT 5",
            (item["note_id"], item["note_id"])).fetchall()
        item["related"] = [{"link_id": l[0], "peer": l[1] if l[2] == item["note_id"] else l[2],
                            "type": l[3], "strength": l[4], "rationale": l[5]} for l in links]
        out.append(item)
    return out


def search(cur, args):
    hits = []
    if args.q:
        hits = fts_query(cur, args.q, args.topk)
        if not hits:
            hits = like_query(cur, args.q, args.topk)
    elif args.tag or args.entity:
        term = args.tag or args.entity
        rows = cur.execute(
            "SELECT rowid, claim, quote, tags FROM notes_fts WHERE tags LIKE ? OR quote LIKE ? OR claim LIKE ? LIMIT ?",
            ("%" + term + "%",) * 3 + (args.topk,)).fetchall()
        hits = [(r[0], {"claim": r[1], "quote": r[2], "tags": r[3], "snippet": None,
                        "score": None, "match": "tag/entity"}) for r in rows]
    else:
        sql, params = structured_filter(cur, args)
        rows = cur.execute(sql + " ORDER BY a.created LIMIT ?", params + [args.topk]).fetchall()
        out = []
        for r in rows:
            claim = cur.execute("SELECT claim FROM notes_fts WHERE rowid = ? LIMIT 1",
                                (int(r[0][1:]),)).fetchone()
            out.append({
                "note_id": r[0], "file_name": r[1], "page": r[2], "page_label": r[3],
                "annotation_type": r[4], "category": r[5], "quote": r[6],
                "anchor_verified": bool(r[7]) if r[7] is not None else None,
                "confidence": r[8], "cluster_id": r[9], "created": r[10],
                "claim": claim[0] if claim else None, "match": "structured-filter",
            })
        return out
    return enrich(cur, hits)


def to_md(items, title, query_desc):
    lines = ["## %s" % title, "", "检索式: `%s` | 命中 %d 条" % (query_desc, len(items)), ""]
    if not items:
        lines += ["**召回不足**：无命中。建议换关键词、去掉问句虚词，或检查 pipeline 是否已运行。", ""]
        return "\n".join(lines)
    for i, it in enumerate(items, 1):
        lines.append("%d. **[%s|%s]** %s" % (i, it.get("category") or "-", it.get("annotation_type") or "-",
                                             (it.get("claim") or it.get("quote") or "")[:160]))
        lines.append("   来源: `%s` p.%s | anchor_verified=%s | confidence=%s | note_id=%s" % (
            it.get("file_name"), it.get("page_label") or it.get("page") or "-",
            it.get("anchor_verified"), it.get("confidence"), it.get("note_id")))
        if it.get("related"):
            lines.append("   关联: %s" % "; ".join("%s(%.2f, %s)" % (r["type"], r["strength"], r["rationale"][:60])
                                                   for r in it["related"][:3]))
        low = [it] if (it.get("confidence") is not None and it.get("confidence") < 0.5
                       and it.get("anchor_verified") is not True) else []
        if low:
            lines.append("   ⚠ 可能误召回/低置信度：锚点未验证或 OCR 来源，引用前请核对原文。")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="/app/output/index.sqlite3")
    ap.add_argument("--q", default=None)
    ap.add_argument("--category", default=None)
    ap.add_argument("--tag", default=None)
    ap.add_argument("--entity", default=None)
    ap.add_argument("--source", default=None)
    ap.add_argument("--page", type=int, default=None)
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--topk", type=int, default=20)
    ap.add_argument("--format", choices=["json", "md"], default="json")
    ap.add_argument("--explain", action="store_true")
    ap.add_argument("--report", default=None)
    ap.add_argument("--demo-queries", nargs="*", default=None)
    args = ap.parse_args()

    if not os.path.exists(args.index):
        print(json.dumps({"error": "index not found: %s (先运行 pipeline.py)" % args.index,
                          "status": "failed"}, ensure_ascii=False))
        return 2
    con = sqlite3.connect("file:%s?mode=ro" % args.index, uri=True)
    cur = con.cursor()

    if args.report:
        if not args.demo_queries:
            print(json.dumps({"error": "--report 需要同时提供 --demo-queries"}, ensure_ascii=False))
            return 2
        secs = ["# 检索召回报告 (retrieval_report)", "",
                "生成时间: %s | 索引: `%s`" % (datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                                               args.index), "",
                "> 说明：本节为真实运行的检索证据；召回不足与低置信度命中均已标注。", ""]
        for q in args.demo_queries:
            args.q, args.category, args.tag, args.entity = q, None, None, None
            items = search(cur, args)
            secs.append(to_md(items, "查询: %s" % q, "q=%s" % q))
            secs.append("")
        with open(args.report, "w", encoding="utf-8") as f:
            f.write("\n".join(secs))
        print(json.dumps({"report": args.report, "queries": len(args.demo_queries)}, ensure_ascii=False))
        return 0

    items = search(cur, args)
    if args.format == "md":
        desc = " ".join("%s=%s" % (k, v) for k, v in vars(args).items()
                        if v and k in ("q", "category", "tag", "entity", "source", "page", "since", "until"))
        print(to_md(items, "检索结果", desc))
    else:
        if not args.explain:
            for it in items:
                it.pop("related", None)
        print(json.dumps({"index": args.index, "count": len(items), "results": items},
                         ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
