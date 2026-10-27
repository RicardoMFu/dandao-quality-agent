"""保持原包不变；输出仅作候选信号，不继承其自动通过/知识缺口结论。"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dandao_upstream", ROOT / "upstream-v1.2/v1_agent.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
CONFIG = ROOT / "upstream-v1.2/config"


def screen(case):
    m = module
    source = {"联网": "联网搜索", "知识库": "知识库", "未知": "AI自己回答"}[case.network_mode]
    sensitive, _ = m.detect_sensitive(case.question, source, m.read_json_yaml(CONFIG / "sensitive_keywords.yaml"), case.answer)
    opinion, _ = m.detect_public_opinion(case.question, case.answer, "", m.read_json_yaml(CONFIG / "public_opinion.yaml"))
    maintenance = m.read_json_yaml(CONFIG / "content_maintenance.yaml")
    defensive, _ = m.detect_defensive_answer(case.answer, source, sensitive, maintenance)
    timely, _ = m.detect_timeliness(case.question, case.answer, maintenance)
    gap, _ = m.detect_knowledge_gap({}, case.question, case.answer, source, sensitive, m.read_json_yaml(CONFIG / "knowledge_gap.yaml"))
    signals = []
    if sensitive.keyword == "hit":
        signals.append({"code": "sensitive_candidate", "status": "candidate", "reason": sensitive.reason, "detail": sensitive.model_dump()})
    if opinion.review_required:
        signals.append({"code": "reputation_candidate", "status": "candidate", "reason": opinion.reason})
    if defensive.review_required:
        signals.append({"code": "defensive_candidate", "status": "candidate", "reason": defensive.reason})
    if gap.status == "suspected_gap":
        signals.append({"code": "knowledge_candidate", "status": "candidate", "reason": "回答存在无资料表达，需核查语料与检索日志；不能据此确认缺语料"})
    if timely.review_required:
        signals.append({"code": "timeliness_candidate", "status": "candidate", "reason": timely.reason})
    return signals


def group_cases(cases):
    rows = [{"问题": c.question, "excel_row": i + 1, "case_id": c.id} for i, c in enumerate(cases)]
    groups = module.group_similar_candidate_rows(rows)
    return [{"question": g["代表问题"], "count": g["出现次数"], "case_ids": [r["case_id"] for r in g["成员"]]} for g in groups]
