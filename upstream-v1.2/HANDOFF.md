# 旦导风险质检 Agent V1.2 移交说明

## 1. 这份包可以做什么

输入校园问答日志的 Excel 或 CSV 后，程序会逐条执行以下检测：

1. 敏感与安全问题候选；
2. 联网引用是否具备进一步核验条件；
3. 复旦相关舆情风险初筛；
4. 知识缺口候选；
5. 防御性回答与语料补充候选；
6. 含日期、钟点、时长或“最新/今年/什么时候”等表达的时效性候选。

V1.2 会将完全重复和相似问法合并为问题组。人工只需按组复核一次，同时可以通过原表行号回溯逐条记录。

## 2. 文件结构

```text
dandao-quality-agent-v1.2/
├── v1_agent.py                   核心质检程序
├── run_local.py                  面向非技术用户的交互式入口
├── config/                       规则与词表
├── tests/                        自动化测试
├── skills/                       可分享的 Codex Skill
├── reporting/build_v12_report.mjs Excel 报告生成脚本
├── examples/sample_qa.csv        脱敏示例数据
├── requirements.txt              Python 依赖
├── README.md                     使用说明
└── HANDOFF.md                    本移交说明
```

历史问答日志、姓名、用户名、本次真实质检结果和运行缓存均未放入移交包。

## 3. 首次安装

需要 Python 3.10 或更高版本。

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

Windows 激活虚拟环境时使用：

```powershell
.venv\Scripts\activate
```

## 4. 最简单的运行方式

```bash
python3 run_local.py
```

按提示输入日志路径、工作表名称、开始时间、结束时间和输出目录。

技术人员也可以直接运行：

```bash
python3 v1_agent.py \
  --input "/path/to/问答记录.xlsx" \
  --sheet "问答记录" \
  --start-time "2026-09-01 00:00:00" \
  --end-time "2026-10-15 00:00:00" \
  --output-dir "outputs/run_001"
```

时间区间采用左闭右开。上例包含 9月1日零点及之后的数据，不包含 10月15日零点及之后的数据。

## 5. 输入字段

必需字段：

- `问题`
- `答案`
- `回答来源`
- `联网搜索引用来源`

建议字段：

- `提问时间`：用于严格按时间区间筛选；
- `用户名`、`真实姓名`、`昵称`：程序读取后会删除，并对正文中的同值内容进行脱敏；
- `用户评价`、`入库状态`、`负反馈标签`、`负反馈原因`：可供后续版本扩展监督信号。

如果请求了时间范围，但文件没有时间列，程序会停止。只有确认源文件已经预先筛选时，才能使用 `--assume-prefiltered`。

## 6. 输出文件

- `results.jsonl`：完整结构化明细，适合程序读取；
- `results.csv`：逐条质检结果；
- `corpus_enrichment_candidates.csv`：逐条语料补充候选；
- `timeliness_candidates.csv`：逐条时效性候选；
- `corpus_enrichment_review_groups.csv/json`：相似问题合并后的语料复核任务；
- `timeliness_review_groups.csv/json`：相似问题合并后的时效复核任务；
- `summary.json`：批次汇总；
- `errors.json`：错误记录；
- `quality_agent.sqlite`：可供后续工作台接入的数据文件。

Excel 汇总报告由 `reporting/build_v12_report.mjs` 生成。该脚本使用 Codex 工作区提供的 `@oai/artifact-tool`。若迁移到普通服务器，建议技术团队把 CSV/JSON/SQLite 接入自有前端，或将报告生成器替换为内部批准的 Excel/BI 方案。

## 7. 修改规则

- 敏感安全：`config/sensitive_keywords.yaml`
- 舆情风险：`config/public_opinion.yaml`
- 知识缺口：`config/knowledge_gap.yaml`
- 防御性回答与时效性：`config/content_maintenance.yaml`
- 可选模型复核提示词：`config/llm_prompts.yaml`

配置文件采用 JSON 兼容的 YAML 写法。修改后先运行测试，再使用少量历史数据回放，检查误报和漏报。

## 8. 相似问题聚类

V1.2 使用可解释的文本归一化、字符片段相似度和关键数字保护规则。它会合并标点、礼貌用语及“如何/怎么、缴纳/交”等轻微措辞差异。

以下内容默认不轻易合并：

- 本科生与研究生；
- 水费与电费；
- 不同年份、日期、金额、楼栋等数字不同的问题。

聚类仍然是启发式规则。上线前应持续抽检问题组，并根据误合并案例增加保护规则。后续可在校内服务器部署中文向量模型，以“规则保护＋语义向量召回＋人工确认”的方式升级。

## 9. 测试与验收

```bash
python3 -m unittest discover -s tests -v
python3 v1_agent.py \
  --input "examples/sample_qa.csv" \
  --start-time "2026-09-01 00:00:00" \
  --end-time "2026-10-15 00:00:00" \
  --output-dir "outputs/sample_run"
```

验收时至少确认：

1. 测试全部通过；
2. `errors.json` 为空；
3. 汇总数量与逐条结果一致；
4. 相似问题组可以回溯到原表行；
5. 候选标签没有被误写为已确认 Badcase；
6. 输出中不包含用户名、真实姓名和昵称字段。

## 10. 当前边界与后续开发建议

当前版本是高召回的规则初筛，不是最终裁决模型：

- 联网引用尚未抓取网页正文，因此只能标记 `unverifiable`；
- 缺少知识库检索日志时，不能确认是否真正存在知识缺口；
- 防御性和时效性命中只表示需要人工复核；
- 相似问题聚类仍可能存在少量误合并或漏合并。

建议后续依次建设：

1. 保存人工复核结论，形成可回放的标注集；
2. 接入知识库检索日志和引用正文；
3. 增加按周趋势、版本对比和优化动作闭环；
4. 将核心任务、问题组和复核状态写入数据库；
5. 开发上传文件、任务进度、筛选与导出页面；
6. 部署到学校信息安全办批准的服务器，并配置访问控制、操作日志和数据保留策略。
