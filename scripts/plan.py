#!/usr/bin/env python3
"""plan.py — 整理计划：文献类型判定、版本分组、去重、标签、命名、目录归属。

只生成计划（dry-run by design），不落盘改动；apply.py 才执行。
每条决策附 rule_id 与证据，支持逐条复核。

用法:
  python3 plan.py [--work-dir /app/work] [--config PATH] [--theme-rules PATH]
--config: JSON，可覆盖 naming_template / directory_scheme / library_root /
          keep_archives 等（通常由 Agent 根据 task.md 生成）。
输出: /app/work/plan.json
"""
import argparse, json, os, re, sys, unicodedata

DEFAULTS = {
    "library_root": "library",
    "directory_scheme": "by_type_year",   # by_type_year | by_theme | flat
    "naming_template": "{first_author}-{year}-{title_slug}",
    "keep_archives": True,
    "max_title_slug": 60,
    "max_tags": 6,
}

DOC_TYPES = ["journal_article", "preprint", "conference_paper", "thesis",
             "supplement", "dataset", "scanned_document", "other_document"]

# 受控标签词表（同义合并→规范形）。tags 只从真实文本命中生成，不外推。
TAG_VOCAB = {
    "machine-learning": ["machine learning", "ml-based", "机器学习"],
    "deep-learning": ["deep learning", "neural network", "深度学习", "神经网络"],
    "density-functional-theory": ["density functional", "dft", "密度泛函"],
    "molecular-dynamics": ["molecular dynamics", "md simulation", "分子动力学"],
    "first-principles": ["first-principles", "ab initio", "第一性原理"],
    "phase-transition": ["phase transition", "相变"],
    "superconductivity": ["superconduct", "超导"],
    "catalysis": ["cataly", "催化"],
    "battery": ["battery", "lithium-ion", "li-ion", "电池"],
    "perovskite": ["perovskite", "钙钛矿"],
    "photocatalysis": ["photocataly", "光催化"],
    "screening": ["high-throughput screening", "高通量筛选"],
    "crystal-structure": ["crystal structure", "晶体结构"],
    "band-structure": ["band structure", "能带"],
    "quantum": ["quantum", "量子"],
    "interface": ["interface", "界面"],
    "defect": ["defect", "缺陷"],
    "optimization": ["optimization", "优化"],
    "bayesian": ["bayesian", "贝叶斯"],
    "dataset": ["dataset", "数据库", "data set"],
}

def slugify(text, maxlen=60):
    """文件名安全化：保留中英文、数字、连字符与下划线；其余归一。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text))
    t = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", t)
    t = re.sub(r"\s+", "_", t.strip())
    t = re.sub(r"[^\w\u4e00-\u9fff\-]+", "", t, flags=re.UNICODE)
    t = re.sub(r"_+", "_", t).strip("_-")
    if len(t) > maxlen:
        t = t[:maxlen]
        # 尽量截在词边界，避免半个词
        cut = t.rfind("_")
        if cut >= maxlen - 20 and cut > 10:
            t = t[:cut]
    return t or ""

def classify(rec, ref):
    """返回 (doc_type, rule_id, evidence)。规则自上而下命中即停。"""
    name = os.path.basename(rec["rel_path"]).lower()
    ext = rec["extension"]
    dh = (ref["doc_hint"]["value"] or "").lower() if ref else ""

    if ext in ("bib", "ris", "enw"):
        return "other_document", "T0-sidecar", "纯元数据侧车文件"
    if rec["mime_sniff"] in ("zip", "gzip", "rar") or ext in ("csv", "xlsx", "xls", "json", "cif", "xyz"):
        return "dataset", "T1-data-format", f"数据格式: {rec['mime_sniff'] or ext}"
    if "suppl" in name or "supporting" in name or "_si" in name or "appendix" in name or "补充" in name:
        return "supplement", "T2-supplement-name", "文件名含补充材料标记"
    if dh == "supplement":
        return "supplement", "T3-supplement-hint", ref["doc_hint"]["evidence"]
    if rec["mime_sniff"] == "pdf" and (rec["pdf_text_chars_first2p"] or 0) == 0 \
            and rec["pdf_encrypted"] is not True and not ref.get("sidecar_path"):
        return "scanned_document", "T4-no-text-layer", "PDF 无文本层（疑似扫描件）"
    if dh in ("thesis", "phdthesis", "mastersthesis") or re.search(r"(?i)thesis|dissertation|学位", name):
        return "thesis", "T5-thesis", dh or name
    if ref["arxiv"]["value"] and not (ref["venue"]["value"] and ref["doi"]["value"]):
        return "preprint", "T6-arxiv-only", "有 arXiv 号且无期刊元数据"
    if dh == "preprint":
        return "preprint", "T7-preprint-hint", ref["doc_hint"]["evidence"]
    venue = (ref["venue"]["value"] or "")
    if re.search(r"(?i)proc\.|conference|symposium|workshop|会议", venue) or \
            (ref["doc_hint"]["value"] or "").lower() in ("inproceedings", "conference"):
        return "conference_paper", "T8-venue-conference", venue
    if ref["doi"]["value"] or venue or dh == "article":
        return "journal_article", "T9-journal-evidence", venue or ref["doi"]["value"] or dh
    if rec["mime_sniff"] == "pdf":
        return "journal_article", "T10-default-pdf", "PDF 且有文本层，按期刊论文兜底（低置信）"
    return "other_document", "T11-fallback", "无文献特征"

def build_tags(rec, ref, max_tags):
    hits = []
    hay = " ".join(filter(None, [
        ref["title"]["value"] or "",
        " ".join(ref["keywords"]["value"] or []),
    ])).lower()
    for tag, syns in TAG_VOCAB.items():
        for s in syns:
            if s in hay:
                hits.append(tag)
                break
    # 关键词直出（受控词表之外，来自原文 Keywords 行），限 3 个
    direct = []
    for k in (ref["keywords"]["value"] or []):
        k2 = k.strip().lower().replace(" ", "-")
        if k2 and k2 not in hits and len(k2) >= 3:
            direct.append(k2)
    tags = hits + direct
    return tags[:max_tags]

def version_key(ref):
    """版本组键：DOI 优先，否则 规范化标题+第一作者。"""
    if ref["doi"]["value"]:
        return "doi:" + ref["doi"]["value"]
    nt = ref.get("normalized_title")
    fa = ref.get("first_author_surname")
    if nt and fa:
        return f"ta:{fa.lower()}:{nt}"
    if nt:
        return "t:" + nt
    return None

def render_name(rec, ref, cfg, used_names, namespace=""):
    tpl = cfg["naming_template"]
    fa = ref.get("first_author_surname")
    year = ref["year"]["value"]
    title = ref["title"]["value"]
    slug = slugify(title, cfg["max_title_slug"]) if title else ""

    # 降级链：作者/年份/标题缺失逐段省略，全缺时回退原名净化+短哈希
    def sub(template):
        s = template
        s = s.replace("{first_author}", slugify(fa, 30) if fa else "unknown_author")
        s = s.replace("{year}", str(year) if year else "ndate")
        s = s.replace("{title_slug}", slug if slug else "untitled")
        s = s.replace("{doi}", slugify(ref["doi"]["value"], 60) if ref["doi"]["value"] else "nodoi")
        s = s.replace("{venue_slug}", slugify(ref["venue"]["value"], 40) if ref["venue"]["value"] else "nvenue")
        return s

    base = sub(tpl)
    if (not fa) and (not year) and (not slug):
        base = slugify(os.path.splitext(os.path.basename(rec["rel_path"]))[0], 50) or "unnamed"
        base = f"{base}__{rec['sha256'][:8]}"  # 保证可追溯与唯一
    ext = rec["extension"] or "bin"
    key = lambda n: (namespace, n)
    name = f"{base}.{ext}"
    if key(name) in used_names:
        base2 = f"{base}__{rec['sha256'][:8]}"
        name = f"{base2}.{ext}"
    i = 2
    while key(name) in used_names:
        name = f"{base2}_{i}.{ext}"
        i += 1
    used_names.add(key(name))
    return name

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", default="/app/work")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    cfg = dict(DEFAULTS)
    if args.config and os.path.exists(args.config):
        with open(args.config, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))

    wd = args.work_dir
    with open(os.path.join(wd, "inventory.json"), "r", encoding="utf-8") as f:
        inv = json.load(f)
    with open(os.path.join(wd, "refs.json"), "r", encoding="utf-8") as f:
        refs = json.load(f)

    SIDECAR_EXT = ("bib", "ris", "enw")

    def filename_cleanliness(rel):
        """重复文件保留谁：文件名越干净越优先（不看出处，仅看可辨识性）。"""
        name = os.path.basename(rel)
        return (
            bool(re.search(r"\(\d+\)", name)),          # "下载 (1)" 类
            bool(re.search(r"(?i)download|副本|copy", name)),
            name.count(" "),
            len(name),
            name,
        )

    # 1) 精确去重（同 sha256）：保留文件名最干净者，其余归档
    hash_groups = {}
    for rec in inv["records"]:
        if rec["sha256"]:
            hash_groups.setdefault(rec["sha256"], []).append(rec["rel_path"])
    exact_dup_of = {}
    exact_dup_groups_kept = []
    for h, group in hash_groups.items():
        if len(group) < 2:
            continue
        group = sorted(group, key=filename_cleanliness)
        kept = group[0]
        for dup in group[1:]:
            exact_dup_of[dup] = kept
        exact_dup_groups_kept.append({"kept": kept, "archived": group[1:]})

    # 2) 版本分组：DOI 与 (标题+第一作者) 两个键做并查集，覆盖"预印本 vs 正式版"
    #    侧车文件（bib/ris/enw）不参与版本组。
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    member_keys = {}  # rel_path -> set of keys
    for rec in inv["records"]:
        rel = rec["rel_path"]
        if rel in exact_dup_of or rec["extension"] in SIDECAR_EXT:
            continue
        ref = refs.get(rel)
        if not ref:
            continue
        keys = set()
        if ref["doi"]["value"]:
            keys.add("doi:" + ref["doi"]["value"])
        nt = ref.get("normalized_title")
        fa = ref.get("first_author_surname")
        if nt and len(nt) >= 12 and fa:
            keys.add(f"ta:{fa.lower()}:{nt[:60]}")
        elif nt and len(nt) >= 12:
            # 无作者时退化用规范化标题分组（同名同题视为版本候选，保守阈值防误并）
            keys.add("t:" + nt[:60])
        member_keys[rel] = keys
        ks = sorted(keys)
        for k in ks[1:]:
            union(ks[0], k)

    vgroups = {}
    for rel, keys in member_keys.items():
        if not keys:
            continue
        root = find(sorted(keys)[0])
        vgroups.setdefault(root, []).append(rel)

    def canonical_rank(rel):
        ref = refs[rel]
        rec = next(r for r in inv["records"] if r["rel_path"] == rel)
        score = 0
        if ref["doi"]["value"]:
            score += 4
        if ref["venue"]["value"]:
            score += 2
        if ref["year"]["value"]:
            score += 1
        if ref["title"]["value"]:
            score += 1
        if (rec["pdf_text_chars_first2p"] or 0) > 0:
            score += 1
        return (-score, rel)

    version_decisions = {}
    for key, members in vgroups.items():
        if len(members) < 2:
            continue
        members_sorted = sorted(members, key=canonical_rank)
        canon, rest = members_sorted[0], members_sorted[1:]
        version_decisions[key] = {"canonical": canon, "archived": rest}

    # 3) 逐文件生成动作
    used_names = set()
    items, conflicts = [], []
    for rec in inv["records"]:
        rel = rec["rel_path"]
        ref = refs.get(rel) or {}
        item = {
            "rel_path": rel, "abs_path": rec["abs_path"], "sha256": rec["sha256"],
            "size_bytes": rec["size_bytes"], "action": None, "target": None,
            "doc_type": None, "classify_rule": None, "tags": [],
            "new_name": None, "version_group": None, "version_role": None,
            "reason": None,
        }
        if rec["error"]:
            item["action"] = "skip"
            item["reason"] = f"输入异常，跳过并如实记录: {rec['error']}"
            item["doc_type"] = "other_document"
            items.append(item)
            continue
        if rel in exact_dup_of:
            item["action"] = "archive_duplicate"
            item["reason"] = f"sha256 与 {exact_dup_of[rel]} 完全相同，判定为重复下载"
            item["target"] = f"_archive/duplicates/{os.path.basename(rel)}"
            item["doc_type"] = "duplicate"
            items.append(item)
            continue

        doc_type, rule_id, evidence = classify(rec, ref)
        item["doc_type"] = doc_type
        item["classify_rule"] = {"rule_id": rule_id, "evidence": evidence}
        item["tags"] = build_tags(rec, ref, cfg["max_tags"])

        # 版本角色
        for key, vd in version_decisions.items():
            if rel == vd["canonical"]:
                item["version_group"] = key
                item["version_role"] = "canonical"
            elif rel in vd["archived"]:
                item["version_group"] = key
                item["version_role"] = "superseded"

        if cfg["directory_scheme"] == "by_type_year":
            year_dir = str(ref["year"]["value"]) if ref["year"]["value"] else "unknown_year"
            dest_dir = os.path.join(cfg["library_root"], doc_type, year_dir)
        elif cfg["directory_scheme"] == "flat":
            dest_dir = cfg["library_root"]
        else:
            dest_dir = os.path.join(cfg["library_root"], doc_type)

        namespace = "_archive/versions" if item["version_role"] == "superseded" else dest_dir
        name = render_name(rec, ref, cfg, used_names, namespace=namespace)
        item["new_name"] = name

        if item["version_role"] == "superseded":
            item["action"] = "archive_version"
            item["target"] = os.path.join("_archive", "versions",
                                          slugify(item["version_group"], 80), name)
            item["reason"] = "同组已有元数据更完整的正式版本，保留归档不删除"
        else:
            item["action"] = "place"
            item["target"] = os.path.join(dest_dir, name)
            item["reason"] = f"按 {rule_id} 归入 {doc_type}"
        items.append(item)

    plan = {
        "case_id": inv.get("case_id"),
        "config": cfg,
        "version_groups": [{"key": k, **v} for k, v in version_decisions.items()],
        "exact_duplicates": exact_dup_groups_kept,
        "items": items,
        "conflicts": conflicts,
    }
    with open(os.path.join(wd, "plan.json"), "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    n_place = sum(1 for i in items if i["action"] == "place")
    print(f"[plan] items={len(items)} place={n_place} "
          f"dup_arch={sum(1 for i in items if i['action']=='archive_duplicate')} "
          f"ver_arch={sum(1 for i in items if i['action']=='archive_version')} "
          f"skip={sum(1 for i in items if i['action']=='skip')}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
