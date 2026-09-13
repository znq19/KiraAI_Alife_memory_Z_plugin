"""Shared strict contracts for model output, tools and configuration."""

from __future__ import annotations
import json
from typing import Annotated, Literal
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Text = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16000)
]
Short = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
]
Category = Literal[
    "event",
    "fact",
    "preference",
    "commitment",
    "relationship",
    "profile",
    "resource",
    "self",
]


def relation_issue(relation):
    """A speech act alone does not establish a relationship between two entities."""
    if relation["predicate"].strip().casefold() in {
        "认为",
        "觉得",
        "说",
        "提到",
        "表示",
        "评价",
        "是",
        "相关",
        "关系",
        "thinks",
        "says",
        "is",
        "mentions",
    }:
        return "谓词没有表达具体关系；请按原文补全，无法确定时删除这条连线"
    if relation["subject"] == relation["object"]:
        return "关系两端相同，需核对身份"
    return ""


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Relation(Strict):
    subject: Short
    predicate: Short
    object: Short

    @model_validator(mode="after")
    def meaningful(self):
        if issue := relation_issue(self.model_dump()):
            raise ValueError(issue)
        return self


# Deterministic aliases for a well-known model slip (Chinese or generic labels).
CATEGORY_ALIASES = {
    "事件": "event",
    "事实": "fact",
    "偏好": "preference",
    "喜好": "preference",
    "习惯": "preference",
    "约定": "commitment",
    "承诺": "commitment",
    "关系": "relationship",
    "画像": "profile",
    "档案": "profile",
    "资料": "profile",
    "资源": "resource",
    "自我": "self",
    "自身": "self",
    "general": "fact",
    "other": "fact",
    "misc": "fact",
    "note": "fact",
    "info": "fact",
    "personal": "profile",
    "identity": "profile",
}


class Fact(Strict):
    category: Category

    @field_validator("category", mode="before")
    @classmethod
    def canonical_category(cls, value):
        if isinstance(value, str):
            cleaned = value.strip().lower()
            if cleaned in CATEGORY_ALIASES:
                return CATEGORY_ALIASES[cleaned]
            if cleaned in {
                "event",
                "fact",
                "preference",
                "commitment",
                "relationship",
                "profile",
                "resource",
                "self",
            }:
                return cleaned
            return value.strip()
        return value

    subject: Short
    content: Text
    reason: str = Field(max_length=2000)
    scenario: str = Field(max_length=2000)
    tags: list[Short] = Field(max_length=12)
    relations: list[Relation] = Field(max_length=20)
    source_ids: list[Short] = Field(min_length=1, max_length=200)
    importance: int = Field(default=5, ge=1, le=10)


class FactMergeGroup(Strict):
    """一组相似事实的处置。**没有 keep**：判定必须给出动作。

    merge   = 合并（跨类别时须同时给 category，先把全组统一到该类别）
    relabel = 只统一类别（同一件事被记成了不同类别，但内容各自保留也没问题）
    drop    = 删掉 source_ids 里那些冗余的（软删，可还原），保留 target_id
    """

    target_id: Short
    source_ids: list[Short] = Field(min_length=1, max_length=50)
    action: Literal["merge", "relabel", "drop"] = "merge"
    category: str = Field(default="", max_length=40)
    content: str = Field(default="", max_length=16000)
    reason: Short

    @model_validator(mode="after")
    def validate_action(self):
        if self.action == "merge" and not self.content.strip():
            raise ValueError("merge requires content")
        # 注意：**不要**在这里要求 len(source_ids) >= 2。
        # 提示词里写的是「source_ids 至少一条」（两条事实合并时模型只会给出另一条），
        # 而 merge_facts 本来就会把 target_id 并进组 → 再要 ≥2 就是把合乎提示词的
        # 输出判成失败、白走一次降级拼接（实测踩到）。
        return self


class FactMerge(Strict):
    """One model call merges a batch of near-duplicate fact groups."""

    groups: list[FactMergeGroup] = Field(max_length=20)


class TidyItem(Strict):
    """永久记忆整理：一条记忆的处置结论。"""

    id: Short
    action: Literal["keep", "extract", "archive", "split"]
    category: str = Field(default="", max_length=40)
    importance: int | None = Field(default=None, ge=1, le=10)
    facts: list[Fact] = Field(default_factory=list, max_length=6)
    keep_content: str = Field(default="", max_length=16000)
    reason: Short


class PermanentTidy(Strict):
    items: list[TidyItem] = Field(max_length=50)


class Compression(Strict):
    summary: Text
    facts: list[Fact] = Field(max_length=100)


class TrashRestore(Strict):
    kind: Literal["fact", "record", "cold"]
    target: Short


class TrashPurge(Strict):
    kind: Literal["fact", "record"]
    target: Short


class AuditAction(Strict):
    # retract 用于清理被证据推翻或纯属冗余的事实：软删、留版本、可恢复。
    action: Literal["keep", "correct", "merge", "retract"]
    target_id: Short
    source_ids: list[Short] = Field(min_length=1, max_length=100)
    content: Text
    reason: Short
    relations: list[Relation] | None = Field(default=None, max_length=20)
    importance: int | None = Field(default=None, ge=1, le=10)


class Audit(Strict):
    actions: list[AuditAction] = Field(max_length=50)


class RecordMerge(Strict):
    """Verdict for a cluster of similar permanent memories."""

    action: Literal["keep", "merge"]
    content: str = Field(default="", max_length=16000)
    reason: Short
    source_ids: list[Short] = Field(min_length=2, max_length=10)

    @model_validator(mode="after")
    def valid(self):
        if self.action == "merge" and not self.content.strip():
            raise ValueError("merged content required")
        return self


FACT_MERGE_PROMPT = (
    "输入是若干组相似事实（groups[]）：同组同主体，类别可能相同也可能不同；"
    "组内 facts 按时间从新到旧排列，facts[0] 最新；若附了 evidence，那是这些事实的来源原文，用它核对。\n"
    "为每一组输出一条结果，数量与顺序与输入完全一致。【必须给出动作，没有 keep】三种动作：\n"
    "- merge：确认是同一件事 → 以 facts[0] 为基准合并，把其余事实独有的人名、数字、日期、否定、"
    "条件、状态补进去；冲突以时间较晚者为准；不同对象要分别写明，不得丢弃独有信息。"
    "content 必须自包含，不写“同上”、不引用 ID、不写“根据记录”之类元话，不得编造。\n"
    "- relabel：确实是同一件事，但两边内容各自都成立、无需合并 → 只统一类别（给 category）。\n"
    "- drop：其中若干条是纯冗余或错误记录 → 保留 target_id，其余进 source_ids 被删除（可恢复）。\n"
    "跨类别时（组内 category 不一致）必须给 category，写明统一后的类别；"
    "合并前会先把整组统一到该类别。\n"
    "硬性字数：每条 content ≤ {content_max} 字，reason ≤ {reason_max} 字；超出即判定失败。\n"
    '只输出 JSON：{"groups":[{"target_id":"…","source_ids":["…"],"action":"merge|relabel|drop",'
    '"category":"…","content":"…","reason":"…"}]}\n'
    "target_id 取要保留的那条 id；source_ids 至少一条，逐字复制。"
)

RECORD_MERGE_PROMPT = (
    "records 按时间从新到旧排列，records[0] 是最新的那条。\n"
    "只输出一个 action：merge（不允许 keep）。\n"
    "以 records[0] 为基准：保留它的内容与结论，再把其余记录里独有的人名、群名、"
    "数字、QQ号、日期、状态补充进去。\n"
    "冲突之处以时间较晚的说法为准，不保留已被推翻的旧结论。\n"
    "涉及不同主体时必须在同一条 content 里分别写明，不得丢弃任何主体或任何独有信息。\n"
    "content 必须自包含：不写“同上”，不引用其他记录 ID，不写元话，不复述重复内容。\n"
    "不得编造原文没有的信息。\n"
    "硬性字数：content ≤ {content_max} 字，reason ≤ {reason_max} 字；超出即判定失败。\n"
    '只输出 JSON：{"action":"merge","content":"…","reason":"…","source_ids":["…"]}\n'
    "source_ids 至少两条，逐字复制 records[].id；禁止编造 ID。"
)


def render_prompt(template: str, content_max: int, reason_max: int) -> str:
    """Fill the soft limits into a configurable prompt template."""
    return template.replace("{content_max}", str(content_max)).replace(
        "{reason_max}", str(reason_max)
    )


class Settings(Strict):
    enabled: bool = True
    capture_enabled: bool = True
    bootstrap_seed: Literal["auto", "always", "off"] = "auto"
    inject_recent_raw: bool = False
    auto_inject: bool = True
    fact_view: Literal["grouped", "flat"] = "grouped"
    threshold: int = Field(default=50, ge=4, le=10000)
    batch_size: int = Field(default=40, ge=2, le=9999)
    probability: float = Field(default=0.8, ge=0, le=1)
    max_level: int = Field(default=8, ge=1, le=32)
    compress_model: str = ""
    audit_model: str = ""
    embedding_model: str = ""
    semantic_enabled: bool = False
    audit_enabled: bool = True
    audit_interval: int = Field(default=7200, ge=30, le=604800)
    audit_batch: int = Field(default=20, ge=1, le=50)
    audit_recheck_days: int = Field(default=7, ge=0, le=3650)
    audit_daily_calls: int = Field(default=24, ge=0, le=1000)
    model_timeout: int = Field(default=120, ge=5, le=600)
    model_retries: int = Field(default=2, ge=0, le=4)
    compress_persona: bool = True
    audit_persona: bool = False
    inject_mode: Literal["situational", "full"] = "situational"
    worker_count: int = Field(default=2, ge=1, le=4)
    context_chars: int = Field(default=24000, ge=2000, le=500000)
    token_warning: int = Field(default=120000, ge=1000, le=2000000)
    recall_keywords: list[Short] = ["记得", "之前", "上次", "曾经"]
    recall_scope: Literal["session", "linked", "global"] = "global"
    top_k: int = Field(default=5, ge=1, le=30)
    proactive_enabled: bool = False
    proactive_interval: int = Field(default=3600, ge=60, le=604800)
    proactive_jitter: int = Field(default=0, ge=0, le=86400)
    proactive_min_sessions: int = Field(default=1, ge=1, le=100)
    proactive_max_sessions: int = Field(default=0, ge=0, le=100)
    proactive_rotate: bool = False
    proactive_sessions: list[Short] = Field(default_factory=list, max_length=100)
    compress_instruction: str = Field(
        default="以自身视角保留事件、感情、人物、关键事实和生活轨迹。精简但不要按珍贵程度丢弃线索；只依据输入，保留时间、否定、条件和不确定性。",
        max_length=4000,
    )
    auto_migrate: bool = True
    mutual_exclusion: bool = True
    migration_max_chars: int = Field(default=120, ge=1, le=16000)
    compress_input_chars: int = Field(default=48000, ge=4000, le=500000)
    boot_enabled: bool = True
    boot_replay_seconds: int = Field(default=90, ge=0, le=86400)
    session_affinity: bool = False
    memorize_cover_check: bool = True
    permanent_tidy_enabled: bool = True
    # 写入永久记忆后直接整理一次（即使没触发去重）
    permanent_tidy_on_write: bool = True
    permanent_cap: int = Field(default=10, ge=1, le=200)
    permanent_budget_chars: int = Field(default=3000, ge=200, le=100000)
    permanent_tidy_batch: int = Field(default=10, ge=1, le=100)
    permanent_tidy_days: int = Field(default=14, ge=0, le=3650)
    fact_merge_cross_threshold: float = Field(default=0.4, ge=0.0, le=1.0)
    fact_merge_evidence: bool = True
    # 轮换槽位：在"同样相关"的候选里，优先把还没被召回过的那几条补进来
    rotate_enabled: bool = True
    rotate_count: int = Field(default=3, ge=0, le=20)
    rotate_keep_rounds: int = Field(default=3, ge=1, le=20)
    rotate_min_hits: int = Field(default=2, ge=1, le=10)
    rotate_cooldown_rounds: int = Field(default=10, ge=0, le=100)
    inject_budget_ms: int = Field(default=0, ge=0, le=10000)
    permanent_dedupe_cross_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    permanent_dedupe: bool = True
    dedupe_force_merge: bool = True
    dedupe_threshold: float = Field(default=0.25, ge=0.1, le=0.95)
    fact_recall_min_score: int = Field(default=2, ge=0, le=20)
    search_active_only: bool = False
    cold_after_days: int = Field(default=180, ge=0, le=3650)
    fact_merge_enabled: bool = True
    fact_merge_threshold: float = Field(default=0.25, ge=0.1, le=0.95)
    fact_merge_soft_chars: int = Field(default=80, ge=10, le=2000)
    fact_merge_max_chars: int = Field(default=150, ge=10, le=4000)
    fact_merge_soft_reason_chars: int = Field(default=15, ge=2, le=200)
    fact_merge_reason_chars: int = Field(default=40, ge=2, le=500)
    fact_merge_batch_clusters: int = Field(default=5, ge=1, le=50)
    fact_merge_prompt: str = Field(default=FACT_MERGE_PROMPT, max_length=8000)
    cross_session_merge: bool = True
    merge_pending_hide: bool = True
    record_merge_soft_chars: int = Field(default=500, ge=50, le=16000)
    record_merge_max_chars: int = Field(default=16000, ge=100, le=16000)
    record_merge_soft_reason_chars: int = Field(default=15, ge=2, le=200)
    record_merge_reason_chars: int = Field(default=60, ge=2, le=500)
    record_merge_prompt: str = Field(default=RECORD_MERGE_PROMPT, max_length=8000)
    profile_summary_count: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def valid_merge_limits(self):
        for soft, hard, name in (
            (self.fact_merge_soft_chars, self.fact_merge_max_chars, "fact content"),
            (
                self.fact_merge_soft_reason_chars,
                self.fact_merge_reason_chars,
                "fact reason",
            ),
            (self.record_merge_soft_chars, self.record_merge_max_chars, "record content"),
            (
                self.record_merge_soft_reason_chars,
                self.record_merge_reason_chars,
                "record reason",
            ),
        ):
            if soft > hard:
                raise ValueError("%s soft limit exceeds hard limit" % name)
        return self

    @model_validator(mode="after")
    def valid_batch(self):
        if self.batch_size >= self.threshold:
            raise ValueError("batch_size must be smaller than threshold")
        for sid in self.proactive_sessions:
            parts = sid.split(":", 2)
            if len(parts) != 3 or parts[1] not in ("dm", "gm") or not all(parts):
                raise ValueError("invalid proactive session")
        return self


def parse_output(text: str, contract: type[Strict]):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError("non-finite JSON number")

    if len(text) > 250000:
        raise ValueError("model output too large")
    return contract.model_validate(
        json.loads(text, object_pairs_hook=unique, parse_constant=invalid)
    )


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class Search(Strict):
    sid: str = ""
    # The admin UI browses one session at a time; global memories are opt-in there.
    include_global: bool = True
    keyword: str = Field(default="", max_length=500)
    prompt: str = Field(default="", max_length=2000)
    level: int | None = Field(default=None, ge=0, le=100)
    start: float | None = None
    end: float | None = None
    offset: int = Field(default=0, ge=0, le=1000000)
    limit: int = Field(default=30, ge=1, le=100)
    subject: str = ""


class Edit(Strict):
    kind: Literal["record", "fact"]
    target: Short
    revision: int = Field(ge=1)
    patch: dict
    reason: Short

    @model_validator(mode="after")
    def validate_patch(self):
        if self.kind == "record":
            if not set(self.patch) <= {
                "summary",
                "active",
                "deleted",
                "category",
                "importance",
            }:
                raise ValueError("invalid fields")
            if "summary" in self.patch and (
                not isinstance(self.patch["summary"], str)
                or not self.patch["summary"].strip()
                or len(self.patch["summary"]) > 16000
            ):
                raise ValueError("invalid summary")
        else:
            allowed = set(Fact.model_fields) - {"source_ids"} | {"deleted"}
            if not set(self.patch) <= allowed:
                raise ValueError("invalid fields")
        for k in ("active", "deleted"):
            if k in self.patch and type(self.patch[k]) is not bool:
                raise ValueError("boolean required")
        if not self.patch:
            raise ValueError("empty patch")
        return self


class Restore(Strict):
    kind: Literal["fact", "record"]
    target: Short
    version_id: int = Field(ge=1)
    revision: int = Field(ge=1)


class NewMemory(Strict):
    sid: Short
    content: Text
    category: str = Field(default="", max_length=40)
    users: list[Short] = Field(default_factory=list, max_length=100)
    start: float | None = None
    end: float | None = None
    importance: int | None = Field(default=None, ge=1, le=10)


class Job(Strict):
    # tidy 也允许手动排队：Bot 用 CorrectMemory(action=tidy) 触发，
    # 工作台的这个按钮走同一条链路。
    kind: Literal["compress", "audit", "reindex", "dedupe", "tidy", "rewrite"]
    sid: Short


class ConfigEdit(Strict):
    revision: Short
    settings: Settings


class NameEdit(Strict):
    entity_id: Short
    name: Short
    revision: int = Field(ge=1)
    reason: Short

    @model_validator(mode="after")
    def valid_name(self):
        if any(ord(c) < 32 for c in self.name):
            raise ValueError("invalid name")
        return self


class EntityRefresh(Strict):
    entity_id: Short


class NameBatch(Strict):
    ids: list[Short] = Field(default_factory=list, max_length=200)
    reason: Short = "批量确认当前QQ昵称"
    # missing: 只查没有名字的；all: 已有名字的也查一遍（写入时仍一律跳过）
    mode: Literal["missing", "all"] = "missing"
