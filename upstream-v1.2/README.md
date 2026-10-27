# 旦导风险质检 Agent V1

一个离线、可审计的问答日志质检原型。V1.2 处理六类候选，并将相似问题合并为人工复核任务组：

1. 敏感或违禁问题的漏拦截、疑似误拦截与边界复核；
2. 联网搜索回答的引用可验证性入口；
3. 明确涉及复旦实体的舆情风险初筛；
4. 在缺少后台检索字段时输出 `suspected_gap`，绝不输出确定知识缺口。
5. 防御性回答筛选：当回答同时表示“信息不足”并“引导至辅导员/部门/官方通知”时，生成语料补充复核任务。
6. 时效性问答筛选：识别具体年月日、钟点、时长，以及“今年/最新/什么时候/截止时间”等表达，交由人工确认是否仍有效。

## 快速运行

```bash
python3 v1_agent.py \
  --input "/path/to/问答历史记录.xlsx" \
  --sheet "问答记录" \
  --limit 200 \
  --output-dir "outputs/test_200"
```

也可以使用 CSV。首次使用或移交验证时，可运行：

```bash
python3 v1_agent.py \
  --input "examples/sample_qa.csv" \
  --start-time "2026-09-01 00:00:00" \
  --end-time "2026-10-15 00:00:00" \
  --output-dir "outputs/sample_run"
```

不熟悉命令行时，可以运行 `python3 run_local.py`，再按提示输入文件路径与时间范围。

输出包括：

- `results.jsonl`：逐条结构化结果；
- `results.csv`：便于检查的扁平明细；
- `corpus_enrichment_candidates.csv`：只保留防御性回答候选，供语料补充人员使用；
- `timeliness_candidates.csv`：只保留时效性问答候选，供内容有效性复核；
- `corpus_enrichment_review_groups.csv`：将完全重复和近似问法合并后的语料补充任务；
- `timeliness_review_groups.csv`：将完全重复和近似问法合并后的时效复核任务；
- `summary.json`：批次汇总；
- `errors.json`：导入及单条错误；
- `quality_agent.sqlite`：可断点复用的结果库。

原始 Excel 不会被修改。用户名、真实姓名、昵称在读取后立即删除，不进入输出或 SQLite。

`results.csv` 中新增了两组可直接筛选的字段：

- `防御性回答`、`防御性回答类别`、`防御性回答命中词`、`防御性回答理由`；
- `时效性复核`、`时效性触发类型`、`时效性命中片段`、`时效性理由`。
- `人工任务标签`：直接标记为 `语料库补充`、`时效性核验`，一条记录可同时命中两类任务。

这两类命中都会进入 `manual_review`，不会被自动标记为已确认 Badcase。规则词表位于 `config/content_maintenance.yaml`，后续可由业务同学持续补充。

人工复核默认使用 `*_review_groups.csv`。每个问题组只复核一次，并保留代表问题、合并记录数、最多 8 个问题变体和最多 12 个样例原表行。聚类会统一“如何/怎么、缴纳/交”等无害措辞差异；含不同年份、金额、楼栋等数字的问题不会轻易合并。逐条候选文件仍保留，便于必要时回溯原始记录。

## 可选模型复核

规则层始终先运行。设置 `OPENAI_API_KEY` 和 `OPENAI_MODEL` 后，可加 `--use-llm`，仅将规则候选发送给 Responses API，并要求返回结构化 JSON。没有密钥时程序自动降级为规则候选＋人工复核，不会把规则猜测冒充模型结论。

```bash
export OPENAI_API_KEY="..."
export OPENAI_MODEL="..."
python3 v1_agent.py --input "/path/to/file.xlsx" --limit 200 --use-llm
```

在包含真实人员或学生数据的环境中，启用外部模型前必须先完成学校的信息安全审批。

## 当前边界

- 联网一致性完整判断需要抓取引用正文、拆分原子事实和模型核对。当前原型在无正文抓取证据时统一输出 `unverifiable`，不会错误输出 `unsupported`。
- 当前日志缺少 `knowledge_base_searched`、`knowledge_base_hit`、`retrieved_doc_count`，因此知识缺口只能是 `suspected_gap`。
- 防御性回答需同时命中信息不足和转人工/官方渠道；仅在完整回答末尾补充“如有疑问可联系”不会被标记。
- 时效性筛选只识别“可能需要更新”，不判定内容已过期。
- 规则和政策均放在 `config/*.yaml`。这些文件采用 JSON 兼容的 YAML 写法，因此不依赖额外 YAML 包。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
