# 全流程演示（合成示例）

以下为**合成输入**的完整流程示例，仅用于说明接口与规则命中方式，非正式案例结果。

## 输入（/input，10 份材料 + case.json + task.md）

| 文件 | 内容 | 预期命中规则 |
|-|-|-|
| smith-2023-perovskite.pdf | 期刊正式版，含 DOI 10.1039/D3TA01234A | T9 journal_article；canonical |
| download (1) smith perovskite.pdf | 上一文件的逐字节副本 | 精确重复 → _archive/duplicates |
| 2304.12345_preprint.pdf | 同工作 arXiv 预印本（无 DOI） | T6 preprint；版本组 → superseded |
| perovskite_supporting_information.pdf | 补充材料 | T2 supplement |
| kumar2024conf.pdf + kumar2024conf.bib | 会议论文 + BibTeX 侧车 | T8 conference_paper；侧车高置信元数据 |
| zhang_thesis_2021.pdf | 学位论文 | T5 thesis |
| scan_old_thesis.pdf | 无文本层扫描件 | T4 scanned_document |
| perovskite_data.csv | 数据集 | T1 dataset |
| broken_download.pdf | 截断损坏 | skip + exceptions |
| 综述笔记.txt | 中文笔记 | T11 other_document |

## 调用

```bash
python3 /skills/weekly-skill/scripts/inspect_case.py            # → brief.json
bash /skills/weekly-skill/scripts/run_pipeline.sh /input /app/work /app/output
python3 /skills/weekly-skill/scripts/validate_output.py --work-dir /app/work --output-dir /app/output
```

## 关键输出片段（示例标注）

**目录结构：**

```
library/
├── journal_article/2023/Smith-2023-High-Throughput_Screening_of_Perovskite_Photocatalysts.pdf
├── preprint/…（无，预印本被版本组取代）
├── conference_paper/2024/Kumar-2024-Bayesian_Optimization_for_Battery_Electrolyte_Discovery.pdf
├── thesis/2021/Zhang-2021-PhD_Thesis_Machine_Learning_for_Crystal_Structure_Prediction.pdf
├── supplement/unknown_year/unknown_author-ndate-Supporting_Information_….pdf
├── scanned_document/unknown_year/scan_old_thesis__f884b278.pdf
├── dataset/unknown_year/perovskite_data__04fa2525.csv
└── other_document/unknown_year/综述笔记__a87d7bbf.txt
_archive/duplicates/download (1) smith perovskite.pdf
_archive/versions/doi_101039_d3ta01234a/Smith-2023-…Photocatalysts.pdf   # 预印本
```

**manifest 行（示例）：**

```
smith-2023-perovskite.pdf, place, library/journal_article/2023/Smith-2023-….pdf,
  title_source=pdf_text_heuristic, doi=10.1039/d3ta01234a, version_role=canonical
2304.12345_preprint.pdf, archive_version, _archive/versions/doi_101039_d3ta01234a/…,
  version_role=superseded, reason="同组已有元数据更完整的正式版本"
broken_download.pdf, skip, reason="pdf probe: …"
```

**版本组（示例）：** `{"key":"doi:10.1039/d3ta01234a","canonical":"smith-2023-perovskite.pdf","archived":["2304.12345_preprint.pdf"]}`

**自查结果（示例）：** `{"passed": true, "verified_files": 10, "problems": []}`

## 演示要点

1. 精确重复按"文件名最干净者"保留；
2. 预印本与正式版通过 (标题+第一作者) 键并入同一 DOI 组，正式版为 canonical；
3. 侧车提供高置信元数据且不参与版本组；
4. 损坏文件诚实 skip，status=partial；
5. 全部操作可 `bash /app/output/audit/rollback.sh` 一键撤销。
