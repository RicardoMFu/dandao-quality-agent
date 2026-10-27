from datetime import date
from .schema import Case, Judgment, RISK_CODES


def source_status(source, at):
    if not source.verified:
        return "unverified"
    if at is None:
        return "unknown_time"
    if source.valid_from and at < source.valid_from:
        return "not_yet_valid"
    # 长期规则的局部日期不等于整篇失效。
    if source.valid_until and at > source.valid_until and source.validity_scope == "单次事项":
        return "expired"
    return "usable"


def applicable(source, audience):
    for field, universal in [("level", None), ("identity", "通用"), ("campus", "全校通用")]:
        scope, target = set(getattr(source.labels, field)), set(getattr(audience, field))
        if "未明确" in scope or "未明确" in target:
            continue
        if universal and universal in scope:
            continue
        if not scope & target:
            return False
    return True


def inspect_evidence(case: Case):
    status = {s.id: source_status(s, case.answered_at) for s in case.sources}
    usable = [s for s in case.sources if status[s.id] == "usable" and applicable(s, case.audience)]
    # 不能用回答之后才发布的更正通知评价当时是否应知道；未知发布日期留待核实。
    usable = [s for s in usable if not s.published_at or s.published_at <= case.answered_at]
    replaced = {old for s in usable for old in s.supersedes}
    active = [s for s in usable if s.id not in replaced]
    for old in replaced:
        status[old] = "superseded"
    for s in case.sources:
        if status[s.id] == "usable" and not applicable(s, case.audience):
            status[s.id] = "out_of_scope"
        elif status[s.id] == "usable" and s.published_at and s.published_at > case.answered_at:
            status[s.id] = "future_publication"
    facts = {}
    for s in active:
        for f in s.facts:
            facts.setdefault(f.key, []).append({"value": f.value, "source_id": s.id, "quote": f.quote})
    conflicts = [{"key": key, "values": values} for key, values in facts.items() if len({v["value"] for v in values}) > 1]
    claims = []
    for c in case.claims:
        values = facts.get(c.key, [])
        distinct = {v["value"] for v in values}
        verdict = "conflict" if len(distinct) > 1 else "supported" if c.value in distinct else "contradicted" if distinct else "unknown"
        claims.append({**c.model_dump(), "verdict": verdict, "evidence": values})
    holds = []
    if not active:
        holds.append("缺少适用且已核验的参考正文")
    if case.answered_at is None:
        holds.append("回答时间未知，无法确定应采用哪个版本")
    if conflicts:
        holds.append("来源存在未消除的事实冲突")
    if any(x["verdict"] in {"unknown", "conflict"} for x in claims):
        holds.append("回答中的关键事实尚无一致依据")
    if case.time_sensitive and any(s.validity_scope == "未知" for s in active):
        holds.append("时效问题的来源有效范围尚未明确")
    # 涉及资格/限定场景，用户或来源条件未知不能凭不相交规则判断适用。
    if any(s.labels.level == ["未明确"] or s.labels.identity == ["未明确"] or s.labels.campus == ["未明确"] for s in active):
        holds.append("参考资料的适用范围尚未明确")
    for s in active:
        for field, universal in [("level", None), ("identity", "通用"), ("campus", "全校通用")]:
            scope = getattr(s.labels, field)
            if getattr(case.audience, field) == ["未明确"] and not (universal and universal in scope):
                holds.append("用户适用条件未知，需核对限定范围")
    holds = list(dict.fromkeys(holds))
    return {"evaluation_date": str(case.answered_at) if case.answered_at else None, "source_status": status, "active_source_ids": [s.id for s in active], "conflicts": conflicts, "claims": claims, "holds": holds}


def validate_judgment(judgment: Judgment, case: Case, evidence):
    sources = {s.id: s for s in case.sources}
    for score in judgment.scores.values():
        if score.answer_quote and score.answer_quote not in case.answer:
            raise ValueError("模型引用的回答片段不存在")
        if any(i not in sources for i in score.source_ids):
            raise ValueError("模型返回未知来源ID")
        if score.source_quote and not any(score.source_quote in sources[i].text for i in score.source_ids):
            raise ValueError("模型引用的来源片段不存在")
    for key in ("accuracy", "citations"):
        s = judgment.scores[key]
        if s.level is not None and (not s.source_ids or not s.source_quote):
            raise ValueError("事实和引用评分必须有正文依据；缺证据请返回null")
        if s.level is not None and not set(s.source_ids) <= set(evidence["active_source_ids"]):
            raise ValueError("事实和引用评分引用了失效、未核验或不适用来源")
    for f in judgment.findings:
        if f.status == "confirmed" and f.code in RISK_CODES and not f.answer_quote:
            raise ValueError("确认风险须提供原回答中的问题句")
        if f.answer_quote and f.answer_quote not in case.answer:
            raise ValueError("风险原句不存在")
        if any(i not in sources for i in f.source_ids):
            raise ValueError("风险引用未知来源")
    return judgment
