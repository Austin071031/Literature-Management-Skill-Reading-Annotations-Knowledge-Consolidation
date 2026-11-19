# 最小调用示例（示意输出，非正式案例结果）

> ⚠ 本文件全部输出为**结构示例**，用于说明接口与流程；数值与内容均为演示性质，
> 不代表任何真实案例的运行结果。

## 输入（示意）

```json
// /input/case.json
{
  "case_id": "demo-minimal",
  "materials_dir": "/input/materials",
  "output_contract": {
    "inventory": "文件清单与元数据",
    "annotations": "逐条批注及锚点",
    "notes": "结构化笔记",
    "index": "可检索索引",
    "status": "运行状态"
  }
}
```

材料目录 `/input/materials/`：

```
paper_draft_v1.pdf      # 内嵌 2 条批注：1 高亮、1 页边批注
paper_draft_v2.pdf      # 同名新版本（v1 -> v2）
paper_draft_v1.pdf      # 重复下载（sha256 与 v1 相同）
reading_notes.md        # 散落笔记：2 行，其中 1 行引用 paper_draft_v2.pdf p.1
```

## 调用

```bash
python3 /skills/weekly-skill/scripts/pipeline.py \
  --case /input/case.json --workdir /app/work --outdir /app/output
python3 /skills/weekly-skill/scripts/search.py \
  --index /app/output/index.sqlite3 --q "training method" --format md
```

## 示意输出

pipeline 标准输出（示例值）：

```json
{"status": "partial", "case_id": "demo-minimal",
 "summary": {"files": 4, "pdfs_processed": 2, "pdfs_failed": 0,
             "annotations": 2, "notes": 3, "note_links": 1,
             "file_version_links": 2, "duplicate_groups": 1, "version_groups": 1},
 "verification_rate": 1.0, "elapsed_s": 1.2}
```

notes.json 单条（节选，示例值）：

```json
{
  "note_id": "N0001",
  "category": "method",
  "claim": "关键方法：GNN potential + active learning",
  "source": {
    "file_id": "F0001", "file_name": "paper_draft_v1.pdf",
    "file_sha256": "df29…c041", "page": 1, "page_label": "1",
    "char_start": 0, "char_end": 73,
    "quote": "We present a graph neural network potential for lithium battery electrolytes.",
    "bbox": [53.0, 60.0, 560.0, 92.0],
    "annotation_id": "F0001#p1a1",
    "anchor_verified": true, "anchor_match": "exact"
  },
  "annotation_type": "highlight",
  "color_hex": "#ffff00",
  "confidence": 1.0,
  "cluster_id": "C001",
  "derived_from": "pdf_annotation"
}
```

search 输出（节选，示例值）：

```
## 检索结果
检索式: `q=training method` | 命中 1 条
1. **[method|highlight]** 关键方法：GNN potential + active learning
   来源: `paper_draft_v1.pdf` p.1 | anchor_verified=True | confidence=1.0 | note_id=N0001
   关联: related(1.00, shared entity method:active learning)
```

result.json 的 output_contract 映射（节选，示例值）：

```json
"output_contract": {
  "inventory": {"deliverable": "inventory.json", "files": 4, "duplicate_groups": 1, "version_groups": 1},
  "annotations": {"deliverable": "notes.json + index.sqlite3", "count": 2, "anchor_verification_rate": 1.0},
  "notes": {"deliverable": "notes.json", "count": 3},
  "index": {"deliverable": "index.sqlite3", "fts5": true},
  "status": {"value": "partial", "failures": []}
}
```
