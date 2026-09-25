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
    # v2.18.9：合并后**标签不再贴切时一并更新** ✓ 不给就沿用组内并集 ✓
    tags: list[str] | None = None
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
    # v2.18.58：只有 correct / merge 会用到 content（keep 直接跳过 ✓ retract 只软删 ✓）
    # ⇒ 不设成必填 ✗ —— 提示词没要求每条都给 content ✓ 强制必填只会把
    #   "keep 没带 content" 这种合乎提示词的输出判失败、白走一次重试 ✗（同 FactMergeGroup 的教训 ✓）
    content: Text = ""
    reason: Short
    relations: list[Relation] | None = Field(default=None, max_length=20)
    importance: int | None = Field(default=None, ge=1, le=10)
    # 主体记错了就填这里（用条目里的 id）✗ 修正归属 —— 只对 correct 有意义，留空=不动主体
    subject: Short | None = None
    # v2.18.9：改写内容后**标签不再贴切时一并更新** ✓ 不给就沿用组内并集 ✓
    # （此前内容能改、标签改不了 → 标签会一直停在旧的 ✗）
    tags: list[Text] | None = None
    # v2.18.9 回声防线：证据只有助手自己（evidence[].bot=1）时填 true ✓
    # 应用侧会**拒绝据此提升 importance** ✗（不许自我强化）
    only_self: bool = False

    @model_validator(mode="after")
    def validate_content(self):
        # 只有真的会改写正文的动作才要 content ✓
        if self.action in ("correct", "merge") and not self.content.strip():
            raise ValueError("correct/merge requires content")
        return self


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


FACT_MERGE_PROMPT = '输入 groups[]：同组同主体，类别可能相同也可能不同；facts 按时间从新到旧排列，facts[0] 最新；若附了 evidence，那是这些事实的来源原文，用它核对。\n每组输出一条，数量与顺序与输入完全一致。【必须给出动作，没有 keep】三种动作：\n- merge：同一件事 → 以 facts[0] 为基准合并，把其余事实独有的人名、数字、日期、否定、条件、状态补进去；冲突以时间较晚者为准；不同对象分别写明，不得丢弃独有信息。合并结果不能比最长的一条明显更长，删掉重复表述。content 必须自包含，不写“同上”、不引用 ID、不写“根据记录”之类元话，不得编造。**合并后若原标签不再贴切，一并给 tags** ✗（不给就沿用原来的并集 ✓）\n**但“以时间较晚者为准”只适用于同一来源强度**：若附了 evidence，其中的 bot=1 表示那条原文是**助手自己说的** ✗ —— 助手更晚的转述**不能覆盖用户更早的原话** ✓ 冲突时一律以用户为准；没有证据就按原说法保留，别凭“更晚”擅自改结论。\n- relabel：同一件事，但两边内容各自都成立、无需合并 → 只统一类别（给 category）。\n- drop：若干条是纯冗余或错误 → 保留 target_id，其余进 source_ids 删除（可恢复）。**若某条 fact 的信息已被 target 完全覆盖**（它没有任何 target 没有的内容）⇒ 直接 drop，**不要**去写合并正文（写出来与 target 等价 ⇒ 白花 token ✓）；只有需要补充信息时才用 merge ✓。\n**判 drop 前逐条自问三问**（只服务 drop，答错会误删 ✗）：① 它和 target 是**同一件事**吗（同一对象的同一方面，而不是「都提到了 X」）？② 它有没有 target **没有的实质信息**（人/数字/日期/否定/条件/状态/程度）？③ 并进 target 会让结论**变模糊**吗？⇒ 三条都过才可 drop（reason 写「与 target 重复」之类）；①否（本来就是两件事）或 ②有（删了会丢信息）⇒ **不许 drop** ✗，改 merge 分别写明 ✓。拿不准一律不许 drop ✗。**信息已被 target 完全覆盖**（没有 target 没有的内容）⇒ 直接 drop，**不要写合并正文**（写出来与 target 等价 ⇒ 白花 token ✗）；需要补充信息才用 merge ✓。三问只是内部判断，**不要写进 JSON** ✗（输出结构照旧，字段见白名单）。\n跨类别（组内 category 不一致）必须给 category；合并前会先把整组统一到该类别。\n硬性字数：每条 content ≤ {content_max} 字，reason ≤ {reason_max} 字；超出即判定失败。\n只输出 JSON：{"groups":[{"target_id":"…","source_ids":["…"],"action":"merge|relabel|drop","category":"…","content":"…","reason":"…"}]}\ntarget_id 取要保留的那条 id；source_ids 至少一条，逐字复制。'

RECORD_MERGE_PROMPT = (
    "records 按时间从新到旧排列，records[0] 是最新的那条。\n"
    "只输出一个 action：merge（不允许 keep）。\n"
    "以 records[0] 为基准：保留它的内容与结论，再把其余记录里独有的人名、群名、"
    "数字、QQ号、日期、状态补充进去。\n"
    "冲突之处以时间较晚的说法为准，不保留已被推翻的旧结论。\n"
    "涉及不同主体时必须在同一条 content 里分别写明，不得丢弃任何主体或任何独有信息。合并结果不能比最长的一条明显更长，删掉重复表述。\n"
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
    # v2.18.11：压缩的分批方式 —— rounds=按整轮（默认 ✓ 抽取更准）/ records=按条数
    compress_batch_mode: Literal["rounds", "records"] = "rounds"
    # rounds 模式：每次压缩几轮 ✓（records 模式仍用 threshold/batch_size ✓）
    compress_rounds: int = Field(default=12, ge=1, le=200)
    # v2.18 第5项：查询词很少时，沿库内线索（别名 + 已核实关系客体）扩词召回
    expand_query: bool = True
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
    audit_batch: int = Field(default=20, ge=1, le=200)
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
    # ── v2.18.74：JEV 决策层（**可选增强**；默认全关 ⇒ 行为与之前逐字节一致）──
    jev_enabled: bool = False
    jev_model: str = ""                   # 在 KiraAI 里选一个类 JEV 决策模型
    jev_base_url: str = ""                # 可留空（自动取所选提供商的 base_url）
    jev_api_key: str = ""                 # 可留空（自动取所选提供商的 key）；支持 $$ENV_NAME
    jev_model_name: str = ""              # 可留空（自动取所选模型的 model_id）
    jev_timeout_ms: int = Field(default=5000, ge=300, le=60000)
    jev_sample: float = Field(default=1.0, ge=0.05, le=1.0)
    jev_recall: bool = False              # 召回筛选（被动召回）
    jev_merge: bool = False               # 合并路由（merge / drop→回收站 / keep）
    jev_audit: bool = False               # 审计预筛（只把可疑对喂审计模型）
    jev_importance: bool = False          # 写入时重要度定级（影响上浮/下沉）
    jev_compress: bool = False            # 压缩前置筛选（只把值得长期记的消息送进压缩）
    # 可选重排模型（KiraAI 里注册的 rerank 模型；留空=不重排，行为与之前一致）
    rerank_model: str = ""
    rerank_enabled: bool = False
    rerank_timeout_ms: int = Field(default=1500, ge=300, le=10000)
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
    # 导入旧记忆时按**事实年龄**折算一次 importance（海马体原本有持续衰减，我们没有）。
    # 每过一个半衰期 importance 减半（最低 1）；设为 0 关闭折算，原样保留 ✓
    migration_decay_half_life_days: int = Field(default=365, ge=0, le=3650)
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
    # ★ 2026-09-19（用户定的规矩）：**完全重新提取**只能对**单条** ✓
    #   全局（工作台弹窗 / Bot 全局）一律禁止 ✗ ⇒ /jobs 会拦 ✓
    #   理由：全局强制会把"本来好好的"事实也重写一遍 ✗（质量风险 ✓ 不只是 token ✓）
    tidy_rebuild_bot_enabled: bool = True
    tidy_rebuild_bot_cooldown_minutes: int = Field(default=60, ge=0, le=10080)
    fact_merge_cross_threshold: float = Field(default=0.4, ge=0.0, le=1.0)
    fact_merge_evidence: bool = True
    # 轮换槽位：在"同样相关"的候选里，优先把还没被召回过的那几条补进来
    rotate_enabled: bool = True
    rotate_count: int = Field(default=3, ge=0, le=20)
    # 事实的**常驻下沉阈值** ✓（2026-09-18 引入；2026-09-23 用户拍板重新标定）
    #   分数 = 重要度×2 + 有效被用次数×3 + 新鲜度（+ 本轮命中加成）
    #     · 新鲜度：10 分**按天线性衰减**，60 天归零（原 30/90 档位 ⇒ 整批卡分、到期成批沉 ✗）
    #     · 有效被用次数：带 60 天半衰期（原只增不减 ⇒ 用过几次就永久免沉 ✗）
    #   低于阈值、且重要度 ≤7 的事实本轮**不进常驻** ✓ 改由轮换槽位接力 ✓
    #   被轮换带进来且被"用上"（rotate_used ↑）⇒ 分数回升 ⇒ 自动浮回常驻 ✓
    #   设 0 = 关闭下沉 ✓（重要度 ≥8 硬规则永不沉 ✓ 与阈值无关 ✓）
    #   ⚠️ 标定依据（实测对照，阈值 15 / 衰减 60 天）：
    #     重要度 1-2 ⇒ 立即让位；3 ⇒ 约 6 天；4 ⇒ 约 18 天；5 ⇒ 约 30 天；
    #     6 ⇒ 约 42 天；7 ⇒ 约 54 天；≥8 永不沉 ✓
    #     本轮被命中的事实 +4 分（≈ 多新鲜 24 天）⇒ **相关优先，但仍受重要度约束** ✓
    fact_sink_threshold: int = Field(default=15, ge=0, le=40)
    rotate_keep_rounds: int = Field(default=3, ge=1, le=20)
    rotate_min_hits: int = Field(default=2, ge=1, le=10)
    rotate_cooldown_rounds: int = Field(default=10, ge=0, le=100)
    # v2.18.56：默认改为**关** ✓ —— 档案槽独立开关交给用户按需打开 ✓
    # 存量配置（存着旧默认 True）由 config_migrate 的 v6 一次性改写 ✓
    rotate_archive_enabled: bool = False
    rotate_archive_chars: int = Field(200, ge=0, le=200000)  # 0 = 不限
    rotate_archive_count: int = Field(3, ge=1, le=20)
    # ── 冷归档（P7 · v7 定案）：默认关 ⇒ 不启用时行为与今天逐字节一致 ──
    cold_archive_enabled: bool = True
    cold_archive_days: int = Field(180, ge=1, le=3650)
    cold_archive_path: str = ""
    cold_archive_auto: bool = True
    inject_budget_ms: int = Field(default=0, ge=0, le=10000)
    permanent_dedupe_cross_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    permanent_dedupe: bool = True
    dedupe_force_merge: bool = True
    dedupe_threshold: float = Field(default=0.25, ge=0.1, le=0.95)
    fact_recall_min_score: int = Field(default=2, ge=0, le=20)
    search_active_only: bool = False
    # v2.18.9：只有表情/图片的消息默认不进召回 ✓（数据仍保留 ✓ 关掉即可召回）
    recall_skip_media: bool = True
    cold_after_days: int = Field(default=180, ge=0, le=3650)
    # v2.18.19（B）：压缩推进的三个新旋钮 ✓
    # ① 一批喂给压缩模型的**原文总量上限** ✗（`record_merge_max_chars` 管的是**输出** ✓ 不是输入 ✗）
    #    实测：输入原本**没有总量上限** ✗ → 几千条迁移数据会直接 token 爆炸 ✓
    compress_input_max_chars: int = Field(default=20000, ge=1000, le=200000)
    # ②③ **冷会话**触发：**两个条件同时满足**才把门槛降为 1 ✓（2026-09-17 用户要求 ✓）
    #    · 只看"陈旧" ✗ ⇒ 还在活跃聊天、只是有几条老记录的会话也会被降门槛 ✗
    #    · 只看"闲置" ✗ ⇒ 刚停下来、内容还很新的会话也会被降门槛 ✗
    #    · 同时满足 = **既久没动、又有积压** ✓ 才是真正该"赶进度"的会话 ✓
    #    （任一项设为 0 ⇒ 自动退回"或" ✓ 不锁死 ✓）
    #    专治存量/迁移数据：会话还在活跃 ✓ 永远不空闲 ✗ 靠这对条件才排得上 ✓
    compress_stale_after_days: int = Field(default=3, ge=0, le=365)
    compress_idle_after_hours: int = Field(default=6, ge=0, le=720)
    # ④ 每会话**冷却**：两次"降门槛抽干"之间至少隔这么多分钟 ✓（防连续抽干烧 token ✓）
    # 单个压缩任务**最多连压几批**（每批 ≤ batch_size 条 ✓）
    # ⚠️ 这是**花钱的闸门** ✗✓ —— 迁移 2000 条若一个任务全压完 ≈50 次模型调用瞬间烧掉 ✓
    # 默认 3 ⇒ 一条任务最多 3 批（≤120 条）✓ 节奏由冷却(30分)与 scheduler 控制 ✓
    # （2026-09-17 用户："不会出现因为有 2000 条迁移过来，用户马上钱就被用光了吧" ✓）
    compress_batches_per_job: int = Field(default=3, ge=1, le=64)
    compress_idle_cooldown_min: int = Field(default=30, ge=0, le=1440)
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
    tool_refine_mode: Literal["expand", "inline"] = Field(
        default="expand",
        description=(
            "主动召回（查档案/看画像/overview）的精修方式："
            "expand=先扩大候选范围（×3）再用 JEV 精修并截回原条数（默认，召回更全）；"
            "inline=只在原有条数内精修（旧行为）。"
            "两条路在 JEV 不可用/失败时都完全等于旧行为。"
        ),
    )

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
    # v2.18.19：浏览档案时是否显示**工具步** ✗（默认不显示 ✓）
    # bot 主被动召回**永远**看不到工具步 ✓（那边是硬过滤 ✓ 与这个开关无关 ✓）
    include_tools: bool = False
    # v2.18.64：浏览档案时是否显示**历史存档 / 冷归档**的卡片 ✓（默认**显示** ✓）
    # 关掉 ⇒ 只看「活跃记忆」（`active=1` 且未冷归档 ✓）
    include_history: bool = True

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
    # ★ 2026-09-19（用户实测：点删除报 422"请检查必填项"）：
    #   快捷操作（重要度 ±1 / 删除）只发 {kind, target, patch} ✗ 不给 revision
    #   而 storage.edit 写的是 `if revision is not None and (… != revision)` ✓
    #   ⇒ **后端本来就允许不给** ✓ 只有这个模型强制必填 ✗ ⇒ 改可选 ✓
    #   语义：给了就做"版本没变才允许改"的冲突检测 ✓ 没给就跳过 ✓
    revision: int | None = Field(default=None, ge=1)
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
    # v2.18.19：整理永久记忆时可**无视冷却**（force ✓）或**只整理指定的几条**（ids ✓）
    # 前端「立即重新整理」/ 后台弹窗「全部重新整理」/ 单条「重新提取事实」都用它 ✓
    force: bool = False
    ids: list[str] | None = None
    # ★ 2026-09-19（用户定的规矩）：**完全重新提取**（本次"不允许 keep"⇒ 一定会给出动作 ✓）
    #   ⚠️ **只能对单条** ✗（必须带 ids ✓）—— 全局强制会把"本来好好的"事实
    #      也重写一遍 ✗（质量风险 ✓ 不只是 token ✓）⇒ /jobs 直接拦 ✓
    rebuild: bool = False
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
