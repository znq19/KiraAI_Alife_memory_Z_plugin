"""JEV（TypeSafe System-One）决策层 —— **可选增强**，缺省全程不启用。

设计硬约束（与《方案 v4》一致，逐条可测）：
1. **永不抛异常**：任何失败（未配置/超时/网络/额度/返回异常）一律返回 None
   ⇒ 调用方永远走原有逻辑，功能不受影响。
2. **绝不进热路径**：只允许在 prewarm（用户打字时）或后台任务里调用，且带硬超时。
3. **决策留痕**：每次判定写一行 JSONL，供后续标定与回溯。
4. **连接解析顺序**：显式 base_url+api_key > 用户选中的 KiraAI 模型（取其 provider_config）。

原生接口（OpenAI 格式**不支持**，实测 400，故必须走这条）：
    POST {base_url}/v1/systemone
    {"model": "...", "state": "...", "questions": {...}}
    -> {"model": "...", "answers": {...}, "usage": {"input_tokens": N, ...}}

★ 计费实测：state **只算一次**（不随问题数放大）；约 86 token/问。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

try:  # 框架内运行时用宿主 logger；独立运行/测试环境自动降级（不硬依赖）
    from core.logging_manager import get_logger
except Exception:  # pragma: no cover
    import logging as _logging

    def get_logger(*_a, **_k):
        return _logging.getLogger("alife_memory_jev")

def _make_logger():
    """框架 logger 需要可写的日志目录；拿不到时降级到标准库（测试/独立运行）。"""
    try:
        return get_logger("alife_memory_jev", "light_purple")
    except Exception:  # pragma: no cover
        import logging

        return logging.getLogger("alife_memory_jev")


logger = _make_logger()

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"

# ── 阈值：全部来自 2026-09-24 实测（真 key / 中英各约 150 条标注样本），
#    上线后仍应在机标定；此处是**保守起点**，宁可弃权不可误判 ──
SAME_HIGH = 0.50      # ≥ 视为「同一件事」（实测：换词重复 0.54、明确不同 ≤0.08
                      #   ⇒ 0.5 能救回换词重复，且与「不同事」仍有两倍以上余量）
SAME_NOT_SAME_MAX = 0.30   # ≤ 视为"明确不是同一件事"（旧名 SAME_LOW 与下方 0.12 重名 ⇒ 被覆盖 ✗ 2026-09-25 改名）
NEW_HIGH = 0.60       # ≥ 视为"带来实质新信息"
DILUTE_HIGH = 0.75     # 实测：该合并档稀释 0.31~0.69 / 真会稀释 0.77~0.84    # ≥ 视为"并入正文会稀释重点"
                      #   实测：该合并的档位稀释 0.30~0.58，真会稀释的 0.77~0.84
                      #   ⇒ 阈值取 0.70 才不误杀（0.60 会卡在贴边的 0.58 上 ✗）
IMPORTANCE_MIN_CONF = 0.35  # 定级置信门槛（实测该任务置信偏低 0.10~1.00，过高会几乎不生效）
TRIGGER_HIGH = 0.50   # 召回触发阈值（实测阈值 0.5 命中 8/8）
RELEVANT_HIGH = 0.50  # 候选相关性阈值（实测相关 0.75~0.98 / 干扰 0.02~0.24）

# 重要度：★ **只压不抬**（用户约定）——「核心/重要/一般」一律保留大模型原值，
# 只有低价值档才由 JEV 下调；映射值进现有「重要度×2」公式（阈值 15，≥8 永不沉）。
#   无价值=1 ⇒ 2+10=12 < 15 ⇒ 立即沉     ｜ 次要=3 ⇒ 16 刚存，约半月后自然沉
IMPORTANCE_LEVELS = {"次要": 3, "无价值": 1}
IMPORTANCE_OPTIONS = {
    "核心": "影响健康安全或长期关系，必须永远记住",
    "重要": "稳定的个人情况或长期约定",
    "一般": "背景信息，有用但不关键",
    "次要": "一次性事务、很快过期的安排",
    "无价值": "寒暄、口头语、没有信息量的内容",
}

# ────────────────────────── 客户端 ──────────────────────────


class JevClient:
    """最小原生客户端：同步 urllib 跑在 to_thread 里（零新增依赖，可硬超时）。

    ★ 熔断：连续失败 3 次 → 冷却 300 秒内直接返回 None（不再占用时间）。
    """

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 4.0):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        # ★ 必须走 endpoint_of 规整：OpenAI 类型提供商的 base_url 常带 /v1，
        #   直接拼会变成 …/v1/v1/systemone ✗（实测：解析对了但真机调用失败）
        self.endpoint = endpoint_of(base_url or DEFAULT_BASE_URL)
        self.api_key = api_key or ""
        self.model = model or DEFAULT_MODEL
        self.timeout = float(timeout)
        self.fails = 0
        self.opened_at = 0.0
        self.tokens = 0
        self.last_trigger: Optional[float] = None          # 累计输入 token（面板可观测）

    # ---------- 状态 ----------
    @property
    def ready(self) -> bool:
        if not self.api_key:
            return False
        if self.fails >= 3 and (time.time() - self.opened_at) < 300:
            return False
        return True

    def _ok(self) -> None:
        self.fails = 0

    def _bad(self) -> None:
        self.fails += 1
        if self.fails == 3:
            self.opened_at = time.time()
            logger.warning("JEV 连续失败 3 次，冷却 300 秒（期间自动走原逻辑）")

    # ---------- 同步实现 ----------
    def _post_sync(self, payload: dict, timeout: float) -> Optional[dict]:
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw)

    # ---------- 对外 ----------
    async def call(self, state: str, questions: dict, timeout: Optional[float] = None) -> Optional[dict]:
        """返回 {"answers": {...}, "usage": {...}}；**任何失败都返回 None**。"""
        if not self.ready or not questions:
            return None
        payload = {"model": self.model, "state": state, "questions": questions}
        limit = float(timeout or self.timeout)
        try:
            data = await asyncio.wait_for(
                asyncio.to_thread(self._post_sync, payload, limit), timeout=limit + 0.5
            )
        except Exception as exc:  # noqa: BLE001  —— 契约：任何失败都不得外泄
            self._bad()
            logger.info(f"JEV 调用未成功，本轮走原逻辑：{type(exc).__name__}")
            return None
        if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
            self._bad()
            return None
        self._ok()
        usage = data.get("usage") or {}
        try:
            self.tokens += int(usage.get("input_tokens") or 0)
        except (TypeError, ValueError):
            pass
        return data

# ────────────────────────── 配置与解析 ──────────────────────────


def _env(value: str) -> str:
    """支持 `$$ENV_NAME` 形式（与框架的敏感配置一致）。"""
    v = (value or "").strip()
    if v.startswith("$$"):
        import os

        return os.environ.get(v[2:], "") or ""
    return v


def split_uuid(uuid: str) -> tuple[str, str]:
    """`provider_id:model_id` → 两段（model_id 允许含冒号）。"""
    pid, _, mid = (uuid or "").partition(":")
    return pid.strip(), mid.strip()


# 常见"末端路径"——用户可能直接粘贴完整端点，规整时要先剥掉
_ENDPOINT_SUFFIXES = ("/v1/systemone", "/systemone", "/chat/completions",
                      "/completions", "/embeddings", "/models", "/v1/messages")


def endpoint_of(base_url: str) -> str:
    """把提供商 base_url 规整成 systemone 端点。

    要容忍各种粘法（实测用户会直接粘贴完整地址）：
      https://api.typesafe.ai            → .../v1/systemone
      https://api.typesafe.ai/v1         → .../v1/systemone
      https://x/v1/chat/completions      → .../v1/systemone
      https://x/v1/systemone             → .../v1/systemone（不重复拼 ✗）
    """
    b = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    for suffix in _ENDPOINT_SUFFIXES:
        if b.endswith(suffix):
            b = b[: -len(suffix)].rstrip("/")
            break
    if b.endswith("/v1"):
        b = b[:-3].rstrip("/")
    return b + "/v1/systemone"


# 不同提供商/第三方插件对这两个字段的叫法可能不同 ⇒ 多键名兜底
_BASE_KEYS = ("base_url", "api_base", "openai_api_base", "base", "endpoint",
              "url", "host", "server", "api_host")
_KEY_KEYS = ("api_key", "apikey", "api_token", "token", "key",
             "access_key", "secret", "auth_token")


def _pick(cfg: dict, names) -> str:
    """从 provider_config 里挑第一个非空的候选键；支持嵌套一层（如 {"openai": {...}}）。"""
    for name in names:
        value = cfg.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in cfg.values():
        if isinstance(value, dict):
            found = _pick(value, names)
            if found:
                return found
    return ""


class JevConfig:
    """不可变快照：一次事件只解析一次，避免热路径反复读配置。"""

    __slots__ = ("enabled", "base_url", "api_key", "model", "timeout", "sample", "uuid")

    def __init__(self, enabled=False, base_url="", api_key="", model="",
                 timeout=4.0, sample=1.0, uuid=""):
        self.enabled = bool(enabled)
        self.base_url = base_url or DEFAULT_BASE_URL
        self.api_key = api_key or ""
        self.model = model or DEFAULT_MODEL
        self.timeout = max(0.5, float(timeout or 4.0))
        self.sample = min(1.0, max(0.0, float(sample if sample is not None else 1.0)))
        self.uuid = uuid or ""

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.api_key)

    def client(self) -> Optional[JevClient]:
        return JevClient(self.base_url, self.api_key, self.model, self.timeout) if self.ready else None


def resolve_config(settings: Any, provider_mgr: Any = None) -> JevConfig:
    """解析 JEV 连接信息：显式字段优先，其次取所选 KiraAI 模型的 provider_config。"""
    def get(name, default=""):
        """取设置值。★ 不能写 `getattr(...) or default`：False 会被 or 吃掉，
        导致布尔型设置里的 False 永远读成默认值（实测踩过 ✗）。"""
        value = getattr(settings, name, default)
        return default if value is None else value

    uuid = str(get("jev_model")).strip()
    base = str(get("jev_base_url")).strip()
    key = _env(str(get("jev_api_key")))
    model = str(get("jev_model_name")).strip()
    if uuid and provider_mgr is not None:
        try:
            pid, mid = split_uuid(uuid)
            info = provider_mgr.get_model_info(pid, mid)
            cfg = (getattr(info, "provider_config", None) or {}) if info else {}
            if not isinstance(cfg, dict):
                cfg = {}
            base = base or _pick(cfg, _BASE_KEYS)
            key = key or _env(_pick(cfg, _KEY_KEYS))
            model = model or mid
            # 与"加速器类插件"共存的保险：若端点被改写成本地代理/加速地址，
            # 提示用户改用显式「JEV 接口地址」直填原始地址（显式值优先级最高 ✓）
            if base and re.search(r"(127\.0\.0\.1|localhost|:\d{2,5}/|/accel|/proxy)", base, re.I):
                logger.warning(
                    "[记忆·Z] JEV 端点看起来是本地加速/代理地址（%s）——"
                    "若连接失败，请在插件设置里用「JEV 接口地址」直接填 JEV 原始地址",
                    base,
                )
            if not base or not key:
                # 诊断一行：让用户知道“选了模型但连不上”缺的是哪一样 ✓
                logger.warning(
                    "[记忆·Z] JEV 连接信息不完整（base=%s key=%s，提供商 %s）"
                    "—— 请在「设置 → 提供商」检查该模型的接口地址与密钥",
                    "有" if base else "缺", "有" if key else "缺", pid,
                )
        except Exception:  # noqa: BLE001 —— 解析失败不得影响插件
            pass
    try:
        timeout = float(get("jev_timeout_ms", 4000)) / 1000.0
    except (TypeError, ValueError):
        timeout = 4.0
    try:
        sample = float(get("jev_sample", 1.0))
    except (TypeError, ValueError):
        sample = 1.0
    return JevConfig(
        enabled=bool(get("jev_enabled", False)),
        base_url=base, api_key=key, model=model,
        timeout=timeout, sample=sample, uuid=uuid,
    )

# ────────────────── 问题构造（★两个实测踩过的坑，必须遵守）──────────────────
#   坑1：候选/事实必须**自包含**放进 instructions —— 非生成模型没有指代消解，
#        问"上面那条"必错（实测同一任务从 AUC 1.00 退化成一坨）。
#   坑2：choice 的 criteria 必须**扁平** {选项名: 描述}，嵌套会直接报错。


def q_noul(text: str, true_hint: str, false_hint: str) -> dict:
    return {"type": "noul", "instructions": text, "criteria": {"true": true_hint, "false": false_hint}}


def q_choice(text: str, options: dict) -> dict:
    return {"type": "choice", "instructions": text, "criteria": dict(options)}


def parse_noul(answers: dict, key: str) -> Optional[float]:
    item = (answers or {}).get(key) or {}
    try:
        return float(item.get("noul"))
    except (TypeError, ValueError):
        return None


def parse_choice(answers: dict, key: str) -> tuple[Optional[str], float]:
    item = (answers or {}).get(key) or {}
    try:
        conf = float(item.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    return (item.get("choice"), conf)


# ── 1) 召回筛选（被动）：参照物=当前上下文，逐候选问"是否直接相关" ──
def build_recall_filter(context: str, hits: list[tuple[str, str]],
                        want_trigger: bool = False) -> dict:
    """上下文放 state（只计一次费），每条候选自带正文。

    ★ 判据按**整批**语义（实测 2026-09-24）：
      · 「对这批消息中任意一条是否有帮助」相比「对回答当前请求是否有用」
        在群聊多话题批次里显著更好（出差 0.25→0.62、改会 0.26→0.70）
      · 但维度越多分数会被压低（同一条事实：私聊 0.62 / 群聊 0.29）
        ⇒ 阈值必须很低（0.15），且要保底条数，不能按高阈值删减

    判据必须**具体到"不知道它会不会答错"**；写成"是否有用"这类笼统措辞，
    实测打分会被压成一团（0.10~0.18），区分度消失。
    """
    qs: dict = {}
    for key, text in hits:
        qs["hit_" + key] = q_noul(
            f"[候选记忆] {text} [/候选记忆]\n"
            "这条记忆对理解或回应这批消息中的任意一条有帮助吗？",
            "有帮助：直接关系到这批消息里某一条的人物/偏好/约定/禁忌/事实",
            "没帮助：与这批消息都无关",
        )
    if want_trigger:
        # 与候选**同一个请求**里附带问一句 ⇒ 零额外调用、零额外延迟 ✓
        qs["__trigger__"] = q_noul(
            "这批消息里，用户是在要求回忆过去说过或发生过的事吗"
            "（哪怕没有用记得/上次/之前这类词）？",
            "是：在问我过去说过的信息、旧事、约定、以前提过的人或事",
            "不是：只是在聊当下、问新东西或让我做事",
        )
    return qs


# ── 2) 合并路由：三信号 → 代码组合（绝不问"该怎么办"）──
def build_compress_screen(key: str, text: str, role: str = "user") -> dict:
    """压缩前筛消息。**用户侧与助手侧判据不同**（实测 2026-09-25）：

    为什么要分角色：原本压缩**对助手消息也提取** ✓，而信息常常落在助手回复里
      （例：用户"你把我那些事记一下" 0.27 ✗，助手"我记下了：①花生过敏②每周日提醒…" **0.98** ✓）
    若只判用户侧，这类整轮会被连坐归档 ⇒ **信息一起丢** ✗
    """
    if role == "assistant":
        return {
            "m_" + key: q_noul(
                f"[助手回复] {text} [/助手回复]\n"
                "这条助手回复里有没有值得长期记住的用户信息，或需要对用户长期遵守的约定/承诺？",
                "有：复述/确认了用户的持久事实，或做出了要长期遵守的约定、承诺、提醒安排",
                "没有：寒暄、过程说明、工具结果、一次性答复",
            ),
        }
    return {
        "m_" + key: q_noul(
            f"[消息] {text} [/消息]\n这条消息里有没有值得长期记住的用户信息？",
            "有：用户的持久事实/偏好/身份/关系/约定/长期目标，或明确表达的情绪与态度",
            "没有：寒暄、客套、一次性事务、纯提问、工具调用或没有信息量的内容",
        ),
    }


def build_merge_prescreen(primary: str, key: str, text: str) -> dict:
    """合并**预筛**：只问「是不是同一件事」（1 问/候选 ⇒ 最省 ✓）。

    用途：在**调用大模型之前**先看一眼。若整批候选都明确「不是同一件事」，
    就可以跳过大模型合并调用（省一次大调用 ✓）。
    """
    pre = f"[主事实] {primary} [/主事实]\n[候选] {text} [/候选]\n"
    return {
        "same_" + key: q_noul(
            pre + "候选和主事实讲的是同一件事吗？",
            "是：同一话题/同一对象的事 —— 说法不同、详略不同、角度不同、"
            "重复询问或补充说明，都算同一件事",
            "不是：互不相干的两件事（只是恰好都提到了同一个词）",
        ),
    }


def build_merge_route_single(primary: str, key: str, text: str) -> dict:
    """单条候选的三问（自包含）。★ 与 build_merge_route 的区别：只问一条，避免批内干扰。"""
    pre = f"[主事实] {primary} [/主事实]\n[候选] {text} [/候选]\n"
    return {
        "same_" + key: q_noul(
            pre + "候选和主事实讲的是同一件事吗？",
            "是：同一话题/同一对象的事 —— 说法不同、详略不同、角度不同、"
            "重复询问或补充说明，都算同一件事",
            "不是：互不相干的两件事（只是恰好都提到了同一个词）",
        ),
        "new_" + key: q_noul(
            pre + "候选里有主事实没有的实质信息吗？",
            "有：主事实没提到的严重程度、后果、应对方式、时间或更具体的信息",
            "没有：和主事实是同一层信息，只是换了说法或更短的表达",
        ),
        "dilute_" + key: q_noul(
            pre + "如果把候选并进主事实的正文，会让主事实的重点变模糊吗？",
            "会：引入与重点无关或更弱的细节，把重点冲淡",
            "不会：并进去反而更完整、更准确",
        ),
    }

def build_merge_route(primary: str, cands: list[tuple[str, str]]) -> dict:
    qs: dict = {}
    for key, text in cands:
        pre = f"[主事实] {primary} [/主事实]\n[候选] {text} [/候选]\n"
        # ★ same 的判据必须写"可以带更多细节"，否则加了细节的同一件事会被判成不同事实
        qs["same_" + key] = q_noul(
            pre + "候选讲的是同一个事实吗（可以带更多细节）？",
            "同一个事实", "另一件不同的事",
        )
        qs["new_" + key] = q_noul(
            pre + "候选里有主事实没有的实质信息吗？",
            "有：主事实没提到的严重程度、后果、应对方式、时间或更具体的信息",
            "没有：和主事实是同一层信息，只是换了说法或更短的表达",
        )
        qs["dilute_" + key] = q_noul(
            pre + "如果把候选并进主事实的正文，会让主事实的重点变模糊吗？",
            "会稀释重点", "不会稀释",
        )
    return qs


def route_merge(same: Optional[float], new: Optional[float], dilute: Optional[float]) -> str:
    """返回 "merge" / "drop" / "keep"；信号缺失一律 keep（不猜）。

    实测校准（2026-09-24，6/6 符合预期）：
      纯重复          same 0.91 new 0.26          -> drop
      同事实+重伤信息  same 0.89 new 0.97 稀释0.46 -> merge
      同事实+无新信息  same 0.93 new 0.52          -> drop
      同事实+应用细节  same 0.80 new 0.88 稀释0.31 -> merge
      不同事实        same 0.05                    -> keep
    """
    if same is None or new is None:
        return "keep"
    if same < SAME_HIGH:
        return "keep"                      # 不是同一件事 ⇒ 本来就不该合，也不会膨胀
    if new < NEW_HIGH:
        return "drop"                      # 同一件事但只是更弱的重复 ⇒ 软删进回收站（可还原）
    if dilute < DILUTE_HIGH:
        return "merge"                     # 有新信息且不稀释 ⇒ 该合的照合
    return "keep"                          # 会稀释主事实 ⇒ 不动（防稀释优先）


# 「不是同一件事」那一侧的判法（实测标定，2026-09-25）：
#   · 明确无关：same ≤ 0.12 ⇒ 直接 keep（省掉大模型 ✓）
#   · 模糊地带（0.12 < same < 0.50）⇒ **交回大模型**（= 原行为 ✓）
#     为什么不用"同话题"判据：实测同主题不同角度 0.39 / 互不相干 0.36 ⇒ 只差 0.03 ✗
#     用它会把这个区间的**无关事实也合进来**（正是要避开的稀释 ✗）
SAME_LOW = 0.12
# 压缩前置筛选（用户 2026-09-25 定稿，两档；实测 AUC 1.00）：
#   单条 ≥ COMPRESS_KEEP_MIN(0.50) ⇒ 进入压缩输入
#   其余                          ⇒ **直归档**（active=0 ⇒ 等同已压缩；原文仍可按 ID 检索 ✓）
#   一条都没到 0.50               ⇒ 全部直归档 + 不调大模型（省一次调用 ✓）
COMPRESS_KEEP_MIN = 0.50


def route_merge_soft(same, new, dilute):
    """带第四态的路由：merge / drop / keep / **inherit**（判不准 ⇒ 交回大模型）。"""
    if same is None:
        return "keep"
    if same >= SAME_HIGH:
        return route_merge(same, new, dilute)
    return "keep" if same <= SAME_LOW else "inherit"


# ── 3) 重要度（写入时定级，进"重要度×2"的现有公式）──
def build_importance(facts: list[tuple[str, str]]) -> dict:
    qs: dict = {}
    for key, text in facts:
        qs["imp_" + key] = q_choice(
            f"[事实] {text} [/事实]\n这条事实对长期记忆的价值等级？", IMPORTANCE_OPTIONS
        )
    return qs


def importance_of(level: Optional[str]) -> Optional[int]:
    """返回应**下调到**的分值；**None = 不动**（核心/重要/一般 保留大模型原值）。"""
    return IMPORTANCE_LEVELS.get(level or "")


# ── 4) 召回触发（主动）：替代字面关键词（实测阈值 0.5 命中 8/8）──
def build_recall_trigger(message: str) -> dict:
    return {"trig": q_noul(
        f"[用户消息] {message} [/用户消息]\n用户在要求回忆过去的事情吗？",
        "在让我回忆以前说过/发生过的事", "不是回忆请求（闲聊、提问、办事等）",
    )}


# ── 5) 审计预筛：逐对判"矛盾/重复/过时"，可疑的才喂审计模型 ──
def build_audit_pairs(pairs: list[tuple[str, str, str]]) -> dict:
    qs: dict = {}
    for key, left, right in pairs:
        pre = f"[事实甲] {left} [/事实甲]\n[事实乙] {right} [/事实乙]\n"
        qs["bad_" + key] = q_noul(
            pre + "这两条事实互相矛盾、重复，或有一条已过时吗？",
            "存在矛盾/重复/过时，需要修正", "两条都正常，互不冲突",
        )
    return qs

# ────────────────────────── 决策留痕（JSONL） ──────────────────────────


class DecisionLog:
    """把"JEV 会怎么判"记成 JSONL，供离线对比；**任何异常都吞掉**。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = path
        self.errors = 0

    def write(self, kind: str, data: dict) -> None:
        if self.path is None or self.errors >= 3:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps({"t": round(time.time(), 3), "kind": kind, **data},
                              ensure_ascii=False)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:  # noqa: BLE001 —— 记录失败绝不能影响主流程
            self.errors += 1


# ────────────────────────── 统一入口 ──────────────────────────


class Decisions:
    """所有 JEV 决策的唯一入口。

    契约（逐条可测）：
      · 未启用 / 未配置 key / 熔断中 / 调用失败 ⇒ 每个方法都返回 None
      · 返回 None ⇒ 调用方**必须**走原有逻辑（功能与今天完全一致）
          """

    def __init__(self, settings: Any = None, provider_mgr: Any = None,
                 log_path: Optional[Path] = None):
        self.cfg = resolve_config(settings, provider_mgr) if settings is not None else JevConfig()
        self._client = self.cfg.client()
        self.log = DecisionLog(log_path)
        self.calls = 0
        self.skipped = 0

    # ---------- 状态 ----------
    @property
    def ready(self) -> bool:
        return bool(self._client and self._client.ready)

    @property
    def tokens(self) -> int:
        return int(getattr(self._client, "tokens", 0) or 0)

    def _maybe(self, sample: float) -> bool:
        """按采样率决定是否真的发起调用（控制额度消耗）。"""
        if not self.ready:
            self.skipped += 1
            return False
        if sample >= 1.0:
            return True
        import random

        if random.random() >= sample:
            self.skipped += 1
            return False
        return True

    def _hit(self) -> bool:
        """采样闸门：jev_sample < 1 时按比例调用（省额度），未命中即当次弃权。"""
        if self.cfg.sample >= 1.0:
            return True
        import random

        if random.random() <= self.cfg.sample:
            return True
        self.skipped += 1
        return False

    async def _ask(self, state: str, questions: dict, kind: str,
                   timeout: Optional[float] = None) -> Optional[dict]:
        if not self._maybe(self.cfg.sample):
            return None
        data = await self._client.call(state, questions, timeout=timeout)
        self.calls += 1
        if data is None:
            return None
        return data

    # ---------- 1) 召回筛选 ----------
    async def recall_filter(self, context: str, hits: list[tuple[str, str]],
                            timeout: Optional[float] = None,
                            want_trigger: bool = False) -> Optional[list[tuple[str, float]]]:
        """返回 [(key, 相关度)] 按分降序；失败返回 None（调用方用原排序）。

        want_trigger=True 时，在**同一次请求**里附带问一句"这批消息是否在要求回忆往事"，
        结果放入 self.last_trigger（零额外调用、零额外延迟）。
        """
        if not hits:
            return None
        data = await self._ask(context or "lang: zh",
                               build_recall_filter(context, hits, want_trigger),
                               "recall", timeout)
        if not data:
            return None
        answers = data.get("answers") or {}
        self.last_trigger = (parse_noul(answers, "__trigger__")
                             if want_trigger else None)
        scored = []
        for key, _text in hits:
            score = parse_noul(answers, "hit_" + key)
            if score is not None:
                scored.append((key, score))
        if len(scored) != len(hits):
            return None                       # 有任何一条没解析出来 ⇒ 整体弃权
        scored.sort(key=lambda kv: -kv[1])
        return scored

    # ---------- 2) 合并路由 ----------
    async def merge_prescreen(self, items, timeout=None):
        """对 [(key, 主事实, 候选)] 一次性问「是不是同一件事」⇒ {key: 分数}。

        失败 / 未启用 ⇒ None（调用方据此**照常调大模型** ✓ 绝不误跳）
        """
        if not items or not self.ready:
            return None
        if not self._hit():
            return None
        questions = {}
        for key, primary, cand in items:
            questions.update(build_merge_prescreen(primary, key, cand))
        data = await self._ask("lang: zh", questions, "merge_prescreen", timeout)
        if not data:
            return None
        answers = data.get("answers") or {}
        out = {}
        for key, _p, _c in items:
            score = parse_noul(answers, "same_" + key)
            if score is None:
                return None          # 有解析不出来的 ⇒ 不冒险，交给大模型 ✓
            out[key] = score
        self.calls += 1
        return out or None

    async def compress_screen(self, items, timeout=None):
        """压缩前置筛选：对 [(key, 角色, 文本)] 一次性问「值不值得长期记」⇒ {key: 分数}。

        失败 / 未启用 ⇒ None（调用方**原样放行** ✓ 绝不误跳）
        """
        if not items or not self.ready:
            return None
        if not self._hit():
            return None
        questions = {}
        for key, role, text in items:
            questions.update(build_compress_screen(key, text, role))
        data = await self._ask("lang: zh", questions, "compress_screen", timeout)
        if not data:
            return None
        answers = data.get("answers") or {}
        out = {}
        for key, _role, _t in items:
            score = parse_noul(answers, "m_" + key)
            if score is None:
                return None                   # 解析不全 ⇒ 整批放行 ✓
            out[key] = score
        self.calls += 1
        return out or None

    async def merge_plan(self, items, timeout=None):
        """**先审后生成**：对 [(key, 主事实, 候选)] 一次性问 same/new/dilute ⇒ {key: 动作}。

        动作 ∈ merge / drop / keep / inherit（由 route_merge_soft 合成 ✓）
        失败 / 解析不全 / 未启用 ⇒ None（调用方**原样放行**给大模型 ✓ 绝不误判）
        """
        if not items or not self.ready:
            return None
        if not self._hit():
            return None
        questions = {}
        for key, primary, cand in items:
            questions.update(build_merge_route_single(primary, key, cand))
        data = await self._ask("lang: zh", questions, "merge_plan", timeout)
        if not data:
            return None
        answers = data.get("answers") or {}
        out = {}
        for key, _p, _c in items:
            same = parse_noul(answers, "same_" + key)
            if same is None:
                return None                   # 解析不全 ⇒ 不冒险，整批交回大模型 ✓
            out[key] = route_merge_soft(same, parse_noul(answers, "new_" + key),
                                        parse_noul(answers, "dilute_" + key) or 0.0)
        self.calls += 1
        return out or None

    async def merge_route(self, primary: str, cands, timeout=None, hints=None):
        """逐条候选判定 merge/drop/keep。

        ★ 必须**一条候选一次调用**（实测：把两条近似候选放进同一次调用会互相干扰，
          本该 merge 的被判成 drop ✗）。并发发出 ⇒ 墙钟时间与批内提问相当。
        """
        fn = self._client
        if fn is None or not self.ready or not cands:
            return None
        if not self._hit():
            return None

        async def one(key: str, text: str):
            hint = (hints or {}).get(key)
            qs = build_merge_route_single(primary, key, text)
            if hint is not None:
                # ★ 预筛已经问过 same ⇒ 这里不再重复问（省约 1/3 的 JEV 用量 ✓）
                qs.pop("same_" + key, None)
            data = await self._ask("lang: zh", qs, "merge", timeout)
            if not data:
                return key, None
            a = data.get("answers") or {}
            same = hint if hint is not None else parse_noul(a, "same_" + key)
            new = parse_noul(a, "new_" + key)
            dil = parse_noul(a, "dilute_" + key)
            if same is None or new is None:
                return key, None
            return key, route_merge_soft(same, new, dil if dil is not None else 0.0)

        # ★ 顺序调用（不并发）：实测并发请求会互相干扰/被上游限流，
        #   同一候选单独问 3 次结果稳定（.88/.98/.58），并发时会被判成 drop ✗
        out = {}
        for k, s in cands[:5]:    # 顺序调用：上限 5 条 ⇒ 最坏 ~15 秒（后台任务，用户无感知）
            key, route = await one(k, s)
            if route:
                out[key] = route
        self.calls += 1
        return out or None

    async def importance(self, facts: list[tuple[str, str]],
                         timeout: Optional[float] = None) -> Optional[dict[str, int]]:
        if not facts:
            return None
        data = await self._ask("lang: zh", build_importance(facts), "importance", timeout)
        if not data:
            return None
        answers = data.get("answers") or {}
        out: dict[str, int] = {}
        for key, _text in facts:
            level, conf = parse_choice(answers, "imp_" + key)
            if not level or conf < IMPORTANCE_MIN_CONF:
                continue                      # 置信不足 ⇒ 不写，保留原值
            mapped = importance_of(level)
            if mapped is None:
                continue                      # ★ 只压不抬：核心/重要/一般 一律不写回
            out[key] = mapped
        return out or None

    # ---------- 4) 召回触发 ----------
    async def recall_trigger(self, message: str, timeout: Optional[float] = None) -> Optional[bool]:
        if not message.strip():
            return None
        data = await self._ask("lang: zh", build_recall_trigger(message), "trigger", timeout)
        if not data:
            return None
        score = parse_noul(data.get("answers") or {}, "trig")
        return None if score is None else (score >= TRIGGER_HIGH)

    # ---------- 5) 审计预筛 ----------
    async def audit_prescreen(self, pairs: list[tuple[str, str, str]],
                              timeout: Optional[float] = None) -> Optional[list[str]]:
        """返回可疑对的 key 列表（空列表=整批干净，可跳过审计模型）。"""
        if not pairs:
            return None
        data = await self._ask("lang: zh", build_audit_pairs(pairs), "audit", timeout)
        if not data:
            return None
        answers = data.get("answers") or {}
        hot = []
        for key, _l, _r in pairs:
            score = parse_noul(answers, "bad_" + key)
            if score is not None and score >= 0.5:
                hot.append(key)
        return hot
