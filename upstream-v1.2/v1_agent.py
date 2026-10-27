from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field, model_validator


RULE_VERSION = "v1.2.0"
PII_COLUMNS = {"用户名", "真实姓名", "昵称"}
REQUIRED_COLUMNS = {"问题", "答案", "回答来源", "联网搜索引用来源"}


class Evidence(BaseModel):
    detector: str
    kind: str
    text: str
    source_url: str | None = None


class SensitiveResult(BaseModel):
    keyword: Literal["hit", "miss"]
    matched_terms: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    policy_decision: Literal["allow", "restrict", "manual_review", "not_applicable"]
    answer_compliance: Literal["compliant", "non_compliant", "not_applicable"]
    final: Literal["pass", "badcase", "manual_review", "not_applicable"]
    reason: str


class CitationResult(BaseModel):
    status: Literal[
        "supported",
        "partially_supported",
        "contradicted",
        "unsupported",
        "unverifiable",
        "not_applicable",
    ]
    reason: str
    parsed_urls: list[str] = Field(default_factory=list)


class PublicOpinionResult(BaseModel):
    risk_level: Literal["high", "medium", "low", "none", "not_applicable"]
    risk_categories: list[str] = Field(default_factory=list)
    fudan_entities: list[str] = Field(default_factory=list)
    evidence_spans: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    reason: str
    review_required: bool

    @model_validator(mode="after")
    def require_evidence_for_high(self) -> "PublicOpinionResult":
        if self.risk_level == "high" and not self.evidence_spans:
            raise ValueError("public opinion high risk requires evidence_spans")
        return self


class KnowledgeGapResult(BaseModel):
    status: Literal["confirmed_gap", "suspected_gap", "not_gap", "not_applicable"]
    reason: str
    missing_backend_fields: list[str] = Field(default_factory=list)


class DefensiveAnswerResult(BaseModel):
    status: Literal["candidate_review", "not_detected", "not_applicable"]
    categories: list[str] = Field(default_factory=list)
    matched_phrases: list[str] = Field(default_factory=list)
    reason: str
    review_required: bool


class TimelinessResult(BaseModel):
    status: Literal["candidate_review", "not_detected", "not_applicable"]
    trigger_types: list[str] = Field(default_factory=list)
    matched_spans: list[str] = Field(default_factory=list)
    reason: str
    review_required: bool


class RecordResult(BaseModel):
    record_id: str
    excel_row: int
    question: str
    answer: str
    answer_source: str
    citation_raw: str
    sensitive: SensitiveResult
    citation: CitationResult
    public_opinion: PublicOpinionResult
    knowledge_gap: KnowledgeGapResult
    defensive_answer: DefensiveAnswerResult
    timeliness: TimelinessResult
    overall_result: Literal["pass", "candidate_badcase", "manual_review"]
    run_status: Literal["success", "partial", "failed", "skipped"]
    review_status: Literal["pending", "confirmed_pass", "confirmed_badcase", "ignored"]
    evidence: list[Evidence] = Field(default_factory=list)
    rule_version: str
    prompt_version: str
    processed_at: str
    llm_reviewed: bool = False
    llm_note: str = ""


def read_json_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def normalized_text(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_question_for_grouping(question: str) -> str:
    """Normalize harmless wording differences while preserving business meaning."""
    text = normalized_text(question).lower()
    replacements = {
        "如何": "怎么",
        "怎样": "怎么",
        "缴纳": "交",
        "支付": "交",
        "需要": "要",
        "可以": "能",
        "是否": "能否",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(r"^(请问|麻烦问下|麻烦问一下|想问下|想问一下|我想知道|学生咨询)", "", text)
    text = re.sub(r"^(我|本人)", "", text)
    text = re.sub(r"(呢|呀|啊|吗|么|一下|谢谢|感谢)$", "", text)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def _question_ngrams(text: str, size: int = 2) -> set[str]:
    if len(text) <= size:
        return {text} if text else set()
    return {text[index:index + size] for index in range(len(text) - size + 1)}


def question_similarity(left: str, right: str) -> float:
    left_norm = normalize_question_for_grouping(left)
    right_norm = normalize_question_for_grouping(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0

    # Different explicit numbers often mean different years, campuses, buildings or deadlines.
    left_numbers = set(re.findall(r"\d+", left_norm))
    right_numbers = set(re.findall(r"\d+", right_norm))
    if left_numbers and right_numbers and left_numbers != right_numbers:
        return 0.0

    shorter, longer = sorted((left_norm, right_norm), key=len)
    if len(shorter) >= 5 and shorter in longer and len(shorter) / len(longer) >= 0.55:
        return 0.92

    left_grams = _question_ngrams(left_norm)
    right_grams = _question_ngrams(right_norm)
    dice = 2 * len(left_grams & right_grams) / max(1, len(left_grams) + len(right_grams))
    sequence = SequenceMatcher(None, left_norm, right_norm).ratio()
    return 0.55 * sequence + 0.45 * dice


def group_similar_candidate_rows(
    rows: list[dict[str, Any]],
    *,
    threshold: float = 0.82,
) -> list[dict[str, Any]]:
    """Cluster exact and near-duplicate questions into one human-review task."""
    groups: list[dict[str, Any]] = []
    ngram_index: dict[str, set[int]] = {}

    for row in rows:
        question = normalized_text(row.get("问题"))
        normalized = normalize_question_for_grouping(question)
        grams = _question_ngrams(normalized)
        candidate_ids: set[int] = set()
        for gram in grams:
            candidate_ids.update(ngram_index.get(gram, set()))

        best_id: int | None = None
        best_score = 0.0
        for group_id in candidate_ids:
            representative = groups[group_id]["代表问题"]
            score = question_similarity(question, representative)
            if score >= threshold and score > best_score:
                best_id, best_score = group_id, score

        if best_id is None:
            best_id = len(groups)
            groups.append(
                {
                    "代表问题": question,
                    "出现次数": 0,
                    "问题变体": [],
                    "样例原表行": [],
                    "成员": [],
                }
            )
            for gram in grams:
                ngram_index.setdefault(gram, set()).add(best_id)

        group = groups[best_id]
        group["出现次数"] += 1
        if question and question not in group["问题变体"] and len(group["问题变体"]) < 8:
            group["问题变体"].append(question)
        excel_row = row.get("excel_row")
        if excel_row not in group["样例原表行"] and len(group["样例原表行"]) < 12:
            group["样例原表行"].append(excel_row)
        group["成员"].append(row)

    return sorted(groups, key=lambda group: (-group["出现次数"], group["代表问题"]))


def flatten_candidate_groups(groups: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for index, group in enumerate(groups, start=1):
        representative = dict(group["成员"][0])
        representative.update(
            {
                "任务组ID": f"{prefix}-{index:04d}",
                "代表问题": group["代表问题"],
                "合并记录数": group["出现次数"],
                "相似问题变体": " | ".join(group["问题变体"]),
                "样例原表行": "; ".join(str(value) for value in group["样例原表行"] if value is not None),
            }
        )
        flattened.append(representative)
    return flattened


def compile_identity_pattern(identity_values: set[str]) -> re.Pattern[str] | None:
    if not identity_values:
        return None
    alternatives = "|".join(re.escape(value) for value in sorted(identity_values, key=len, reverse=True))
    return re.compile(alternatives)


def redact_identity_values(text: str, identity_pattern: re.Pattern[str] | set[str] | None) -> tuple[str, int]:
    if isinstance(identity_pattern, set):
        identity_pattern = compile_identity_pattern(identity_pattern)
    if identity_pattern is None:
        return text, 0
    return identity_pattern.subn("[已脱敏]", text)


def stable_record_id(question: str, answer: str, source: str, citations: str) -> str:
    raw = "\x1f".join([question, answer, source, citations]).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def find_terms(text: str, terms: list[str]) -> list[str]:
    lowered = text.lower()
    return sorted({term for term in terms if term.lower() in lowered})


def contains_any(text: str, terms: list[str]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def extract_urls(text: str) -> list[str]:
    return re.findall(r"https?://[^\s\]\[<>\"']+", text)


def detect_sensitive(
    question: str,
    answer_source: str,
    config: dict[str, Any],
    answer: str = "",
) -> tuple[SensitiveResult, list[Evidence]]:
    matches: list[str] = []
    categories: list[str] = []
    restrict = False
    allowed_context = False
    crisis = False
    evidence: list[Evidence] = []

    for category in config["categories"]:
        hit_terms = find_terms(question, category["terms"])
        if not hit_terms:
            continue
        matches.extend(hit_terms)
        categories.append(category["id"])
        evidence.append(Evidence(detector="sensitive", kind="keyword", text=f"{category['policy_id']}: {', '.join(hit_terms)}"))
        if category["id"] == "psychological_crisis":
            crisis = not contains_any(question, category.get("allow_context", []))
            continue
        if contains_any(question, category.get("allow_context", [])):
            allowed_context = True
        if contains_any(question, category.get("restrict_actions", [])):
            restrict = True

    logged_block = answer_source == "触发敏感词"
    refusal_phrases = config.get(
        "safe_refusal_phrases",
        ["抱歉", "无法", "不能", "不会", "不提供", "拒绝", "保密", "暂时没有相关信息"],
    )
    safe_refusal = bool(answer) and contains_any(answer, refusal_phrases)
    if not matches:
        return (
            SensitiveResult(
                keyword="miss",
                policy_decision="not_applicable",
                answer_compliance="not_applicable",
                final="not_applicable",
                reason="未命中 V1 敏感候选规则。",
            ),
            evidence,
        )

    if crisis:
        decision = "manual_review"
        reason = "命中心理危机表达，应转人工关注，不能按普通违禁意图自动处理。"
    elif restrict and not allowed_context:
        decision = "restrict"
        reason = "命中风险对象与操作性动作组合，按 V1 政策应限制。"
    elif allowed_context and not restrict:
        decision = "allow"
        reason = "命中词出现在公开知识、安全教育、排障或求助语境中。"
    else:
        decision = "manual_review"
        reason = "仅凭关键词或当前上下文不足以确认违规意图。"

    if decision == "restrict" and (logged_block or safe_refusal):
        compliance, final = "compliant", "pass"
        if safe_refusal and not logged_block:
            reason += " 回答中检测到拒答或保密表达，未见明显泄露。"
    elif decision == "restrict" and not logged_block:
        compliance, final = "non_compliant", "badcase"
    elif decision == "allow" and logged_block:
        compliance, final = "non_compliant", "manual_review"
        reason += " 日志记录为触发敏感词，疑似误拦截。"
    elif decision == "allow":
        compliance, final = "compliant", "pass"
    else:
        compliance, final = "not_applicable", "manual_review"

    return (
        SensitiveResult(
            keyword="hit",
            matched_terms=sorted(set(matches)),
            categories=sorted(set(categories)),
            policy_decision=decision,
            answer_compliance=compliance,
            final=final,
            reason=reason,
        ),
        evidence,
    )


def detect_citation(answer: str, answer_source: str, citation_raw: str) -> tuple[CitationResult, list[Evidence]]:
    if answer_source != "联网搜索" or not answer:
        return CitationResult(status="not_applicable", reason="不属于‘联网搜索且答案非空’检测范围。"), []
    urls = extract_urls(citation_raw)
    if not citation_raw or not urls:
        return CitationResult(status="unverifiable", reason="联网回答缺少可解析引用，无法完成证据判断。"), []
    evidence = [Evidence(detector="citation", kind="source_url", text=url, source_url=url) for url in urls]
    return (
        CitationResult(
            status="unverifiable",
            reason="V1 测试未抓取引用正文；按降级规则保持 unverifiable，不能写成 unsupported。",
            parsed_urls=urls,
        ),
        evidence,
    )


def evidence_span(text: str, term: str, radius: int = 45) -> str:
    idx = text.find(term)
    if idx < 0:
        return ""
    return text[max(0, idx - radius) : min(len(text), idx + len(term) + radius)]


def detect_public_opinion(
    question: str,
    answer: str,
    citation_raw: str,
    config: dict[str, Any],
) -> tuple[PublicOpinionResult, list[Evidence]]:
    combined = f"{question}\n{answer}"
    entities = find_terms(combined, config["fudan_entities"])
    urls = extract_urls(citation_raw)
    if not entities:
        return (
            PublicOpinionResult(
                risk_level="not_applicable",
                reason="当前问题和回答未明确命中复旦实体。",
                review_required=False,
            ),
            [],
        )

    high_question_hits = find_terms(question, config["high_terms"])
    high_answer_hits = find_terms(answer, config["high_terms"])
    medium_question_hits = find_terms(question, config["medium_terms"])
    medium_answer_hits = find_terms(answer, config["medium_terms"])
    low_hits = find_terms(question, config["low_terms"])
    privacy_terms = ["身份证号", "手机号", "宿舍号", "泄露隐私"]
    privacy_actions = ["给我", "发我", "查询", "查一下", "公开", "传播", "曝光", "获取"]
    privacy_high = bool(find_terms(question, privacy_terms)) and contains_any(question, privacy_actions)
    accusation_context = contains_any(
        combined,
        ["涉嫌", "被指", "据说", "听说", "爆料", "曝光", "内幕", "传闻", "正在发生", "事故", "事件"],
    )
    substantive_question_high = [term for term in high_question_hits if term not in privacy_terms]
    answer_amplification_high = bool(high_answer_hits) and accusation_context

    if privacy_high or substantive_question_high or answer_amplification_high:
        level, categories, review = "high", ["严重指控/安全/隐私候选"], True
        trigger_terms = sorted(set(high_question_hits + (high_answer_hits if answer_amplification_high else [])))
        reason = "明确涉及复旦实体并命中高风险表达；仅表示必须人工复核，不代表指控属实。"
    elif medium_question_hits or (medium_answer_hits and accusation_context):
        level, categories, review = "medium", ["传闻/指控/情绪化扩写候选"], True
        trigger_terms = sorted(set(medium_question_hits + (medium_answer_hits if accusation_context else [])))
        reason = "明确涉及复旦实体并命中传闻或指控表达，需要人工核验。"
    elif low_hits:
        level, categories, review = "low", ["一般投诉或服务故障"], False
        trigger_terms = low_hits
        reason = "属于一般服务故障或负面体验，未发现隐私、严重指控或群体动员。"
    else:
        level, categories, review = "none", [], False
        trigger_terms = []
        reason = "命中复旦实体，但内容为中性校园信息或公开办事知识。"

    spans = [evidence_span(combined, term) for term in trigger_terms]
    spans = [span for span in spans if span]
    evidence = [Evidence(detector="public_opinion", kind="text_span", text=span) for span in spans]
    return (
        PublicOpinionResult(
            risk_level=level,
            risk_categories=categories,
            fudan_entities=entities,
            evidence_spans=spans,
            source_urls=urls,
            reason=reason,
            review_required=review,
        ),
        evidence,
    )


def detect_knowledge_gap(
    row: dict[str, Any],
    question: str,
    answer: str,
    answer_source: str,
    sensitive: SensitiveResult,
    config: dict[str, Any],
) -> tuple[KnowledgeGapResult, list[Evidence]]:
    if answer_source in config["excluded_sources"] or sensitive.final in {"badcase", "manual_review"}:
        return KnowledgeGapResult(status="not_applicable", reason="按检测顺序被敏感或来源排除。"), []

    backend_fields = ["knowledge_base_searched", "knowledge_base_hit", "retrieved_doc_count"]
    if all(field in row for field in backend_fields):
        searched = bool(row.get("knowledge_base_searched"))
        hit = bool(row.get("knowledge_base_hit"))
        count = row.get("retrieved_doc_count")
        if searched and not hit and (count in (0, "0", None)):
            return KnowledgeGapResult(status="confirmed_gap", reason="后台字段确认已检索知识库但未命中。"), []
        return KnowledgeGapResult(status="not_gap", reason="后台检索字段不满足 confirmed_gap 条件。"), []

    campus = contains_any(question, config["campus_terms"])
    no_info_hits = find_terms(answer, config["no_info_phrases"])
    if answer_source == "AI自己回答" and campus and no_info_hits:
        evidence = [Evidence(detector="knowledge_gap", kind="no_info_phrase", text=term) for term in no_info_hits]
        return (
            KnowledgeGapResult(
                status="suspected_gap",
                reason="校园问题由 AI 自己回答且包含无资料表达；缺少后台检索字段，只能作为疑似候选。",
                missing_backend_fields=backend_fields,
            ),
            evidence,
        )
    return (
        KnowledgeGapResult(
            status="not_gap",
            reason="未同时满足校园问题、AI 自己回答和明确无资料表达的临时弱规则。",
            missing_backend_fields=backend_fields,
        ),
        [],
    )


def detect_defensive_answer(
    answer: str,
    answer_source: str,
    sensitive: SensitiveResult,
    config: dict[str, Any],
) -> tuple[DefensiveAnswerResult, list[Evidence]]:
    """Find weak fallback answers that should become corpus-enrichment tasks.

    A redirect alone is not enough: a complete answer may reasonably end with
    "contact the counselor if needed". V1.1 therefore requires both an explicit
    information-gap signal and a redirect action/target.
    """
    if not answer or answer_source == "触发敏感词" or sensitive.policy_decision == "restrict":
        return (
            DefensiveAnswerResult(
                status="not_applicable",
                reason="空回答、敏感拦截或安全拒答不属于语料补充筛选范围。",
                review_required=False,
            ),
            [],
        )

    rule = config["defensive_answer"]
    insufficient = find_terms(answer, rule["insufficient_info_phrases"])
    actions = find_terms(answer, rule["redirect_actions"])
    targets = find_terms(answer, rule["redirect_targets"])
    redirect = bool(actions or targets)

    if insufficient and redirect:
        matched = sorted(set(insufficient + actions + targets))
        categories = ["insufficient_information"]
        if actions:
            categories.append("redirect_action")
        if targets:
            categories.append("redirect_target")
        evidence = [
            Evidence(detector="defensive_answer", kind="matched_phrase", text=term)
            for term in matched
        ]
        return (
            DefensiveAnswerResult(
                status="candidate_review",
                categories=categories,
                matched_phrases=matched,
                reason="回答同时表示信息不足，并将用户引导至辅导员、部门或官方通知；建议人工核对后补充语料库。",
                review_required=True,
            ),
            evidence,
        )

    return (
        DefensiveAnswerResult(
            status="not_detected",
            matched_phrases=sorted(set(insufficient + actions + targets)),
            reason="未同时命中‘信息不足’和‘向人工/官方渠道引导’两类信号。",
            review_required=False,
        ),
        [],
    )


def _regex_matches(text: str, pattern: str) -> list[str]:
    return [match.group(0) for match in re.finditer(pattern, text, flags=re.IGNORECASE)]


def detect_timeliness(
    question: str,
    answer: str,
    config: dict[str, Any],
) -> tuple[TimelinessResult, list[Evidence]]:
    """Select Q&A that may become stale; this does not decide freshness."""
    if not question and not answer:
        return (
            TimelinessResult(
                status="not_applicable",
                reason="问题和回答均为空。",
                review_required=False,
            ),
            [],
        )

    combined = f"{question}\n{answer}"
    pattern_specs = [
        ("explicit_date", r"(?<!\d)(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}(?!\d)"),
        ("explicit_date", r"(?<!\d)(?:19|20)\d{2}年(?:\d{1,2}月(?:\d{1,2}日?)?)?"),
        ("explicit_date", r"(?<!\d)\d{1,2}月\d{1,2}日(?!\d)"),
        ("date_period", r"(?<!\d)\d{1,2}月(?:上旬|中旬|下旬)(?!\d)"),
        ("clock_time", r"(?<!\d)(?:(?:[01]?\d|2[0-3])[:：][0-5]\d|24[:：]00)(?!\d)"),
        ("duration", r"(?<!\d)\d{1,3}\s*(?:天|周|个月|月|学期)(?!\d)"),
    ]
    match_pairs: list[tuple[str, str]] = []
    for trigger_type, pattern in pattern_specs:
        found = _regex_matches(combined, pattern)
        match_pairs.extend((trigger_type, span) for span in found)

    query_hits = find_terms(question, config["timeliness"]["query_phrases"])
    match_pairs.extend(("recency_or_schedule_query", term) for term in query_hits)

    match_pairs = sorted(set(match_pairs))
    trigger_types = sorted({trigger_type for trigger_type, _ in match_pairs})
    spans = sorted({span for _, span in match_pairs})
    if trigger_types:
        evidence = [
            Evidence(detector="timeliness", kind=trigger_type, text=span)
            for trigger_type, span in match_pairs
        ]
        return (
            TimelinessResult(
                status="candidate_review",
                trigger_types=trigger_types,
                matched_spans=spans,
                reason="问答中含具体日期/时间/时长，或用户明确询问当前时间安排；需人工确认信息是否仍然有效。",
                review_required=True,
            ),
            evidence,
        )

    return (
        TimelinessResult(
            status="not_detected",
            reason="未命中具体日期、钟点、时长或时效性询问表达。",
            review_required=False,
        ),
        [],
    )


def llm_review_candidate(result: RecordResult, config: dict[str, Any]) -> tuple[bool, str]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_MODEL", "").strip()
    if not api_key or not model:
        return False, "未设置 OPENAI_API_KEY/OPENAI_MODEL，按规则候选＋人工复核降级。"

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "record_id": {"type": "string"},
            "sensitive_decision": {"type": "string", "enum": ["allow", "restrict", "manual_review"]},
            "public_opinion_level": {"type": "string", "enum": ["high", "medium", "low", "none"]},
            "reason": {"type": "string"},
        },
        "required": ["record_id", "sensitive_decision", "public_opinion_level", "reason"],
    }
    payload = {
        "model": model,
        "store": False,
        "instructions": config["instructions"],
        "input": json.dumps(
            {
                "record_id": result.record_id,
                "question": result.question,
                "answer": result.answer,
                "rule_candidate": {
                    "sensitive": result.sensitive.model_dump(),
                    "public_opinion": result.public_opinion.model_dump(),
                },
            },
            ensure_ascii=False,
        ),
        "text": {"format": {"type": "json_schema", "name": "risk_review", "strict": True, "schema": schema}},
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.load(response)
        output_text = ""
        for item in body.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    output_text = content.get("text", "")
        parsed = json.loads(output_text)
        return True, json.dumps(parsed, ensure_ascii=False)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
        return False, f"LLM 复核失败，保留人工复核：{type(exc).__name__}: {exc}"


def flatten_result(result: RecordResult) -> dict[str, Any]:
    manual_task_tags: list[str] = []
    if result.defensive_answer.review_required:
        manual_task_tags.append("语料库补充")
    if result.timeliness.review_required:
        manual_task_tags.append("时效性核验")
    return {
        "record_id": result.record_id,
        "excel_row": result.excel_row,
        "问题": result.question,
        "答案": result.answer,
        "回答来源": result.answer_source,
        "联网搜索引用来源": result.citation_raw,
        "敏感关键词": "; ".join(result.sensitive.matched_terms),
        "敏感类别": "; ".join(result.sensitive.categories),
        "敏感政策判断": result.sensitive.policy_decision,
        "敏感最终状态": result.sensitive.final,
        "敏感理由": result.sensitive.reason,
        "引用状态": result.citation.status,
        "引用理由": result.citation.reason,
        "舆情等级": result.public_opinion.risk_level,
        "舆情类别": "; ".join(result.public_opinion.risk_categories),
        "复旦实体": "; ".join(result.public_opinion.fudan_entities),
        "舆情证据": " | ".join(result.public_opinion.evidence_spans),
        "知识缺口": result.knowledge_gap.status,
        "知识缺口理由": result.knowledge_gap.reason,
        "防御性回答": result.defensive_answer.status,
        "防御性回答类别": "; ".join(result.defensive_answer.categories),
        "防御性回答命中词": "; ".join(result.defensive_answer.matched_phrases),
        "防御性回答理由": result.defensive_answer.reason,
        "时效性复核": result.timeliness.status,
        "时效性触发类型": "; ".join(result.timeliness.trigger_types),
        "时效性命中片段": "; ".join(result.timeliness.matched_spans),
        "时效性理由": result.timeliness.reason,
        "人工任务标签": "; ".join(manual_task_tags),
        "整体结果": result.overall_result,
        "人工复核状态": result.review_status,
        "证据": " | ".join(f"[{e.detector}/{e.kind}] {e.text}" for e in result.evidence),
        "规则版本": result.rule_version,
        "Prompt版本": result.prompt_version,
        "模型已复核": result.llm_reviewed,
        "模型备注": result.llm_note,
        "处理时间": result.processed_at,
    }


def persist_sqlite(db_path: Path, results: list[RecordResult]) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS results (
                record_id TEXT PRIMARY KEY,
                excel_row INTEGER NOT NULL,
                overall_result TEXT NOT NULL,
                review_status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # A completed batch replaces the prior snapshot in this run directory.
        # The transaction prevents half-cleared output if persistence fails.
        connection.execute("DELETE FROM results")
        for result in results:
            connection.execute(
                """
                INSERT INTO results(record_id, excel_row, overall_result, review_status, payload_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_id) DO UPDATE SET
                    excel_row=excluded.excel_row,
                    overall_result=excluded.overall_result,
                    review_status=excluded.review_status,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    result.record_id,
                    result.excel_row,
                    result.overall_result,
                    result.review_status,
                    result.model_dump_json(),
                    result.processed_at,
                ),
            )
        connection.commit()
    finally:
        connection.close()


def run(args: argparse.Namespace) -> int:
    base_dir = Path(__file__).resolve().parent
    config_dir = base_dir / "config"
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    errors: list[dict[str, Any]] = []

    sensitive_config = read_json_yaml(config_dir / "sensitive_keywords.yaml")
    opinion_config = read_json_yaml(config_dir / "public_opinion.yaml")
    gap_config = read_json_yaml(config_dir / "knowledge_gap.yaml")
    maintenance_config = read_json_yaml(config_dir / "content_maintenance.yaml")
    prompt_config = read_json_yaml(config_dir / "llm_prompts.yaml")

    try:
        input_path = Path(args.input)
        if input_path.suffix.lower() == ".csv":
            frame = pd.read_csv(input_path, nrows=args.limit)
        else:
            engine = None
            if input_path.suffix.lower() == ".xls":
                with input_path.open("rb") as stream:
                    if stream.read(4).startswith(b"PK"):
                        engine = "openpyxl"
            frame = pd.read_excel(args.input, sheet_name=args.sheet, nrows=args.limit, engine=engine)
    except Exception as exc:
        print(f"导入失败: {exc}", file=sys.stderr)
        return 2

    missing = sorted(REQUIRED_COLUMNS - set(frame.columns))
    if missing:
        print(f"缺少必需列: {missing}", file=sys.stderr)
        return 2

    source_row_count = len(frame)
    requested_time_window = ""
    time_filter_mode = "not_requested"
    time_column_used = ""
    if args.start_time or args.end_time:
        requested_time_window = f"{args.start_time or '未指定'} 至 {args.end_time or '未指定'}"
        candidates = [args.time_column] if args.time_column else ["提问时间", "创建时间", "时间", "timestamp", "created_at", "create_time"]
        time_column_used = next((column for column in candidates if column and column in frame.columns), "")
        if time_column_used:
            parsed_time = pd.to_datetime(frame[time_column_used], errors="coerce")
            mask = parsed_time.notna()
            if args.start_time:
                mask &= parsed_time >= pd.Timestamp(args.start_time)
            if args.end_time:
                mask &= parsed_time < pd.Timestamp(args.end_time)
            frame = frame.loc[mask].copy()
            time_filter_mode = "filtered_by_column"
        elif args.assume_prefiltered:
            time_filter_mode = "source_file_assumed_prefiltered"
        else:
            print("请求了时间窗口，但文件中没有可用时间列。若源文件已预先筛选，请增加 --assume-prefiltered。", file=sys.stderr)
            return 2
    privacy_columns_found = sorted(PII_COLUMNS & set(frame.columns))
    identity_values = {
        normalized_text(value)
        for column in privacy_columns_found
        for value in frame[column].dropna().tolist()
        if len(normalized_text(value)) >= 2
    }
    identity_pattern = compile_identity_pattern(identity_values)
    frame = frame.drop(columns=privacy_columns_found, errors="ignore")
    results: list[RecordResult] = []
    redacted_identity_occurrences = 0

    for index, series in frame.iterrows():
        excel_row = index + 2
        try:
            raw = series.to_dict()
            question, count_q = redact_identity_values(normalized_text(raw.get("问题")), identity_pattern)
            answer, count_a = redact_identity_values(normalized_text(raw.get("答案")), identity_pattern)
            answer_source = normalized_text(raw.get("回答来源"))
            citation_raw, count_c = redact_identity_values(normalized_text(raw.get("联网搜索引用来源")), identity_pattern)
            redacted_identity_occurrences += count_q + count_a + count_c
            record_id = stable_record_id(question, answer, answer_source, citation_raw)

            sensitive, ev1 = detect_sensitive(question, answer_source, sensitive_config, answer=answer)
            citation, ev2 = detect_citation(answer, answer_source, citation_raw)
            opinion, ev3 = detect_public_opinion(question, answer, citation_raw, opinion_config)
            gap, ev4 = detect_knowledge_gap(raw, question, answer, answer_source, sensitive, gap_config)
            defensive, ev5 = detect_defensive_answer(answer, answer_source, sensitive, maintenance_config)
            timeliness, ev6 = detect_timeliness(question, answer, maintenance_config)
            evidence = ev1 + ev2 + ev3 + ev4 + ev5 + ev6

            if sensitive.final == "badcase":
                overall, review = "candidate_badcase", "pending"
            elif (
                sensitive.final == "manual_review"
                or citation.status in {
                    "partially_supported",
                    "contradicted",
                    "unsupported",
                    "unverifiable",
                }
                or opinion.review_required
                or gap.status in {"suspected_gap", "confirmed_gap"}
                or defensive.review_required
                or timeliness.review_required
            ):
                overall, review = "manual_review", "pending"
            else:
                overall, review = "pass", "ignored"

            now = datetime.now(timezone.utc).isoformat()
            result = RecordResult(
                record_id=record_id,
                excel_row=excel_row,
                question=question,
                answer=answer,
                answer_source=answer_source,
                citation_raw=citation_raw,
                sensitive=sensitive,
                citation=citation,
                public_opinion=opinion,
                knowledge_gap=gap,
                defensive_answer=defensive,
                timeliness=timeliness,
                overall_result=overall,
                run_status="success",
                review_status=review,
                evidence=evidence,
                rule_version=RULE_VERSION,
                prompt_version=prompt_config["prompt_version"],
                processed_at=now,
            )
            if args.use_llm and overall != "pass":
                reviewed, note = llm_review_candidate(result, prompt_config)
                result.llm_reviewed = reviewed
                result.llm_note = note
                if not reviewed:
                    result.run_status = "partial"
            results.append(result)
        except Exception as exc:
            errors.append({"excel_row": excel_row, "error_type": type(exc).__name__, "error": str(exc)})

    persist_sqlite(output_dir / "quality_agent.sqlite", results)
    with (output_dir / "results.jsonl").open("w", encoding="utf-8") as fh:
        for result in results:
            fh.write(result.model_dump_json() + "\n")

    flat_rows = [flatten_result(result) for result in results]
    if flat_rows:
        with (output_dir / "results.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(flat_rows[0].keys()))
            writer.writeheader()
            writer.writerows(flat_rows)
        candidate_exports = {
            "corpus_enrichment_candidates.csv": [
                row for row, result in zip(flat_rows, results) if result.defensive_answer.review_required
            ],
            "timeliness_candidates.csv": [
                row for row, result in zip(flat_rows, results) if result.timeliness.review_required
            ],
        }
        for filename, rows in candidate_exports.items():
            with (output_dir / filename).open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(flat_rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)

        grouped_exports = {
            "corpus_enrichment_review_groups.csv": flatten_candidate_groups(
                group_similar_candidate_rows(candidate_exports["corpus_enrichment_candidates.csv"]), "C"
            ),
            "timeliness_review_groups.csv": flatten_candidate_groups(
                group_similar_candidate_rows(candidate_exports["timeliness_candidates.csv"]), "T"
            ),
        }
        for filename, rows in grouped_exports.items():
            if not rows:
                continue
            with (output_dir / filename).open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
            (output_dir / filename.replace(".csv", ".json")).write_text(
                json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    counters = {
        "answer_source": Counter(result.answer_source or "空值" for result in results),
        "overall_result": Counter(result.overall_result for result in results),
        "sensitive_final": Counter(result.sensitive.final for result in results),
        "citation_status": Counter(result.citation.status for result in results),
        "public_opinion_level": Counter(result.public_opinion.risk_level for result in results),
        "knowledge_gap_status": Counter(result.knowledge_gap.status for result in results),
        "defensive_answer_status": Counter(result.defensive_answer.status for result in results),
        "timeliness_status": Counter(result.timeliness.status for result in results),
        "run_status": Counter(result.run_status for result in results),
    }
    summary = {
        "input_file": Path(args.input).name,
        "sheet": args.sheet,
        "requested_limit": args.limit,
        "input_rows_read": source_row_count,
        "rows_after_time_filter": len(frame),
        "requested_time_window": requested_time_window,
        "time_filter_mode": time_filter_mode,
        "time_column_used": time_column_used,
        "results_written": len(results),
        "error_count": len(errors),
        "privacy_columns_removed_count": len(privacy_columns_found),
        "redacted_identity_occurrences": redacted_identity_occurrences,
        "rule_version": RULE_VERSION,
        "prompt_version": prompt_config["prompt_version"],
        "llm_mode_requested": bool(args.use_llm),
        "llm_reviewed_count": sum(result.llm_reviewed for result in results),
        "answered_count": sum(bool(result.answer) for result in results),
        "citation_eligible_count": sum(result.answer_source == "联网搜索" and bool(result.answer) for result in results),
        "counts": {name: dict(counter) for name, counter in counters.items()},
        "scope_note": "联网引用一致性仅适用于回答来源=联网搜索且答案非空的记录。",
        "maintenance_scope_note": "防御性回答和时效性问答仅生成人工任务，不直接判定为 Badcase。",
        "review_grouping_note": "人工复核队列默认按相似问题聚类；每组保留代表问题、出现次数、问题变体和样例原表行。",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="旦导风险质检 Agent V1")
    parser.add_argument("--input", required=True, help="问答日志 Excel 或 CSV 路径")
    parser.add_argument("--sheet", default="问答记录", help="工作表名称")
    parser.add_argument("--limit", type=int, default=None, help="仅读取前 N 条数据")
    parser.add_argument("--output-dir", default="outputs/latest", help="输出目录")
    parser.add_argument("--use-llm", action="store_true", help="仅对规则候选启用可选模型复核")
    parser.add_argument("--start-time", default="", help="时间窗口起点，例如 2026-08-29 12:00")
    parser.add_argument("--end-time", default="", help="时间窗口终点，采用左闭右开")
    parser.add_argument("--time-column", default="", help="时间列名称；留空时尝试常见名称")
    parser.add_argument("--assume-prefiltered", action="store_true", help="文件没有时间列时，确认源文件已按目标时间窗口导出")
    return parser


if __name__ == "__main__":
    raise SystemExit(run(build_parser().parse_args()))
