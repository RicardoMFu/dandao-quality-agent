from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from .schema import Case, Judgment, Score, DIMENSIONS, RISK_CODES
from .rubric import RULE_VERSION, PROMPT_VERSION, calculate
from .evidence import inspect_evidence
from .upstream import screen
from .judge import judge


def redact(text):
    """默认仅对明确结构的号码脱敏，不声称能识别所有姓名与私人数据。"""
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号已脱敏]", text)
    text = re.sub(r"(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])", "[证件号已脱敏]", text)
    text = re.sub(r"(?i)\b(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9_.-]{16,})", "[密钥已脱敏]", text)
    return text


def safe_case(case):
    # 用字段递归处理而非替换JSON串，保留JSON转义安全性。
    def walk(x):
        if isinstance(x, str):
            return redact(x)
        if isinstance(x, list):
            return [walk(v) for v in x]
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        return x
    return Case.model_validate(walk(case.model_dump(mode="json")))


def run_case(case: Case, mode="rules"):
    # 先记录隐私候选，再脱敏；原始号码不进入SQLite或模型请求。
    pii_candidate = redact(case.answer) != case.answer
    case = safe_case(case)
    evidence = inspect_evidence(case)
    signals = screen(case)
    holds = list(evidence["holds"])
    risks = []
    if pii_candidate:
        signals.append({"code": "privacy_candidate", "status": "candidate", "reason": "回答含疑似号码或凭证，已脱敏，需确认是否为真实私人信息"})
        holds.append("疑似个人信息或凭证需人工确认")
    if any(s["code"] in {"sensitive_candidate", "reputation_candidate"} for s in signals):
        holds.append("风险规则命中，需核对回答实际行为")
    # 越权承诺是候选；不能以简单命中自动判违法或泄密。
    if re.search(r"(?:保证|已经|已为你|替你|直接帮你).{0,12}(?:批准|审批通过|提交成功|录取|退课成功|缴费成功)", case.answer):
        signals.append({"code": "boundary_candidate", "status": "candidate", "reason": "出现审批或执行承诺，请核对是否有真实权限及成功回执"})
        holds.append("操作或审批承诺缺少权限与执行凭证")
    if any(c["verdict"] == "contradicted" for c in evidence["claims"]):
        holds.append("已标注回答事实与适用来源不一致")
    if mode == "llm":
        judgment, meta = judge(case, evidence)
        for finding in judgment.findings:
            if finding.status != "unknown":
                holds.append(finding.reason)
            if finding.code in RISK_CODES and finding.status == "confirmed":
                risks.append(finding.code)
        # 人工仍需确认；显示的是AI初评分，机器记录不会覆盖人工记录。
    else:
        judgment = Judgment(scores={i: Score(level=None, reason="需结合证据进行人工或AI评审") for i in DIMENSIONS}, summary="规则初筛完成；九项评分由AI评审或人工填写。")
        meta = {"model": None, "elapsed_ms": 0, "usage": {}, "prompt_version": PROMPT_VERSION}
    decision = calculate(judgment, risks, list(dict.fromkeys(holds)))
    serialized = case.model_dump(mode="json")
    return {
        "run_id": str(uuid.uuid4()), "created_at": datetime.now(timezone.utc).isoformat(),
        "case": serialized, "input_hash": hashlib.sha256(json.dumps(serialized, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "mode": mode, "rule_version": RULE_VERSION, "meta": meta,
        "signals": signals, "evidence": evidence, "judgment": judgment.model_dump(mode="json"),
        "decision": decision, "review_status": "pending",
    }
