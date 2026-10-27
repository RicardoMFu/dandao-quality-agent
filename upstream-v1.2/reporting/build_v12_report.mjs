import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const root = process.cwd();
const runDir = process.argv[2] ? path.resolve(process.argv[2]) : path.join(root, "outputs/latest");
const outputPath = process.argv[3] ? path.resolve(process.argv[3]) : path.join(runDir, "旦导风险质检Agent_V1.2_质检报告.xlsx");
const reportPeriod = process.argv[4] || "本批次";
const results = (await fs.readFile(path.join(runDir, "results.jsonl"), "utf8"))
  .trim().split("\n").filter(Boolean).map((line) => JSON.parse(line));
const summary = JSON.parse(await fs.readFile(path.join(runDir, "summary.json"), "utf8"));
const errors = JSON.parse(await fs.readFile(path.join(runDir, "errors.json"), "utf8"));
const corpusGroupRows = JSON.parse(await fs.readFile(path.join(runDir, "corpus_enrichment_review_groups.json"), "utf8"));
const timelyGroupRows = JSON.parse(await fs.readFile(path.join(runDir, "timeliness_review_groups.json"), "utf8"));

const wb = Workbook.create();
const dashboard = wb.worksheets.add("汇总看板");
const corpus = wb.worksheets.add("语料补充");
const timely = wb.worksheets.add("时效复核");
const risk = wb.worksheets.add("风险复核");
const detail = wb.worksheets.add("评测明细");
const notes = wb.worksheets.add("运行说明");

const color = {
  navy: "#183B56", blue: "#2D6A9F", sky: "#EAF3F8", teal: "#287D78",
  green: "#3E8260", paleGreen: "#E9F4ED", amber: "#C98216", paleAmber: "#FFF2D2",
  red: "#C64747", paleRed: "#FCEAEA", ink: "#253746", gray: "#667582",
  paleGray: "#F4F6F8", line: "#D7E0E7", white: "#FFFFFF",
};
const font = "Arial";

function title(sheet, range, text) {
  sheet.getRange(range).merge();
  sheet.getRange(range.split(":")[0]).values = [[text]];
  sheet.getRange(range).format = { fill: color.navy, font: { name: font, bold: true, color: color.white, size: 18 }, verticalAlignment: "center" };
  sheet.getRange(range).format.rowHeight = 30;
}
function section(sheet, range, text) {
  sheet.getRange(range).merge();
  sheet.getRange(range.split(":")[0]).values = [[text]];
  sheet.getRange(range).format = { fill: color.sky, font: { name: font, bold: true, color: color.navy, size: 12 }, verticalAlignment: "center" };
}
function header(range) {
  range.format = { fill: color.navy, font: { name: font, bold: true, color: color.white }, verticalAlignment: "center", wrapText: true };
}
function excerpt(value, max = 360) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  return text.length > max ? `${text.slice(0, max)}…` : text;
}
function addCard(labelRange, valueRange, label, formula, numberFormat = "#,##0") {
  dashboard.getRange(labelRange).merge();
  dashboard.getRange(labelRange.split(":")[0]).values = [[label]];
  dashboard.getRange(labelRange).format = { fill: color.sky, font: { name: font, bold: true, color: color.navy }, horizontalAlignment: "center", verticalAlignment: "center" };
  dashboard.getRange(valueRange).merge();
  dashboard.getRange(valueRange.split(":")[0]).formulas = [[formula]];
  dashboard.getRange(valueRange).format = { fill: color.white, font: { name: font, bold: true, color: color.ink, size: 21 }, horizontalAlignment: "center", verticalAlignment: "center", borders: { preset: "outside", style: "thin", color: color.line } };
  dashboard.getRange(valueRange.split(":")[0]).format.numberFormat = numberFormat;
}
function normalizeQuestion(question) {
  let value = String(question ?? "").toLowerCase().replace(/\s+/g, " ").trim();
  for (const [source, target] of [["如何", "怎么"], ["怎样", "怎么"], ["缴纳", "交"], ["支付", "交"], ["需要", "要"], ["可以", "能"], ["是否", "能否"]]) {
    value = value.split(source).join(target);
  }
  value = value.replace(/^(请问|麻烦问下|麻烦问一下|想问下|想问一下|我想知道|学生咨询)/, "");
  value = value.replace(/^(我|本人)/, "");
  value = value.replace(/(呢|呀|啊|吗|么|一下|谢谢|感谢)$/, "");
  return value.replace(/[^0-9a-z\u4e00-\u9fff]+/g, "");
}
function ngrams(value, size = 2) {
  if (!value) return new Set();
  if (value.length <= size) return new Set([value]);
  return new Set(Array.from({ length: value.length - size + 1 }, (_, i) => value.slice(i, i + size)));
}
function similarity(left, right) {
  const a = normalizeQuestion(left); const b = normalizeQuestion(right);
  if (!a || !b) return 0;
  if (a === b) return 1;
  const numsA = new Set(a.match(/\d+/g) ?? []); const numsB = new Set(b.match(/\d+/g) ?? []);
  if (numsA.size && numsB.size && [...numsA].some((n) => !numsB.has(n)) || numsA.size && numsB.size && [...numsB].some((n) => !numsA.has(n))) return 0;
  const [shorter, longer] = a.length <= b.length ? [a, b] : [b, a];
  if (shorter.length >= 5 && longer.includes(shorter) && shorter.length / longer.length >= 0.55) return 0.92;
  const ag = ngrams(a); const bg = ngrams(b);
  const overlap = [...ag].filter((gram) => bg.has(gram)).length;
  const dice = 2 * overlap / Math.max(1, ag.size + bg.size);
  // Dice is deliberately conservative; normalization handles common paraphrases first.
  return dice;
}
function groupByQuestion(items, kind) {
  const groups = [];
  const index = new Map();
  for (const r of items) {
    const question = (r.question || r.record_id).trim();
    const normalized = normalizeQuestion(question);
    const grams = ngrams(normalized);
    const candidates = new Set();
    for (const gram of grams) for (const id of (index.get(gram) ?? [])) candidates.add(id);
    let bestId = null; let bestScore = 0;
    for (const id of candidates) {
      const score = similarity(question, groups[id].question);
      if (score >= 0.82 && score > bestScore) { bestId = id; bestScore = score; }
    }
    if (bestId === null) {
      bestId = groups.length;
      groups.push({ question, count: 0, variants: [], rows: [], sources: new Set(), answer: r.answer, terms: new Set(), types: new Set(), first: r });
      for (const gram of grams) {
        if (!index.has(gram)) index.set(gram, new Set());
        index.get(gram).add(bestId);
      }
    }
    const g = groups[bestId];
    g.count += 1;
    if (!g.variants.includes(question) && g.variants.length < 8) g.variants.push(question);
    if (g.rows.length < 8) g.rows.push(r.excel_row);
    if (r.answer_source) g.sources.add(r.answer_source);
    if (kind === "corpus") {
      for (const x of r.defensive_answer.matched_phrases) g.terms.add(x);
      for (const x of r.defensive_answer.categories) g.types.add(x);
    } else {
      for (const x of r.timeliness.matched_spans) g.terms.add(x);
      for (const x of r.timeliness.trigger_types) g.types.add(x);
    }
  }
  return groups.sort((a, b) => b.count - a.count || a.question.localeCompare(b.question, "zh-CN"));
}
function riskLabel(r) {
  if (r.sensitive.final === "badcase") return ["P0", "疑似敏感泄露"];
  if (r.public_opinion.risk_level === "high") return ["P1", "高风险舆情"];
  if (r.public_opinion.risk_level === "medium") return ["P1", "中风险舆情"];
  if (r.sensitive.final === "manual_review") return ["P2", "敏感语境待判"];
  if (["suspected_gap", "confirmed_gap"].includes(r.knowledge_gap.status)) return ["P2", "知识缺口候选"];
  return ["P2", "联网引用待核验"];
}

const corpusItems = results.filter((r) => r.defensive_answer.review_required);
const timelyItems = results.filter((r) => r.timeliness.review_required);
function fromGroupedRow(row, kind) {
  const split = (value) => String(value ?? "").split(/\s*[|;]\s*/).filter(Boolean);
  return {
    question: row["代表问题"], count: Number(row["合并记录数"] ?? 0), variants: split(row["相似问题变体"]),
    rows: split(row["样例原表行"]), sources: new Set(split(row["回答来源"])), answer: row["答案"],
    terms: new Set(split(kind === "corpus" ? row["防御性回答命中词"] : row["时效性命中片段"])),
    types: new Set(split(kind === "corpus" ? row["防御性回答类别"] : row["时效性触发类型"])), first: row,
  };
}
const corpusGroups = corpusGroupRows.map((row) => fromGroupedRow(row, "corpus"));
const timelyGroups = timelyGroupRows.map((row) => fromGroupedRow(row, "timely"));
const riskItems = results.filter((r) =>
  ["badcase", "manual_review"].includes(r.sensitive.final)
  || ["partially_supported", "contradicted", "unsupported", "unverifiable"].includes(r.citation.status)
  || r.public_opinion.review_required
  || ["suspected_gap", "confirmed_gap"].includes(r.knowledge_gap.status)
).sort((a, b) => {
  const order = { P0: 0, P1: 1, P2: 2 };
  return order[riskLabel(a)[0]] - order[riskLabel(b)[0]] || a.excel_row - b.excel_row;
});

// Detail is the authoritative record-level source for dashboard formulas.
detail.showGridLines = false;
const detailHeaders = [
  "原表行", "record_id", "问题", "答案", "回答来源", "敏感关键词", "敏感结果", "引用状态", "舆情等级", "知识缺口",
  "防御性回答", "防御性命中词", "时效性复核", "时效触发类型", "时效命中片段", "人工任务标签", "整体结果", "复核状态", "证据", "规则版本", "处理时间"
];
detail.getRangeByIndexes(0, 0, 1, detailHeaders.length).values = [detailHeaders];
header(detail.getRangeByIndexes(0, 0, 1, detailHeaders.length));
const detailRows = results.map((r) => {
  const tags = [];
  if (r.defensive_answer.review_required) tags.push("语料库补充");
  if (r.timeliness.review_required) tags.push("时效性核验");
  return [
    r.excel_row, r.record_id, r.question, r.answer, r.answer_source, r.sensitive.matched_terms.join("; "), r.sensitive.final,
    r.citation.status, r.public_opinion.risk_level, r.knowledge_gap.status, r.defensive_answer.status,
    r.defensive_answer.matched_phrases.join("; "), r.timeliness.status, r.timeliness.trigger_types.join("; "), r.timeliness.matched_spans.join("; "),
    tags.join("; "), r.overall_result, r.review_status, r.evidence.map((e) => `[${e.detector}/${e.kind}] ${e.text}`).join(" | "), r.rule_version, r.processed_at,
  ];
});
detail.getRangeByIndexes(1, 0, detailRows.length, detailHeaders.length).values = detailRows;
detail.tables.add(`A1:U${detailRows.length + 1}`, true, "EvaluationDetailsV11").style = "TableStyleMedium2";
detail.getRange(`A2:U${detailRows.length + 1}`).format = { font: { name: font, size: 9 }, verticalAlignment: "top", rowHeight: 38 };
detail.getRange(`C2:U${detailRows.length + 1}`).format.wrapText = true;
detail.getRange(`U2:U${detailRows.length + 1}`).format.numberFormat = "yyyy-mm-dd hh:mm";
for (const [col, width] of [["A",10],["B",18],["C",34],["D",58],["E",16],["F",18],["G",15],["H",16],["I",14],["J",16],["K",18],["L",32],["M",18],["N",24],["O",28],["P",20],["Q",18],["R",16],["S",46],["T",14],["U",20]]) detail.getRange(`${col}:${col}`).format.columnWidth = width;
detail.getRange(`Q2:Q${detailRows.length + 1}`).conditionalFormats.add("containsText", { text: "candidate_badcase", format: { fill: color.paleRed, font: { color: color.red, bold: true } } });
detail.getRange(`P2:P${detailRows.length + 1}`).conditionalFormats.add("containsText", { text: "语料库补充", format: { fill: color.paleAmber, font: { color: "#76520D", bold: true } } });
detail.freezePanes.freezeRows(1); detail.freezePanes.freezeColumns(3);

// Corpus enrichment queue, grouped by exact and similar questions.
corpus.showGridLines = false; title(corpus, "A1:M2", "语料补充复核队列");
corpus.getRange("A3:M3").merge(); corpus.getRange("A3").values = [[`原始命中 ${corpusItems.length.toLocaleString()} 条，合并完全重复和相似问法后 ${corpusGroups.length.toLocaleString()} 个问题组。每组只需复核一次。`]];
corpus.getRange("A3:M3").format = { fill: color.paleAmber, font: { name: font, color: "#76520D" }, wrapText: true };
const corpusHeaders = ["优先级", "任务ID", "合并记录数", "样例原表行", "代表问题", "相似问法", "代表回答", "回答来源", "命中词", "复核结论", "建议补充内容", "负责人", "状态"];
corpus.getRange("A5:M5").values = [corpusHeaders]; header(corpus.getRange("A5:M5"));
const corpusRows = corpusGroups.map((g, i) => [
  [...g.terms].some((x) => x.includes("知识库仍在不断更新")) ? "P1" : "P2", `C-${String(i + 1).padStart(4, "0")}`, g.count,
  g.rows.join(", "), g.question, g.variants.join(" | "), excerpt(g.answer, 520), [...g.sources].join("; "), [...g.terms].join("; "), "pending", "", "", "待处理"
]);
corpus.getRangeByIndexes(5, 0, corpusRows.length, 13).values = corpusRows;
corpus.tables.add(`A5:M${corpusRows.length + 5}`, true, "CorpusReviewQueue").style = "TableStyleMedium2";
corpus.getRange(`A6:M${corpusRows.length + 5}`).format = { font: { name: font, size: 9 }, verticalAlignment: "top", rowHeight: 48 };
corpus.getRange(`D6:M${corpusRows.length + 5}`).format.wrapText = true;
for (const [col, width] of [["A",10],["B",12],["C",12],["D",18],["E",36],["F",46],["G",58],["H",18],["I",32],["J",18],["K",40],["L",16],["M",16]]) corpus.getRange(`${col}:${col}`).format.columnWidth = width;
corpus.getRange(`J6:J${corpusRows.length + 5}`).dataValidation = { rule: { type: "list", values: ["pending", "需补充", "无需补充", "非校园问题", "合并至现有语料"] } };
corpus.getRange(`M6:M${corpusRows.length + 5}`).dataValidation = { rule: { type: "list", values: ["待处理", "处理中", "已完成"] } };
corpus.freezePanes.freezeRows(5); corpus.freezePanes.freezeColumns(4);

// Timeliness queue, grouped by exact and similar questions.
timely.showGridLines = false; title(timely, "A1:M2", "时效性复核队列");
timely.getRange("A3:M3").merge(); timely.getRange("A3").values = [[`原始命中 ${timelyItems.length.toLocaleString()} 条，合并完全重复和相似问法后 ${timelyGroups.length.toLocaleString()} 个问题组。每组只需复核一次；命中不代表信息已过期。`]];
timely.getRange("A3:M3").format = { fill: color.paleAmber, font: { name: font, color: "#76520D" }, wrapText: true };
const timelyHeaders = ["优先级", "任务ID", "合并记录数", "样例原表行", "代表问题", "相似问法", "代表回答", "回答来源", "触发类型", "命中时间", "复核结论", "最新口径/来源", "状态"];
timely.getRange("A5:M5").values = [timelyHeaders]; header(timely.getRange("A5:M5"));
const timelyRows = timelyGroups.map((g, i) => [
  [...g.types].some((x) => ["explicit_date", "clock_time"].includes(x)) ? "P1" : "P2", `T-${String(i + 1).padStart(4, "0")}`, g.count,
  g.rows.join(", "), g.question, g.variants.join(" | "), excerpt(g.answer, 520), [...g.sources].join("; "), [...g.types].join("; "), [...g.terms].join("; "), "pending", "", "待处理"
]);
timely.getRangeByIndexes(5, 0, timelyRows.length, 13).values = timelyRows;
timely.tables.add(`A5:M${timelyRows.length + 5}`, true, "TimelinessReviewQueue").style = "TableStyleMedium2";
timely.getRange(`A6:M${timelyRows.length + 5}`).format = { font: { name: font, size: 9 }, verticalAlignment: "top", rowHeight: 48 };
timely.getRange(`D6:M${timelyRows.length + 5}`).format.wrapText = true;
for (const [col, width] of [["A",10],["B",12],["C",12],["D",18],["E",36],["F",46],["G",58],["H",18],["I",28],["J",32],["K",18],["L",40],["M",16]]) timely.getRange(`${col}:${col}`).format.columnWidth = width;
timely.getRange(`K6:K${timelyRows.length + 5}`).dataValidation = { rule: { type: "list", values: ["pending", "仍有效", "需更新", "无法确认", "历史信息"] } };
timely.getRange(`M6:M${timelyRows.length + 5}`).dataValidation = { rule: { type: "list", values: ["待处理", "处理中", "已完成"] } };
timely.freezePanes.freezeRows(5); timely.freezePanes.freezeColumns(4);

// Existing risk review queue is kept separate from content-maintenance work.
risk.showGridLines = false; title(risk, "A1:K2", "风险与引用复核队列");
risk.getRange("A3:K3").merge(); risk.getRange("A3").values = [[`共 ${riskItems.length.toLocaleString()} 条。这些任务来自敏感安全、联网引用、舆情或知识缺口规则，与新增的语料/时效队列分开处理。`]];
risk.getRange("A3:K3").format = { fill: color.paleGray, font: { name: font, color: color.ink }, wrapText: true };
const riskHeaders = ["优先级", "原表行", "问题", "答案摘要", "回答来源", "候选类型", "敏感判断", "舆情等级", "证据/理由", "复核结论", "复核备注"];
risk.getRange("A5:K5").values = [riskHeaders]; header(risk.getRange("A5:K5"));
const riskRows = riskItems.map((r) => { const [p, label] = riskLabel(r); return [p, r.excel_row, r.question, excerpt(r.answer, 360), r.answer_source, label, r.sensitive.policy_decision, r.public_opinion.risk_level, excerpt([r.sensitive.reason, r.citation.reason, r.public_opinion.reason, r.knowledge_gap.reason].join(" | "), 420), "pending", ""]; });
risk.getRangeByIndexes(5, 0, riskRows.length, 11).values = riskRows;
risk.tables.add(`A5:K${riskRows.length + 5}`, true, "RiskReviewQueueV11").style = "TableStyleMedium2";
risk.getRange(`A6:K${riskRows.length + 5}`).format = { font: { name: font, size: 9 }, verticalAlignment: "top", rowHeight: 46 };
risk.getRange(`C6:K${riskRows.length + 5}`).format.wrapText = true;
for (const [col, width] of [["A",10],["B",10],["C",38],["D",54],["E",16],["F",22],["G",15],["H",14],["I",48],["J",18],["K",32]]) risk.getRange(`${col}:${col}`).format.columnWidth = width;
risk.getRange(`J6:J${riskRows.length + 5}`).dataValidation = { rule: { type: "list", values: ["pending", "confirmed_pass", "confirmed_badcase", "ignored"] } };
risk.getRange(`A6:A${riskRows.length + 5}`).conditionalFormats.add("containsText", { text: "P0", format: { fill: color.paleRed, font: { color: color.red, bold: true } } });
risk.freezePanes.freezeRows(5); risk.freezePanes.freezeColumns(2);

// Dashboard.
dashboard.showGridLines = false;
title(dashboard, "A1:L2", `旦导风险质检 Agent V1.2  ${reportPeriod}`);
dashboard.getRange("A3:L4").merge();
dashboard.getRange("A3").values = [[`时间口径：按“${summary.time_column_used}”严格筛选 ${summary.requested_time_window}。候选仅代表需要人工复核，不代表已确认 Badcase、语料缺失或信息过期。`]];
dashboard.getRange("A3:L4").format = { fill: color.paleAmber, font: { name: font, color: "#76520D", bold: true }, wrapText: true, verticalAlignment: "center", borders: { preset: "outside", style: "thin", color: "#E4BE64" } };
const last = results.length + 1, corpusLast = corpusGroups.length + 5, timelyLast = timelyGroups.length + 5, riskLast = riskItems.length + 5;
addCard("A6:C6", "A7:C8", "扫描记录", `=COUNTA('评测明细'!$A$2:$A$${last})`);
addCard("D6:F6", "D7:F8", "候选 Badcase", `=COUNTIFS('评测明细'!$Q$2:$Q$${last},"candidate_badcase")`);
addCard("G6:I6", "G7:I8", "风险复核", `=COUNTA('风险复核'!$A$6:$A$${riskLast})`);
addCard("J6:L6", "J7:L8", "处理错误", `=${errors.length}`);
addCard("A10:C10", "A11:C12", "语料命中记录", `=SUM('语料补充'!$C$6:$C$${corpusLast})`);
addCard("D10:F10", "D11:F12", "语料问题组", `=COUNTA('语料补充'!$A$6:$A$${corpusLast})`);
addCard("G10:I10", "G11:I12", "时效命中记录", `=SUM('时效复核'!$C$6:$C$${timelyLast})`);
addCard("J10:L10", "J11:L12", "时效问题组", `=COUNTA('时效复核'!$A$6:$A$${timelyLast})`);

section(dashboard, "A14:F14", "回答来源分布");
dashboard.getRange("A15:B15").values = [["回答来源", "记录数"]]; header(dashboard.getRange("A15:B15"));
const sources = ["知识库", "AI自己回答", "联网搜索", "触发敏感词", "开启联网搜索但未回答", "空值"];
dashboard.getRange("A16:A21").values = sources.map((x) => [x]);
for (let row = 16; row <= 20; row++) dashboard.getRange(`B${row}`).formulas = [[`=COUNTIFS('评测明细'!$E$2:$E$${last},A${row})`]];
dashboard.getRange("B21").formulas = [[`=COUNTBLANK('评测明细'!$E$2:$E$${last})`]];
dashboard.getRange("A16:B21").format.borders = { preset: "inside", style: "thin", color: color.line };
const sourceChart = dashboard.charts.add("bar", dashboard.getRange("A15:B21")); sourceChart.title = "回答来源（条）"; sourceChart.hasLegend = false; sourceChart.setPosition("G14", "L24");
sourceChart.titleTextStyle.typeface = font; sourceChart.xAxis = { axisType: "textAxis", textStyle: { typeface: font, fontSize: 10 } }; sourceChart.yAxis = { numberFormatCode: "#,##0", numberFormatSourceLinked: false, textStyle: { typeface: font } };

section(dashboard, "A24:F24", "人工任务命中（记录数，可重叠）");
dashboard.getRange("A25:B25").values = [["任务类型", "记录数"]]; header(dashboard.getRange("A25:B25"));
const tasks = ["语料库补充", "时效性核验", "联网引用待核验", "敏感语境待判", "中高风险舆情", "知识缺口候选"];
dashboard.getRange("A26:A31").values = tasks.map((x) => [x]);
dashboard.getRange("B26").formulas = [[`=COUNTIFS('评测明细'!$K$2:$K$${last},"candidate_review")`]];
dashboard.getRange("B27").formulas = [[`=COUNTIFS('评测明细'!$M$2:$M$${last},"candidate_review")`]];
dashboard.getRange("B28").formulas = [[`=COUNTIFS('评测明细'!$H$2:$H$${last},"unverifiable")`]];
dashboard.getRange("B29").formulas = [[`=COUNTIFS('评测明细'!$G$2:$G$${last},"manual_review")`]];
dashboard.getRange("B30").formulas = [[`=COUNTIFS('评测明细'!$I$2:$I$${last},"high")+COUNTIFS('评测明细'!$I$2:$I$${last},"medium")`]];
dashboard.getRange("B31").formulas = [[`=COUNTIFS('评测明细'!$J$2:$J$${last},"suspected_gap")+COUNTIFS('评测明细'!$J$2:$J$${last},"confirmed_gap")`]];
dashboard.getRange("A26:B31").format.borders = { preset: "inside", style: "thin", color: color.line };
dashboard.getRange("D26:F31").merge(); dashboard.getRange("D26").values = [["解读\n\n人工队列已将完全重复和相似问法聚成问题组，按合并记录数从高到低排列。每组只需复核一次，仍可通过样例原表行回溯逐条记录。"]];
dashboard.getRange("D26:F31").format = { fill: color.paleGray, font: { name: font, color: color.ink }, wrapText: true, verticalAlignment: "top", borders: { preset: "outside", style: "thin", color: color.line } };
dashboard.getRange("A26:L31").format.rowHeight = 25;
const taskChart = dashboard.charts.add("bar", dashboard.getRange("A25:B31")); taskChart.title = "人工任务命中（条）"; taskChart.hasLegend = false; taskChart.setPosition("G25", "L35");
taskChart.titleTextStyle.typeface = font; taskChart.xAxis = { axisType: "textAxis", textStyle: { typeface: font, fontSize: 10 } }; taskChart.yAxis = { numberFormatCode: "#,##0", numberFormatSourceLinked: false, textStyle: { typeface: font } };

section(dashboard, "A36:L36", "高频人工任务示例");
dashboard.getRange("A37:L37").values = [["队列", "优先级", "出现次数", "问题", "", "", "代表回答", "", "", "", "处理建议", "任务页"]];
dashboard.getRange("D37:F37").merge(); dashboard.getRange("G37:J37").merge(); header(dashboard.getRange("A37:L37"));
const examples = [
  ...corpusGroups.slice(0, 4).map((g) => ["语料补充", [...g.terms].some((x) => x.includes("知识库仍在不断更新")) ? "P1" : "P2", g, "核对后补充可直接回答的校园语料", "语料补充"]),
  ...timelyGroups.slice(0, 4).map((g) => ["时效核验", [...g.types].some((x) => ["explicit_date", "clock_time"].includes(x)) ? "P1" : "P2", g, "核对当前通知并更新有效期", "时效复核"]),
];
examples.forEach(([queue, priority, g, action, page], idx) => {
  const row = 38 + idx; dashboard.getRange(`D${row}:F${row}`).merge(); dashboard.getRange(`G${row}:J${row}`).merge();
  dashboard.getRange(`A${row}:C${row}`).values = [[queue, priority, g.count]];
  dashboard.getRange(`D${row}`).values = [[excerpt(g.question, 160)]]; dashboard.getRange(`G${row}`).values = [[excerpt(g.answer, 220)]];
  dashboard.getRange(`K${row}:L${row}`).values = [[action, page]];
  dashboard.getRange(`A${row}:L${row}`).format = { fill: idx % 2 ? color.paleGray : color.white, font: { name: font, size: 9 }, wrapText: true, verticalAlignment: "top", borders: { preset: "inside", style: "thin", color: color.line } };
  dashboard.getRange(`A${row}:L${row}`).format.rowHeight = 48;
});
section(dashboard, "A47:L47", "建议的试运行方式");
dashboard.getRange("A48:L51").merge(); dashboard.getRange("A48").values = [["先从两个问题组队列各取前100个高频任务进行人工复核。语料队列重点判断现有内容是否不足；时效队列重点核对年份、截止日期、办理时间和官方来源。根据首轮误报情况继续校准聚类与命中规则。"]];
dashboard.getRange("A48:L51").format = { fill: color.paleGreen, font: { name: font, color: "#285A3E" }, wrapText: true, verticalAlignment: "top", borders: { preset: "outside", style: "thin", color: "#AED1BC" } };
for (const [col, width] of [["A",16],["B",13],["C",13],["D",16],["E",16],["F",16],["G",16],["H",16],["I",16],["J",16],["K",22],["L",16]]) dashboard.getRange(`${col}:${col}`).format.columnWidth = width;
dashboard.getRange("A1:L51").format.font.name = font; dashboard.freezePanes.freezeRows(4);

// Run notes.
notes.showGridLines = false; title(notes, "A1:B2", "运行口径与限制");
const noteRows = [
  ["项目", "内容"], ["输入文件", summary.input_file], ["工作表", summary.sheet], ["请求时间窗口", summary.requested_time_window],
  ["时间筛选方式", `使用“${summary.time_column_used}”逐条筛选，方式为 ${summary.time_filter_mode}。`],
  ["读取记录", summary.input_rows_read], ["有效回答", summary.answered_count], ["隐私处理", `未输出 ${summary.privacy_columns_removed_count} 个身份类字段，正文同值脱敏 ${summary.redacted_identity_occurrences} 处。`],
  ["规则版本", summary.rule_version], ["外部模型", "未启用。本次为可审计规则筛选。"],
  ["语料补充口径", "回答同时出现信息不足和转辅导员/部门/官方通知时命中。安全拒答不进入该队列。"],
  ["时效性口径", "含具体年月日、钟点、时长，或问题含今年/最新/什么时候/截止时间等表达。命中不等于已过期。"],
  ["队列合并", "完全重复与相似问法聚为一个人工任务，并保留代表问题、变体、频次及原表行；关键数字不同的问题默认分开。"],
  ["联网引用", `适用 ${summary.citation_eligible_count} 条。V1.2 未抓取引用页正文，保持 unverifiable，不代表已确认错误。`],
  ["处理错误", errors.length],
];
notes.getRangeByIndexes(3, 0, noteRows.length, 2).values = noteRows; header(notes.getRange("A4:B4"));
notes.getRange(`A5:A${noteRows.length + 3}`).format = { fill: color.sky, font: { name: font, bold: true, color: color.navy } };
notes.getRange(`B5:B${noteRows.length + 3}`).format.wrapText = true; notes.getRange(`A4:B${noteRows.length + 3}`).format.borders = { preset: "inside", style: "thin", color: color.line };
notes.getRange("A:A").format.columnWidth = 24; notes.getRange("B:B").format.columnWidth = 94; notes.getRange(`A5:B${noteRows.length + 3}`).format.rowHeight = 34; notes.freezePanes.freezeRows(4);

wb.recalculate();
await fs.mkdir(runDir, { recursive: true });
const exported = await SpreadsheetFile.exportXlsx(wb); await exported.save(outputPath);

const previewRanges = {
  "汇总看板": "A1:L51", "语料补充": "A1:M30", "时效复核": "A1:M30", "风险复核": "A1:K30", "评测明细": "A1:U25", "运行说明": `A1:B${noteRows.length + 3}`,
};
for (const [sheetName, range] of Object.entries(previewRanges)) {
  const preview = await wb.render({ sheetName, range, scale: 1, format: "png" });
  await fs.writeFile(path.join(runDir, `qa_${sheetName}_v12.png`), new Uint8Array(await preview.arrayBuffer()));
}
const formulaErrors = await wb.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!", options: { useRegex: true, maxResults: 300 }, summary: "final formula error scan", maxChars: 4000 });
const dashboardCheck = await wb.inspect({ kind: "table", range: "汇总看板!A1:L35", include: "values,formulas", tableMaxRows: 35, tableMaxCols: 12, maxChars: 16000 });
console.log(JSON.stringify({ outputPath, resultCount: results.length, corpusRows: corpusItems.length, corpusTasks: corpusGroups.length, timelyRows: timelyItems.length, timelyTasks: timelyGroups.length, riskRows: riskItems.length, formulaErrors: formulaErrors.ndjson, dashboardCheck: dashboardCheck.ndjson }, null, 2));
