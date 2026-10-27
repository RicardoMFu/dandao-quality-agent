from __future__ import annotations

from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Labels(StrictModel):
    level: list[str] = Field(default_factory=lambda: ["未明确"])
    identity: list[str] = Field(default_factory=lambda: ["未明确"])
    campus: list[str] = Field(default_factory=lambda: ["未明确"])
    scene: list[str] | None = None

    @model_validator(mode="after")
    def check_exclusive(self):
        enums = {
            "level": {"本科生", "硕士生", "博士生", "未明确"},
            "identity": {"医学生", "留学生", "港澳台生", "通用", "未明确"},
            "campus": {"邯郸校区", "江湾校区", "枫林校区", "张江校区", "全校通用", "未明确"},
            "scene": {"思想引领", "学业指导", "生涯规划", "心理支持", "生活服务", "第二课堂", "其他"},
        }
        for field, allowed in enums.items():
            values = getattr(self, field)
            if field == "scene" and values is None:
                continue
            if not values or len(set(values)) != len(values) or not set(values) <= allowed:
                raise ValueError(f"{field}含空值、重复或未知标签")
            if len(values) > 1 and set(values) & {"通用", "全校通用", "未明确", "其他"}:
                raise ValueError(f"{field}的通用、未明确、其他标签须独选")
        return self


class Fact(StrictModel):
    key: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=1000)
    quote: str = Field(min_length=1, max_length=4000)


class Source(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    channel: Literal["官网", "公众号", "知识库", "其他"] = "知识库"
    url: str = Field(default="", max_length=2000)
    text: str = Field(min_length=1, max_length=40000)
    # 人工核验表示正文与来源已对照，不表示现在仍有效。
    verified: bool = False
    published_at: date | None = None
    checked_at: date | None = None
    valid_from: date | None = None
    valid_until: date | None = None
    validity_scope: Literal["单次事项", "长期规则", "综合资料", "未知"] = "未知"
    supersedes: list[str] = Field(default_factory=list)
    replacement_quote: str = Field(default="", max_length=4000)
    labels: Labels = Field(default_factory=Labels)
    facts: list[Fact] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def verify_quotes(self):
        if self.valid_from and self.valid_until and self.valid_from > self.valid_until:
            raise ValueError("生效时间不能晚于失效时间")
        if any(f.quote not in self.text or f.value not in f.quote for f in self.facts):
            raise ValueError("事实值和引文必须存在于来源正文")
        if self.supersedes and (not self.replacement_quote or self.replacement_quote not in self.text):
            raise ValueError("替代关系必须附正文中的明确证据")
        if self.url and not self.url.startswith(("https://", "http://")):
            raise ValueError("来源链接仅支持http/https")
        return self


class AnswerClaim(StrictModel):
    key: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=1000)
    quote: str = Field(min_length=1, max_length=4000)


class Case(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    title: str = Field(default="新问答", max_length=300)
    question: str = Field(min_length=1, max_length=12000)
    answer: str = Field(max_length=20000)
    answered_at: date | None = None
    network_mode: Literal["联网", "知识库", "未知"] = "未知"
    time_sensitive: bool = False
    audience: Labels = Field(default_factory=Labels)
    sources: list[Source] = Field(default_factory=list, max_length=30)
    expected_points: list[str] = Field(default_factory=list, max_length=30)
    claims: list[AnswerClaim] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def check_links(self):
        ids = [s.id for s in self.sources]
        if len(set(ids)) != len(ids):
            raise ValueError("来源ID必须唯一")
        for s in self.sources:
            if any(x not in ids or x == s.id for x in s.supersedes):
                raise ValueError("替代关系只能指向本案例的其他来源")
        for claim in self.claims:
            if claim.quote not in self.answer or claim.value not in claim.quote:
                raise ValueError("回答事实标注须对应原回答片段；编辑回答后请同步标注")
        return self


DIMENSIONS = {"accuracy", "relevance", "completeness", "clarity", "uncertainty", "citations", "privacy", "safety", "reputation"}
RISK_CODES = {"privacy_leak", "dangerous_steps", "unsupported_accusation", "fabrication", "latest_without_source", "boundary_violation"}


class Score(StrictModel):
    level: Literal[0, 1, 3, 5] | None
    reason: str = Field(min_length=1, max_length=2000)
    source_ids: list[str] = Field(default_factory=list)
    answer_quote: str = Field(default="", max_length=4000)
    source_quote: str = Field(default="", max_length=4000)


class Finding(StrictModel):
    code: str = Field(min_length=1, max_length=100)
    status: Literal["candidate", "confirmed", "unknown"]
    reason: str = Field(min_length=1, max_length=2000)
    answer_quote: str = Field(default="", max_length=4000)
    source_ids: list[str] = Field(default_factory=list)


class Judgment(StrictModel):
    scores: dict[str, Score]
    findings: list[Finding] = Field(default_factory=list, max_length=60)
    missing_points: list[str] = Field(default_factory=list, max_length=30)
    summary: str = Field(min_length=1, max_length=3000)

    @model_validator(mode="after")
    def exact_dimensions(self):
        if set(self.scores) != DIMENSIONS:
            raise ValueError("必须返回且只返回九项评分")
        return self


class Review(StrictModel):
    run_id: str
    reviewer: str = Field(min_length=1, max_length=80)
    judgment: Judgment
    resolution: Literal["已解决", "部分解决", "未解决", "待核实"]
    reason: str = Field(min_length=1, max_length=2000)
    confirmed_risks: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def known_risks(self):
        if not set(self.confirmed_risks) <= RISK_CODES:
            raise ValueError("未知风险编码")
        return self
