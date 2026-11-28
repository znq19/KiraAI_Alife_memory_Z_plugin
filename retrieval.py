"""Local relevance and safe factual projections; no embedding provider required."""

import logging
import re
import json
import time
from collections import OrderedDict

# Our own read tools return a distinctive JSON envelope. Those results are
# self-recall echoes: storing them as memories only bloats the next context.
_MEMORY_PAYLOAD_MARKERS = (
    '"archives_in_context"',
    '"children_total"',
    '"kids"',        # v2.18.19：读原文改短键 ✗ 新旧都留以便识别历史载荷 ✓
    '"next_page"',
    '"subjects"',
    '"entities"',
    '"omitted_ids"',
    '"related_archives"',
)
TOOL_RESULT_PREFIX = "工具感知结果："


def is_tool_result(text):
    """这条内容是不是**模型抓回来的工具结果** ✓（2026-09-18 用户实测 ✓）

    它们会被存成记录、再压成档案/提炼成事实 ⇒ 之前会**流进轮换槽** ✗
    ⇒ 轮换槽是"相关但还没召回过的**记忆**" ✓ 工具结果不是记忆 ✗ ⇒ 排除 ✓
    （**主召回不动** ✓ —— 工具结果是对话史的一部分 ✓ 该能被想起来 ✓）
    """
    return str(text or "").lstrip().startswith(TOOL_RESULT_PREFIX)


def looks_like_memory_payload(text):
    head = (text or "")[:4000]
    if head.startswith(TOOL_RESULT_PREFIX):
        head = head[len(TOOL_RESULT_PREFIX) :].lstrip()
    # Tolerate different serializers (with or without spaces after ':').
    return head[:200].replace(" ", "").startswith('{"ok":true') and any(
        marker in head for marker in _MEMORY_PAYLOAD_MARKERS
    )


def tool_preview(text, limit=240):
    """Short, single-line preview kept as the injected summary."""
    flat = " ".join((text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def tool_call_summary(tool_calls, limit=60):
    """Readable replacement for the raw tool_calls JSON blob."""
    parts = []
    for call in tool_calls or []:
        function = call.get("function", {}) if isinstance(call, dict) else {}
        name = function.get("name") or call.get("name") or "工具"
        arguments = function.get("arguments") or ""
        parts.append(f"{name}({arguments[:limit]})" if arguments else name)
    return "[调用工具：" + "、".join(parts) + "]" if parts else ""


# Names written by third-party plugins for their own synthetic messages; they
# must never become a user's remembered nickname.
SYNTHETIC_NAMES = frozenset(
    {
        "提醒任务所有者",
        "Kira",
        "system",
        "system:reminder_plugin",
        "Web UI 用户",
        "Web UI 管理员",
        "自主意图循环",
        "未知",
    }
)

_NOISE = re.compile(r"[\s，。、；：！？,.!?;:'\"“”‘’()（）\[\]【】<>《》\-—~～/\\]+")


def normalize_text(text):
    """Case- and punctuation-insensitive form used for duplicate detection."""
    return _NOISE.sub("", (text or "").casefold())


def _bigrams(text):
    flat = normalize_text(text)
    if len(flat) < 2:
        return {flat} if flat else set()
    return {flat[i : i + 2] for i in range(len(flat) - 1)}


def similarity(left, right, min_overlap=4):
    """Bigram containment: "does one memory largely cover the other".

    Jaccard over long texts is too diluted for near-duplicate detection, so we
    score the overlap against the smaller side and require a real overlap.
    Short facts need a lower overlap floor; callers can pass ``min_overlap``.
    """
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return 0.0
    overlap = len(a & b)
    if overlap < min_overlap:
        return 0.0
    return overlap / min(len(a), len(b))


def identity_info(entity_id):
    """Display labels; synthetic ids are pending/uncategorised, never a fake entity."""
    from . import identity

    if entity_id in (identity.GLOBAL, identity.GLOBAL_ID):
        return {
            "label": "全局记忆",
            "identity_note": "跨会话共享的全局记忆，不是群聊。",
            "lookup_id": "",
        }
    if entity_id in (identity.SELF, identity.SELF_ID):
        return {
            "label": "机器人自身",
            "identity_note": "机器人自己的认知与经历。",
            "lookup_id": "",
        }
    if entity_id in (identity.UNSCOPED, identity.UNSCOPED_ID):
        return {
            "label": "未分类 · 来源会话未确定",
            "identity_note": "旧数据没有可靠会话标识；保留待核对，不猜群名或归属。",
            "lookup_id": "",
        }
    if entity_id.startswith(identity.PENDING):
        shape = identity.pending_shape(entity_id)
        kind = "群" if shape and shape[0] == "group" else "人物"
        number = shape[2] if shape else ""
        return {
            "label": f"待绑定 · {kind} {number}",
            "identity_note": "已按号码登记；出现同号码账号或在线适配器后会自动合并。",
            "lookup_id": "",
        }
    if entity_id.startswith(identity.LEGACY):
        shape = identity.legacy_shape(entity_id)
        if not shape:
            return {
                "label": "未分类",
                "identity_note": "身份尚未确认。",
                "lookup_id": "",
            }
        kind, adapter, number = shape
        if kind == "user":
            lookup = f"{adapter}:{number}" if adapter else ""
        elif kind == "group":
            lookup = f"{adapter}:gm:{number}" if adapter else ""
        else:
            lookup = ""
        return {
            "label": f"待绑定 · 号码 {number}" if number else "未分类",
            "identity_note": "同号码账号出现后自动合并，不会单独保留为旧档案。",
            "lookup_id": lookup,
        }
    return {
        "label": "名称待补全",
        "identity_note": "使用稳定账号区分身份，昵称相同不会合并。",
        "lookup_id": entity_id,
    }


def archive_view(row, child_offset=0, child_count=20, include_content=False):
    # v2.18.19：**短键 + 绝对时间 + 只带必要标记** ✓（用户要求与其他通道一致 ✓）
    # · `t` 用**可读时间** ✗ 不再发 epoch 浮点（`1783254654.123` 谁都读不出 ✓）
    # · `mem` **只在是永久记忆时**才写 ✓（不是就不写 ✗ 省掉 `"permanent":0` ✓）
    result = {
        "s": row.get("summary") or "",                        # 内容 ✓
        **tfield("t", short_time(row.get("end") or row.get("start"))),  # 绝对时间（跨年才带年份 ✓）
        "sp": row.get("speaker") or "",                       # 说话人 ✓
        "lv": row.get("level", 0),                            # 0=原文 / 1+=摘要 ✓
    }
    if row.get("permanent"):
        # 永久记忆 = 必须每轮在场的那种（bot 主动写的约束/身份 ✓ 不是提取出来的事实 ✓）
        result["mem"] = 1
    children = row.get("children", [])
    if children:
        result["kids"] = len(children)
        if child_offset + child_count < len(children):
            result["next"] = child_offset + child_count
    if include_content:
        result["versions"] = row.get("versions", [])
        result["legacy_sources"] = row.get("legacy_sources", [])
    result["ci"] = not children or include_content
    if result["ci"]:
        # Decode only the plugin's own complete archive/message envelope, not arbitrary prose.
        content = row["content"]
        try:
            parsed = json.loads(content)
            if isinstance(parsed, list) and all(
                isinstance(r, dict) and {"id", "role", "content"} <= r.keys()
                for r in parsed
            ):
                content = parsed
            elif (
                isinstance(parsed, dict)
                and parsed.get("role") in {"user", "assistant", "tool"}
                and "content" in parsed
            ):
                content = parsed
        except (ValueError, TypeError):
            pass
        result["content"] = content
    return result


class RecallWindow:
    """Delivered-result history: what this conversation has already been shown.

    It accumulates for the lifetime of the conversation (30 minutes), so the
    model never receives the same memory twice unless it explicitly asks for it.
    """

    def __init__(self):
        self.entries = OrderedDict()

    def get(self, key):
        now = time.monotonic()
        for k in list(self.entries):
            if now - self.entries[k]["updated"] > 1800:
                del self.entries[k]
        return self.entries.get(key, {"query": "", "ids": [], "facts": []})

    def forget_sid(self, sid):
        """★ 2026-09-19：**压缩之后**解除该会话的"已给过"压制 ✓

        为什么需要：seen 窗口只记得"我给过你" ✗ 却不记得
          "**你上下文里现在还留着吗**" ✓
        一旦发生压缩，那批结果很可能已经**被压掉/出窗** ✗
        而 seen 仍在压制 ⇒ 模型最长 30 分钟拿不回来（只能靠 allow_seen 自救 ✓）

        ⚠️ 安全：只**解除压制**（允许重发）✓ —— 不动任何数据 ✓
           最坏结果只是多花几个 token ✓ 绝无数据风险 ✓
        """
        if not sid:
            return 0
        n = 0
        for k in list(self.entries):
            if isinstance(k, tuple) and k and k[0] == sid:
                del self.entries[k]
                n += 1
        return n

    def remember(self, key, query, ids, facts=()):
        old = self.get(key)
        self.entries[key] = dict(
            # An empty query never overwrites the last search topic.
            query=query or old["query"],
            ids=list(dict.fromkeys([*old["ids"], *ids]))[-300:],
            facts=list(dict.fromkeys([*old["facts"], *facts]))[-300:],
            updated=time.monotonic(),
        )
        self.entries.move_to_end(key)
        while len(self.entries) > 256:
            self.entries.popitem(last=False)


STOP = {
    "记得",
    "之前",
    "上次",
    "曾经",
    "什么",
    "那个",
    "这个",
    "我们",
    "你们",
    "他们",
    "怎么",
    "是不是",
    "the",
    "and",
    "that",
    "with",
}


import re

# 纯包裹层：成对出现才剥
WRAPPER_TAGS = ("msg", "text", "forward", "quote", "message")
_WRAPPER_RE = {
    tag: (
        re.compile(rf"<{tag}(?:\s[^>]*)?>", re.I),
        re.compile(rf"</{tag}\s*>", re.I),
    )
    for tag in WRAPPER_TAGS
}

# 带语义的标签：压缩成短记号，但保留信息。
# 内容用 [^<\n]*?（不吃标签、不跨行）：万一某条消息写了未闭合的 <sticker>，
# 配对规则也绝不会一路吃到后面某条消息的 </sticker> 上去（内容安全优先）。
_INLINE_RE = [
    (re.compile(r"<reply>([^<\n]*?)</reply>", re.S | re.I), lambda m: "↩" + _inner(m)),
    (re.compile(r"<at>([^<\n]*?)</at>", re.S | re.I), lambda m: "@" + _inner(m)),
    (re.compile(r"<sticker>([^<\n]*?)</sticker>", re.S | re.I), lambda m: "[表情" + _inner(m) + "]"),
    (re.compile(r"<image>([^<\n]*?)</image>", re.S | re.I), lambda m: "[图片" + _inner(m) + "]"),
]

_CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_SPACE_BETWEEN_CJK = re.compile(rf"(?<=[{_CJK}])[ \t]+(?=[{_CJK}])")
# 逐字拉开写（「很 重 要」「请 注 意」）是刻意的强调：至少三个汉字被空白隔开。
# 折行残留只会插入「一个」空格，不可能连成这种形态，所以用它区分。
_SPACED_OUT = re.compile(rf"[{_CJK}](?:[ \t]+[{_CJK}]){{2,}}")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_BLANK_LINES = re.compile(r"\n\s*\n+")
_LEADING_INDENT = re.compile(r"\n[ \t]+")
_BAD_CHARS = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\ufffd]")


def _inner(match):
    return match.group(1).strip()


# ---- 协议外壳 ---------------------------------------------------------------
# LLM 实际会写出各种形态：<msg> <msg/> <msg /> <msg attr="1"/> <MSG/> </msg>
#   </msg > <text/> <text></text> ……
# 所以这里的匹配一律写成「名字前后允许空白 + 属性任意 + 结尾 / 可有可无」。
# 底线：**只吃标签本身，绝不碰标签以外的任何字符**（\b 保证 <msg_id> 这类
# 名字更长的标签不会被误伤；标签里不允许跨行，避免吃到大段正文）。
def _open_re(name):
    return re.compile(rf"<\s*(?!/)\s*{name}\b[^>\n]*?>", re.I)


def _close_re(name):
    return re.compile(rf"<\s*/\s*{name}\b[^>\n]*?>", re.I)


_MSG_OPEN = _open_re("msg")
_MSG_CLOSE = _close_re("msg")
_OTHER_OPEN = re.compile(r"<\s*(?!/)\s*(?:forward|quote|message)\b[^>\n]*?>", re.I)
_OTHER_CLOSE = re.compile(r"<\s*/\s*(?:forward|quote|message)\b[^>\n]*?>", re.I)
# 抠完配对后的残留（自闭合 / 孤立开闭）：<reply/> <sticker> </image> 之类
_INLINE_BARE = re.compile(r"<\s*/?\s*(?:reply|at|sticker|image)\b[^>\n]*?>", re.I)

# 一个消息块：以 <text> 开头、</text> 收尾。
# 用「锚定 + 非贪婪」而不是数配对，这样正文里真的写了 <text> 也不会被吃掉：
# 只有当 <text> 前面是行首（或只剩 ↩/@/[表情] 这类标记）时才当外壳剥。
# 模型经常把整条回复写在一行里（<msg><reply>…</reply><text>…</text><sticker>…</sticker></msg>），
# 所以标记前缀/后缀也要允许，否则行内那块 <text> 会原样留在记忆里。
_MARKER = r"(?:↩[^\s<]*|@[^\s<]*|\[(?:表情|图片)[^\]]*\])"
_TEXT_OPEN = r"<\s*text\b[^>\n]*?>"
_TEXT_CLOSE = r"<\s*/\s*text\b[^>\n]*?>"
_BLOCK_STRICT = re.compile(
    rf"(?P<lead>(?:^|\n)[ \t]*(?:{_MARKER}[ \t]*)*){_TEXT_OPEN}"
    # 正文里不允许再出现 text 标签：否则非贪婪匹配会跨过后一个 </text> 回溯，
    # 把两条消息合成一条、并留下半截标签（实测踩到）。
    rf"(?P<body>(?:(?!<\s*/?\s*text\b).)*?)"
    rf"{_TEXT_CLOSE}(?P<tail>[ \t]*(?:{_MARKER}[ \t]*)*)"
    # 同一行可能还有下一个块（<text>A</text><text>B</text>），所以结尾也允许 '<'
    rf"(?=[ \t]*(?:{_MARKER}[ \t]*)*(?:\n|$|<))",
    re.S | re.I,
)
# 正文里**字面写了** <text> 的（「他说 3<5 且提到 <text> 这个词」）上面那条匹配不到，
# 用这条兜底：允许正文含同类标签，代价是极端输入（同一行两个块）可能留下标签——
# 宁可留下标签，也不能把两段正文合成一段、或吃掉正文（内容安全优先）。
_BLOCK_LOOSE = re.compile(
    rf"(?P<lead>(?:^|\n)[ \t]*(?:{_MARKER}[ \t]*)*){_TEXT_OPEN}(?P<body>.*?)"
    rf"{_TEXT_CLOSE}(?P<tail>[ \t]*(?:{_MARKER}[ \t]*)*)"
    rf"(?=[ \t]*(?:{_MARKER}[ \t]*)*(?:\n|$|<))",
    re.S | re.I,
)
# 空文本块（<text/>、<text />、<text></text>、只有空白的 <text>  </text>）：
# 里面没有任何内容，所以**任何位置**都可以直接删——删掉不丢字，也就不需要锚定。
# （非空块才需要行级锚定来保护正文里的字面 <text>，见 _BLOCK_STRICT/_BLOCK_LOOSE。）
_TEXT_EMPTY = re.compile(
    rf"(?:{_TEXT_OPEN}\s*{_TEXT_CLOSE}|<\s*text\b[^>\n]*?/\s*>)", re.I
)


def _block_repl(match):
    """剥掉 <text>/</text> 标签本身，里面的内容一个字不动。

    前缀里的标记（``↩7``/``[表情8]``）必须原样带回去——只吃掉标签。
    """
    lead = match.group("lead")
    if not lead.strip():
        lead = "\n"  # 纯行首空白：留一个换行当消息边界
    return lead + match.group("body") + match.group("tail")


# 思考块：连内容一起丢（它是协议内部推理，不是"说过的话"）
_REASONING_PAIR = re.compile(
    r"<\s*reasoning\b[^>\n]*?>(.*?)<\s*/\s*reasoning\b[^>\n]*?>", re.S | re.I
)
_REASONING_OPEN = re.compile(r"<\s*(?!/)\s*reasoning\b[^>\n]*?>", re.I)
_REASONING_BARE = re.compile(r"<\s*/?\s*reasoning\b[^>\n]*?>", re.I)
_MSG_HEAD = re.compile(r"<\s*(?!/)\s*msg\b", re.I)


def strip_reasoning(text):
    """去掉思考块（``<reasoning>…</reasoning>``）。

    - 成对：跨行、带属性、大小写都认，整段删掉。
    - 未闭合 ``<reasoning>``：**只截到下一个 ``<msg`` 之前**（协议上推理在消息
      之前）；找不到 ``<msg`` 就只删标签、正文一个字不动——宁可留下思考，
      也不丢正文（内容安全优先）。
    - ``<reasoning/>``、孤立的 ``</reasoning>``：删标签。
    """
    if not text:
        return ""
    out = _REASONING_PAIR.sub("", str(text))
    while True:
        match = _REASONING_OPEN.search(out)
        if not match:
            break
        nxt = _MSG_HEAD.search(out, match.end())
        if not nxt:
            break  # 后面没有 <msg：只删标签，保留正文
        out = out[: match.start()] + out[nxt.start() :]
    return _REASONING_BARE.sub("", out)


def _strip_wrappers(text):
    """剥掉 message_str 的最外层容器，保留消息之间的边界（换行）。"""
    out = text
    for pattern, repl in _INLINE_RE:
        out = pattern.sub(repl, out)
    out = _TEXT_EMPTY.sub("", out)
    out = _MSG_OPEN.sub("", out)
    out = _MSG_CLOSE.sub("\n", out)
    out = _OTHER_OPEN.sub("", out)
    out = _OTHER_CLOSE.sub("", out)
    for _ in range(3):  # 少数情况会套两层
        new_out = _BLOCK_STRICT.sub(_block_repl, out)
        new_out = _BLOCK_LOOSE.sub(_block_repl, new_out)
        if new_out == out:
            break
        out = new_out
    return _INLINE_BARE.sub("", out)


# ⚠️ 2026-09-19（用户真机日志实测）：`[At 3991867505]` 这种**数字 id** 会原样进注入 ✗
#   模型拿这个号**什么都做不了** ⇒ 纯噪声 ✓
#   有昵称就用 `@昵称` ✓ 没昵称就整个去掉（周围文字本来就点了人名 ✓）
# ⚠️ 2026-09-19：`[At 123]` 这种**结构化** at 壳，过去**没有任何地方剥过** ✗
#   ⇒ 用户日志里 `[At 3991867505]` 原样进注入 ✓
#   **结构化壳不依赖名单表** ✓（与 [Reply]/[CQ:at]/<at> 同档 ✓ —— 仓库既有约定 ✓）
_AT_SHELL = re.compile(r"\[At\s*-?\d+[^\]]*\]", re.I)
# ★ 2026-09-19：引用内容开头的 `[2026-09-19 08:09:37]` 时间戳 ✓ 对模型毫无用处
#   而且它一占就 21 个字符 ⇒ 40 字的引用预算被吃掉一半 ✗（用户真机日志实测 ✓）
_TS_LEAD = re.compile(
    r"^\[?\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?\s*\]?\s*"
)
_AT_WITH_NAME = re.compile(r"\[At\s*-?\d+\s*\(\s*nickname:\s*([^)]*?)\s*\)\s*\]", re.I)
_AT_BARE = re.compile(r"\[At\s*-?\d+\s*\]", re.I)


def tfield(key, value):
    """时间字段：**有值才带这个键** ✓（用户定的规矩：空 t 省略 ✓ 有 t 的要有 ✓）

    这样模型看到的 JSON 里就不会出现 `"t": ""` 这种空壳 ✓
    （空值既没信息 ✓ 又会让模型以为"这里本来有时间但丢了" ✗）
    """
    return {key: value} if value else {}


def strip_at_ids(text):
    """`[At id(nickname: 名字)]` → `@名字`；`[At id]` → 去掉 ✓（id 对模型无用 ✗）"""
    if not text or "[At" not in text:
        return text
    return _AT_BARE.sub("", _AT_WITH_NAME.sub(lambda m: "@" + m.group(1), text))


def clean_text(text, keep=()):
    """剥掉最外层包裹 + 归一空白。孤立的 ``<``、正文里的字面标签都原样保留。

    ``keep`` 是「不能被空白归一碰」的片段——昵称真的可能带空格（``星 月``），
    渲染前先从实体表查出这类名字传进来，正文里的它们原样保留。
    """
    if not text:
        return ""
    out = str(text)
    holders = {}
    # 刻意拉开写的强调句先原样保出来
    for match in _SPACED_OUT.finditer(out):
        token = "\ue000%d\ue001" % len(holders)
        while token in out:
            token += "\ue000"
        holders[token] = match.group(0)
        out = out.replace(match.group(0), token, 1)
    for value in keep or ():
        value = str(value or "")
        # 只保护真正出现、且确实含空白的名字；占位符用私用区字符，不会被其它规则碰到
        if value and value in out and any(c.isspace() for c in value):
            token = "\ue000%d\ue001" % len(holders)
            while token in out:
                token += "\ue000"
            holders[token] = value
            out = out.replace(value, token)
    out = _BAD_CHARS.sub("", out)
    out = _strip_wrappers(out)
    for pattern, repl in _INLINE_RE:
        out = pattern.sub(repl, out)
    out = _LEADING_INDENT.sub("\n", out)
    out = _BLANK_LINES.sub("\n", out)
    out = _SPACE_BETWEEN_CJK.sub("", out)
    out = _MULTI_SPACE.sub(" ", out)
    out = "\n".join(line.strip() for line in out.split("\n")).strip()
    for token, value in holders.items():
        out = out.replace(token, value)
    return out




# ★ 2026-09-18（用户）：媒体描述的预算 **30** ✓（召回侧=后台侧**统一** ✓）
#   理由：压缩/审计发生在"接近的轮" ✓ 原消息还在近期窗口 ⇒ 摘要不需要整段描述 ✓
DESC_CHARS_RECALL = 30


def trim_nested(text, reply_chars=40, desc_chars=DESC_CHARS_RECALL):
    """压缩嵌套的长文本：引用里的原文、表情/图片的视觉描述。

    只截断「嵌套段落」，正文摘要不动；括号用配对扫描，不会被内容里的 ``]`` 提前截断。
    日志实测：表情包的视觉描述平均 276 字符，是摘要里最大的单块开销。

    ⚠️ 2026-09-18（用户）：`desc_chars` 100 → **30** ✓
    理由：引用/正文里的**媒体描述**只需要"是个啥" ✓ 整段机器描述纯烧 token ✗
    """
    if not text:
        return ""
    out, i = [], 0
    while i < len(text):
        reply = _REPLY_HEAD.match(text, i)
        if reply:
            end = _scan_bracket(text, i)
            if end < 0:
                out.append(text[i:])
                break
            raw_inner = text[reply.end() : end].strip()
            inner = raw_inner.strip("[]").strip()
            # ★ 2026-09-19（用户）：引用内容**开头常常是一串时间戳** ✗
            #   `[2026-09-19 08:09:37] 爱奈丽：行吧…` ⇒ 21 个字符白占预算 ✓
            #   （模型看时间戳毫无用处 ✓ 而且 created/event_at 另有字段 ✓）
            #   ⇒ 剥掉后再按 40 字裁 ✓ 让预算全用在**真正的引用内容**上 ✓
            inner = _TS_LEAD.sub("", inner)
            # ⚠️ 判"引用里是不是媒体"要用**没剥括号**的原样 ✓
            #   （剥了 `[` 就匹配不上 `_MEDIA_HEAD` ✗ —— 实测踩到 ✓）
            # ★ 2026-09-18（用户）：**去掉无意义的 msgid、压平嵌套** ✓
            #   `[Reply 120366828: [你好呀]]` ⇒ `(回复：你好呀)` ✓
            #   模型**没有**按 msgid 查询的能力 ⇒ 号码纯噪声 ✗（每条省 ~12 字符 ✓）
            #   引用里若是**媒体块** ⇒ 按更小的预算裁（`desc_chars` ✓ 默认 30 ✓
            #     —— 视觉描述只需要"是个啥" ✓ 不用整段 ✓）
            if _MEDIA_HEAD.match(raw_inner):
                out.append("[Reply: %s]" % _clip(inner, desc_chars))
            else:
                out.append("[Reply: %s]" % _clip(inner, reply_chars))
            i = end + 1
            continue
        media = _MEDIA_HEAD.match(text, i)
        if media:
            end = _scan_bracket(text, i)
            if end < 0:
                out.append(text[i:])
                break
            body = text[media.end() : end].rstrip()
            tail = ""
            split = re.match(r"(.*?)(,\s*file_path:.*)$", body, re.S)
            if split:
                body, tail = split.group(1), split.group(2)
            out.append(f"[{media.group(1)} {_clip(body, desc_chars)}{tail}]")
            i = end + 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def model_text(text, keep=(), reply_chars=40, desc_chars=DESC_CHARS_RECALL):
    """统一入口：剥思考块 + 剥包裹 + 压空白 + 截断嵌套长描述（只影响模型看到的样子）。

    这里**比落库多剥一层思考块**：落库要保原文（用户可能真的引用了 Bot 的思考块），
    但发给模型的东西不该带内部推理——存量清理万一没跑到，模型也不会被污染。
    """
    # At 归一也在这一层 ✓ ⇒ 所有渲染路径（注入/召回/画像/工具）一次覆盖 ✓
    return strip_at_ids(trim_nested(clean_text(strip_reasoning(text), keep), reply_chars, desc_chars))


def _scan_bracket(text, start):
    """从 ``text[start]`` 的 ``[`` 起找到配对的 ``]``（考虑嵌套），返回下标或 -1。"""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return i
    return -1


# ⚠️ 2026-09-19（用户真机日志）：id **可能是负数** ✗
#   实测 archive 槽注入 `[Reply ID: -19 content: …]` 原样穿透（旧正则只认 \d+ ✗）
#   ⇒ 必须 `-?\d+`，否则负 id 的引用永远走不到新格式 ✓
_REPLY_HEAD = re.compile(r"\[Reply ID:\s*(-?\d+)\s*content:\s*")
# v2.18.18：**媒体占位符开头**就算媒体 ✓
# ⚠️ 注意它**本来就不要求闭合的 ]** ✓ —— 用户日志里漏掉的那条
# `[Image 这张图片展示了…`（描述被截断/省略号收尾 ✗）正是靠这一点被接住的 ✓
# ⚠️ 词表要覆盖**宿主与插件双方**产生的形态 ✓（宿主：`[Image …]`/`[Sticker …]` ✓）
# 且**不能**包含 `Reply` ✗ —— 引用壳后面可能跟着真话 ✓（`[Reply x] 你好呀`）
# 词表只写一处 ✓ 两个正则都从它生成 ✗ 免得漂移 ✓
_MEDIA_WORDS = (
    r"Sticker|Image|图片|贴纸|表情|语音|视频|文件|图文|"
    r"voice|video|file|photo|image|face"
)
_MEDIA_HEAD = re.compile(r"\[(" + _MEDIA_WORDS + r")\s+", re.I)          # 显示裁剪用（要捕获组 ✓）
_MEDIA_HEAD_ANY = re.compile(r"^\s*\[(?:" + _MEDIA_WORDS + r")(?:\s|\])", re.I)   # 判据用（不要求空格/闭合 ✓）


def _clip(text, limit):
    text = text.strip()
    if limit and len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


def short_time(value):
    """epoch → ``MM-DD HH:MM``（同年）/ ``YYYY-MM-DD HH:MM``（跨年）。

    跨年**一定要带年份**：模型虽然知道"现在"，但看到「11-16 10:33」会当成今年 ✗
    （事实那边用 ``short_day``，口径保持一致）
    """
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return ""
    if ts <= 0 or ts < 946684800 or ts > 4102444800:
        return ""  # 非法/越界时间一律留空（绝不渲染 1970-01-01）✗
    stamp = time.localtime(ts)
    if stamp.tm_year == time.localtime().tm_year:
        return time.strftime("%m-%d %H:%M", stamp)
    return time.strftime("%Y-%m-%d %H:%M", stamp)


def full_time(value):
    """epoch → ``YYYY-MM-DD HH:MM``（本地时区）。

    模型对浮点秒没有任何直觉（1772908601.6758957 是几号？），可读时间反而让它
    判断时间线更准；年份必须带上，跨年批次才不会有歧义。
    """
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


# 给模型看的类别短码：两字母，配合静态规则块里的一行图例（规则块走缓存，不额外花每轮 token）
CATEGORY_CODES = {
    "event": "ev",
    "fact": "fa",
    "preference": "pr",
    "commitment": "co",
    "relationship": "re",
    "profile": "pf",
    "resource": "rs",
    "self": "sf",
    "common": "cm",
}
# 静态 system prompt 用：码表只出现一次，动态注入块不重复。
CATEGORY_CODE_LEGEND = (
    "类别码：ev=事件 fa=事实 pr=偏好 co=约定 re=关系 "
    "pf=画像 rs=资源 sf=自我 cm=常识 rl=铁律"
)
# 记忆类别优先级：越小越"必须留在上下文里"（用于注入排序与裁剪）
CATEGORY_RANK = {
    "rule": 0,
    "commitment": 1,
    "preference": 2,
    "profile": 3,
    "relationship": 4,
    "resource": 5,
    "event": 6,
    "fact": 7,
    "note": 8,
}

def named_pair(entity_id, name=None):
    """``qq:769690776(周武)`` —— 稳定 ID 在前，名字在括号里。

    模型照着抄 subject 时拿到的是 ID；同时它知道这个人叫什么，写摘要就能用名字。
    """
    entity_id = str(entity_id or "")
    name = str(name or "").strip()
    if not entity_id:
        return name
    if not name or name == entity_id:
        return entity_id
    return f"{entity_id}({name})"


def bare_id(value):
    """从 ``qq:769690776(周武)`` 里取回 ``qq:769690776``。"""
    value = str(value or "").strip()
    head, sep, _ = value.partition("(")
    return head.strip() if sep else value


def squeeze(text):
    """轻量归一：去控制/替换字符、压缩空白、去掉 CJK 之间的空格。

    检索两侧都过一遍，于是「翅 膀」这种被插入空格的写法依然能命中「翅膀」。
    """
    if not text:
        return ""
    out = _BAD_CHARS.sub("", str(text))
    out = _SPACE_BETWEEN_CJK.sub("", out)
    return _MULTI_SPACE.sub(" ", out).strip()


def index_grams(text):
    """把文本切成「与 query_tokens 完全一致口径」的 n-gram 串，用于 FTS5 索引。

    唯一的硬性要求：**索引候选集必须是打分结果的超集**——打分侧是子串匹配
    （``instr``，见 ``_lexical_sql``），任何"打分能命中、索引查不到"的行都会被
    静默漏召回（实测：单字查询「猫」查不到「…欢猫」，就是索引里只留了双字
    滑窗、没有单字）。

    于是收录：每个字/字符本身（覆盖单字词元），以及每一对相邻字/字符
    （覆盖 ≥2 字词元——它若出现在文本里，它的每一对相邻字也都在文本里，
    词元之间是 OR，命中一对即入选）。查询侧对 ≥3 字的词元做同样的拆对
    （见 fts_match_query），所以「iph」能查到「iphone」这种跨词边界的子串。
    汉字必须留双字滑窗：trigram 分词器对中文双字词命中不了（实测）。
    索引前走同一个 squeeze()/casefold，保证与打分侧口径一致。
    """
    squeezed = squeeze(text or "")
    out = []
    for chunk in re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", squeezed.casefold()):
        out.extend(chunk)  # 单字：单字查询词元唯一的命中机会
        if len(chunk) >= 2:
            out.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return " ".join(out)


# 旧名（v2.11.0）：索引体只含双字滑窗，现已补齐单字，保留别名避免外部引用失效
bigram_body = index_grams


def fts_match_query(tokens):
    """把词元拼成安全的 FTS5 MATCH 表达式（一律当短语、内部引号双写）。

    ≥3 字的词元再拆成二元组：打分侧是**子串**匹配，原文里的词可能更长
    （"iph" ⊂ "iphone"），整词在索引里查不到，但它的二元组一定在
    （``index_grams`` 收录了每个相邻对）→ 候选集不会漏。
    词元之间是 OR：只放宽候选，绝不收窄。
    """
    quoted = []
    for token in tokens:
        value = str(token or "").strip()
        if not value:
            continue
        pieces = (
            [value]
            if len(value) <= 2
            else [value[i : i + 2] for i in range(len(value) - 1)]
        )
        quoted.extend('"%s"' % piece.replace('"', '""') for piece in pieces)
    return " OR ".join(quoted)


# ★ 2026-09-19：中文分词（jieba）**软依赖** ✓ —— 照 KiraOS 的写法：
#   "jieba 是中文分词的最佳选择，但**不应该作为插件加载的硬依赖**"
#   没装就降级为按字切分 ✓ FTS5 仍可工作 ✓ 只是中文查准率差一些 ✓
# 依赖自动安装由框架负责（插件根 requirements.txt ⇒ plugin_installer 自动 pip ✓）
# 本模块**不依赖框架** ✓ 用标准库日志（jieba 缺失时提示一次 ✓）
_log = logging.getLogger("alife_memory_z")


try:
    import jieba  # type: ignore

    _JIEBA_AVAILABLE = True
    # 静音 jieba 自己的 DEBUG 输出（每次建词典会打 4 行 ✗ 对用户是噪声 ✓）
    try:
        jieba.setLogLevel(logging.WARNING)
    except Exception:  # pragma: no cover - 老版本没有这个方法
        pass
except ImportError:  # pragma: no cover - 取决于环境
    jieba = None  # type: ignore
    _JIEBA_AVAILABLE = False
    _log.warning(
        "jieba 未安装 ⇒ 中文检索降级为按字切分（功能不受影响，查准率略低）。"
        "建议: pip install jieba（KiraAI 装插件时会自动装 ✓）"
    )


def warm_jieba():
    """**预热**分词词典 ✓ —— 由插件加载时调用（后台线程 ✓ 不阻塞启动 ✓）

    为什么要在加载时做（用户实测）：
        jieba 首次分词才建词典 ⇒ 日志里出现 `Loading model cost **1.340 seconds**` ✗
        而且它是**对话进行中**才发生 ✗ ⇒ 那 1.3 秒砸在一次真实回复的链路上 ✓
    ⇒ 和 KiraOS 一样，把这一步放在**插件初始化**阶段 ✓✓
    （词典有本地缓存 ✓ 之后每次启动都快 ✓ 这里只是"提前疼一下"✓）
    """
    if not _JIEBA_AVAILABLE:
        return False
    try:
        jieba.initialize()
        return True
    except Exception:  # pragma: no cover - 预热失败不影响功能（首次用时再建 ✓）
        _log.warning("jieba 预热失败（不影响功能 ✓ 首次检索时会再尝试 ✓）")
        return False


def score_tokens(query):
    """**打分用**的词元 ✓ —— 有 jieba 时按**词**切，否则回退到 query_tokens（按字 ✓）。

    为什么打分侧要按词切（用户实测）：
      `doro 的 bot 是谁` 按字切会得到 [的d, do, or, ro, 的b, bo, ot, …] ✗
      ⇒ 任何**两字重叠**都算命中 ⇒ `doro` 一个词命中 **761** 条 ⇒ 真正那条被埋 ✓
      按词切 ⇒ [doro, bot] ✓ ⇒ 命中数大减 ✓ 与"叫/名字"邻近的行才能排上来 ✓

    ⚠️ 只改**打分** ✗ 不动 FTS 查询词元（`query_tokens` / `index_grams`）✓
      因为索引侧必须保持"打分能命中的都能查到"的超集不变式 ✓（见 index_grams 注释）
      ⇒ 所以**不需要重建索引** ✓ 风险为零 ✓
    """
    text = squeeze(query or "").casefold()
    if not _JIEBA_AVAILABLE:
        return query_tokens(query)
    words = []
    for chunk in re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", text):
        if re.match(r"[a-z0-9_]", chunk):
            words.append(chunk)
            continue
        # 汉字片段：jieba 切词；单字词与虚词不进打分（噪声 ✓）
        words.extend(w for w in jieba.lcut(chunk) if len(w) >= 2)
    out = sorted({w for w in words if w.strip()} - STOP)
    return out or query_tokens(query)   # 切没了就回退（安全 ✓）


def query_tokens(query):
    """查询侧词元（与 relevance 口径完全一致），供 SQL 粗筛复用。"""
    chunks = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", squeeze(query).casefold())
    return sorted(
        {
            t
            for chunk in chunks
            for t in (
                [chunk]
                if len(chunk) < 2 or not re.match(r"[\u3400-\u9fff]", chunk)
                else [chunk[i : i + 2] for i in range(len(chunk) - 1)]
            )
        }
        - STOP
    )


def relevance(query, text):
    tokens = score_tokens(query)
    lowered = squeeze(text).casefold()
    return sum(len(t) * (t in lowered) for t in tokens)


def short_day(ts):
    """给模型看的日期短码：今年不带年，跨年才带（省 token，且一眼能读）。"""
    try:
        stamp_value = float(ts)
    except (TypeError, ValueError):
        return ""
    if stamp_value < 946684800 or stamp_value > 4102444800:
        return ""  # 2000~2100 年之外（含 0/负数/毫秒戳）一律视为缺失：绝不渲染 1970-01-01 ✗
    stamp = time.localtime(stamp_value)
    now = time.localtime()
    if stamp.tm_year == now.tm_year:
        return time.strftime("%m-%d", stamp)
    return time.strftime("%Y-%m-%d", stamp)


def rotation_order(ids, shown, used, limit):
    """轮换挑选的**纯计算**版本（与 storage.rotation_pick 完全同一套顺序）。

    ① 从没展示过的优先 ② 用过/展示比例高的次之 ③ 展示次数少的再后
    ④ 同条件按传入顺序（那本身就是相关性排序）稳定。
    """
    wanted = [str(i) for i in (ids or []) if i]
    if not wanted or limit <= 0:
        return []
    order = {rid: index for index, rid in enumerate(wanted)}

    def key(rid):
        count_shown, count_used = shown.get(rid, 0), used.get(rid, 0)
        return (
            0 if count_shown == 0 else 1,
            -(count_used / count_shown) if count_shown else 0,
            count_shown,
            order.get(rid, 0),
        )

    return sorted(wanted, key=key)[: int(limit)]


def overlap_hit(text, reply, min_hits=2):
    """这轮回复里有没有"用上"这条记忆？（轮换槽位的反馈信号）

    - 普通情况：词元重合数 >= min_hits
    - 独特词元（连续数字，如 QQ 号/编号）：命中 1 个即算（几乎不可能碰巧出现）
    """
    if not text or not reply:
        return False
    tokens = {t for t in query_tokens(text) if len(t) >= 2}
    if not tokens:
        return False
    body = squeeze(reply).casefold()
    hits = sum(1 for token in tokens if token in body)
    if hits >= max(1, int(min_hits)):
        return True
    for token in tokens:
        if token.isdigit() and len(token) >= 5 and token in body:
            return True
    # 长数字串（QQ 号/编号）：从原文里直接抓，出现即算命中
    body = squeeze(reply).casefold()
    for chunk in re.findall(r"\d{5,}", str(text)):
        if chunk in body:
            return True
    return False


def bot_facts(facts, current_sid="", short=None, self_id="", session_codes=None):
    """给 Bot 看的精简事实视图：只留判断与追溯必需的字段。

    内部簿记（fingerprint/deleted/revision/merge_pending/audited）、
    分类装饰（scenario/tags）、以及只给审计用的大段 reason 都不进上下文。

    ``short`` 可传入「真实 id → 短码」的转换函数；空字段（如空的 relations）
    直接省略——它们在每轮注入里是纯开销。
    """
    view = []
    for fact in facts:
        # 短键 + 类别短码 + 默认值省略：每轮都发的东西，信封比内容还贵
        item = {
            "c": CATEGORY_CODES.get(fact.get("category", ""), fact.get("category", "")),
            "u": fact.get("subject", ""),  # 稳定实体 ID 不变，工具要用它
            "x": fact.get("content", ""),
        }
        importance = fact.get("importance", 5)
        if importance != 5:
            item["imp"] = importance
        relations = fact.get("verified_relations", fact.get("relations", []))
        if relations:
            item["rel"] = relations
        if fact.get("sid") and fact["sid"] != current_sid:
            item["sid"] = fact["sid"]
            if session_codes:
                item["from"] = session_codes.get(fact["sid"], fact["sid"])
            if fact.get("src_user"):
                item["by"] = fact["src_user"]
        if self_id and fact.get("src_user") == self_id:
            # v2.18.9 回声防线：这条事实的**最新来源是助手自己** ✓
            # 模型必须知道"这是我自己说过的话" ✗ 不能当成外部证据 ✓
            item["self"] = 1
        source = fact.get("src") or (fact.get("sources") or [None])[-1]
        if source:
            # 短码映射里没有的（例如 sources 兜底值）就用原值，绝不输出 null
            item["src"] = (short(source) or source) if short else source
        if fact.get("src_kind"):
            item["src_kind"] = fact["src_kind"]
            if fact.get("src_tool_kind"):
                item["src_tool_kind"] = fact["src_tool_kind"]
        created = fact.get("created")
        # 事件时间（这条事实讲的事发生在什么时候）优先；老数据没有 event_at 时退回 created。
        created = fact.get("created")
        # ③ 2026-09-19（用户）：**不许拿 created 兜底** ✗
        #   created = 入库/整理这条事实的时刻（storage.py:2073 原话"不是事件时间"）
        #   拿它冒充"事发生在什么时候" = 造假 ✓ ⇒ 老数据没有 event_at ⇒ **不显示时间** ✓
        event_at = fact.get("event_at")
        event_end = fact.get("event_end") or event_at
        if isinstance(event_at, (int, float)) and event_at > 0:
            # ② 2026-09-19（用户）：**只有"那一刻就是那句话"才给到分钟** ✓
            #   · 单来源（sources 只有 1 条）⇒ 它的时间就是那句话被说出来的时刻 ⇒ 到分钟 ✓
            #   · 多来源 ⇒ 是"一段时间的概括" ⇒ 标分钟就是假精确 ✗ ⇒ 只到日 ✓
            _srcs = fact.get("sources")
            _single = isinstance(_srcs, list) and len(_srcs) == 1
            if _single and short_time(event_at):
                item["t"] = short_time(event_at)
            elif short_day(event_at):
                item["t"] = short_day(event_at)
            # 跨天的事（多条时间点合并进来的）给个区间，不然"塌成一点" ✗
            if isinstance(event_end, (int, float)) and event_end - event_at > 86400:
                if short_day(event_end):
                    item["t2"] = short_day(event_end)
        # 记录时刻和事情发生的时间不是一回事：差得远时才附上，省 token
        if (
            isinstance(created, (int, float))
            and created > 0
            and isinstance(event_end, (int, float))
            and created - event_end > 86400
        ):
            item["rec"] = short_day(created)
        if fact.get("relationship_status") == "needs_review" or fact.get(
            "relation_warnings"
        ):
            item["rev"] = True
        if fact.get("category") == "common":
            scope = str(fact.get("common_scope") or "world")
            item["cs"] = "f" if scope == "field" else "w"
        view.append(item)
    return view


# ── v2.17.0：事实「按主体分组」视图（默认）──────────────────────────────
# 旧的扁平视图 bot_facts() 一律不动 ✓ 它是回滚路径（view="flat" 时逐字节一致 ✓）
# 组内位置固定：[类别, 内容, 重要性?, 关系?, 时间?, 谁说的?] ✓ 尾部为空就省略 ✗
# 注意：只允许从**尾部**省 —— 中间位有值、前一位没有时，前一位补 "" 占位 ✓

FACT_VIEW_GROUPED = "grouped"
FACT_VIEW_FLAT = "flat"


def _grouped_row(fact, codes, current_sid, self_id=""):
    """一条事实 → 位置化行（尾部省略）。"""
    category = fact.get("category") or ""
    row = [CATEGORY_CODES.get(category, category), str(fact.get("content") or "")]

    importance = fact.get("importance")
    row.append(importance if importance not in (None, "", 5) else "")

    rels = []
    own = codes.get(str(fact.get("subject") or ""), str(fact.get("subject") or ""))
    for rel in fact.get("verified_relations") or fact.get("relations") or []:
        if not isinstance(rel, dict):
            continue
        head = codes.get(str(rel.get("subject") or ""), str(rel.get("subject") or ""))
        tail = codes.get(str(rel.get("object") or ""), str(rel.get("object") or ""))
        # 省略主体 = 就是本组主体（仅此一种情况 ✗ 其他一律写全，避免歧义 ✓）
        if head and own and head == own:
            head = ""
        rels.append("%s>%s>%s" % (head, rel.get("predicate") or "", tail))
    row.append(";".join(rels))

    event_at = fact.get("event_at")
    label = short_day(event_at) if event_at else ""
    event_end = fact.get("event_end")
    if label and event_end:
        end = short_day(event_end)
        if end and end != label:
            label = "%s~%s" % (label, end)
    row.append(label)

    speaker = codes.get(str(fact.get("src_user") or ""), "")
    room = str(fact.get("sid") or "")
    who = speaker
    if room and current_sid and room != current_sid:
        who = "%s@%s" % (speaker, codes.get(room, room)) if speaker else "@" + codes.get(room, room)
    kind = str(fact.get("src_kind") or "")
    if not kind:
        kind = "assistant" if (self_id and fact.get("src_user") == self_id) else (
            "user" if speaker else ""
        )
    tag = {"user": "[U]", "assistant": "[A]", "tool": "[T]"}.get(kind, "")
    if tag:
        who = (tag + " " + who).strip()
    row.append(who)

    # v2.18.9 回声防线：本条事实的**最新来源是助手自己** → 末尾打 self 旗标 ✓
    # 尾部空缺会被下面的循环吞掉 ✗ 所以平时零开销 ✓
    row.append("self" if (self_id and fact.get("src_user") == self_id) else "")

    while row and row[-1] == "":
        row.pop()
    return row


def bot_facts_grouped(facts, current_sid="", codes=None, self_id="", links=None):
    """按主体分组渲染事实（v2.17.0 默认视图）。

    形如::

        {"n1": [["pf", "周武是用户的大学室友", 7, "n1>朋友>n2", "08-20"]],
         "n2": [["ev", "昨天和周武一起吃饭", 6, "", "09-11"]]}

    - 组键 = **主体短码**（真实 id 见 names 表 ✓）→ 主体只出现一次 ✓
    - 组间/组内都沿用上游顺序（提到的人/高相关在前 ✓ 不要重排 ✗）
    - 关系用短码三元组 "主体>关系>客体" ✓ 多条用 ";" 连接 ✓
    """
    codes = codes or {}
    groups = {}
    for fact in facts or []:
        subject = str(fact.get("subject") or "")
        if not subject:
            continue
        # 身份绑定（批次 3）：先把写法归一到规范键 ✓ 同一人不再分成两组 ✓
        #   `links` 由后端一处算好（含结构化归一 + 人工绑定）✓ 这里只查表 ✓
        _base = codes.get(subject, subject)
        key = (links or {}).get(_base, _base)
        groups.setdefault(key, []).append(
            _grouped_row(fact, codes, current_sid, self_id=self_id)
        )
    out = {}
    for key in groups:  # 保持上游顺序（提到的人/高相关在前）
        out[key] = list(groups[key])
    return out


# ── 下沉 / 上浮（2026-09-18 批次 2）──────────────────────────────
#  术语沿用既有的「常驻 / 轮换槽位」✓ **不新增概念** ✓
#  · 「下沉」= 移出常驻 ⇒ 天然落进轮换候选（轮换池是独立查询 ✓ 不在本文件过滤 ✓）
#  · 「上浮」= 在轮换里被**「用上」**（`rotate_used` ↑）⇒ 分数回升 ⇒ 回常驻 ✓
#  分数只用于**排序 / 过滤**，**不落库** ⇒ 随时可逆、可调 ✓
NEVER_SINK_IMPORTANCE = 8          # 硬规则：重要度 ≥ 8 **永不沉** ✓（用户拍板 ✓）
# ★ 2026-09-23（用户拍板重新标定）：
#   · 新鲜度改为**按天线性归零** ✗ 原 30/90 档位会让整批事实卡在同一分数、
#     到期又**成批**沉下去（线上实测：迁移批次全卡在 12 分 ⇒ 体检页"0 条会下沉"）
#   · 被用次数加**时效半衰期** ✗ 原实现只增不减 ⇒ "用够几次"变成**永久免沉**（没有出口）
#   · 本轮被命中（实体/关键词/内容匹配）的事实给固定加分 —— 相关性优先 ✓
#     但**命中不等于豁免** ✗：仍需过"重要度 + 时效"这一关 ✓
FRESH_FADE_DAYS = 60               # 新鲜度 10 分 → 0 分所需天数（线性）
RELEVANCE_BONUS = 4                # 本轮被命中：+4 分（≈ 24 天新鲜度的等价量）
USED_HALF_LIFE_DAYS = 60           # 被用次数的时效半衰期（天）
USED_CAP = 5                       # 单条事实被用次数参与计分的上限（防"刷分"）


def _effective_used(fact, now):
    """「被用上」的有效次数 ✓ 带时效半衰期

    · 没有 `rotate_used_at`（存量数据 / 手工造的行）⇒ **不衰减** ✓
      —— 不改变历史数据的命运 ✓（升级不惊扰存量 ✓）
    """
    used = min(int(fact.get("rotate_used") or 0), USED_CAP)
    if used <= 0:
        return 0.0
    try:
        last = float(fact.get("rotate_used_at") or 0)
    except (TypeError, ValueError):
        last = 0.0                      # 脏值 ⇒ 当作"没有时间戳"（不衰减 ✓ 绝不炸注入）
    if last <= 0:
        return float(used)
    days = max(0.0, (now - last) / 86400.0)
    return used * (0.5 ** (days / USED_HALF_LIFE_DAYS))


def fact_sink_score(fact, now=None, bonus=0):
    """常驻分数 ✓ = 重要度×2 + 有效被用次数×3 + 新鲜度（+ bonus）

    与既有机制对齐 ✓：`rotate_used` 就是轮换槽记的"被用过"次数 ✓
    （`mark_rotation(kind="fact")` 已在批次 1 修好 ✓ 所以这个数是真实的 ✓）
    新鲜度按事实的 `created` 算 ✓：10 分线性衰减，`FRESH_FADE_DAYS` 天后归零 ✓
    `bonus` 给"本轮被命中"的那批 ✓（相关性优先 ✓ 低重要度仍会随时间让位 ✓）
    """
    now = now or time.time()
    importance = int(fact.get("importance") or 5)
    used = _effective_used(fact, now)
    created = float(fact.get("created") or 0)
    age_days = max(0.0, (now - created) / 86400.0) if created else 9999.0
    fresh = max(0.0, 10.0 * (1.0 - age_days / FRESH_FADE_DAYS))
    return int(round(importance * 2 + used * 3 + fresh + bonus))


def should_sink(fact, threshold, now=None, bonus=0):
    """这条事实这次要不要**让位** ✓（移出常驻 ✓ 不是删除 ✓）

    ⚠️ 两条保守规则：
    · 重要度 ≥ `NEVER_SINK_IMPORTANCE` ⇒ **永不沉** ✓
    · 判不出年龄（没有 `created`）⇒ **不沉** ✓（不知道就别动 ✓）
    """
    if int(fact.get("importance") or 5) >= NEVER_SINK_IMPORTANCE:
        return False                      # ★ 硬规则：≥8 永不沉 ✓
    if not fact.get("created"):
        return False                      # ★ 没有时间戳 ⇒ 保守不沉 ✓
    return fact_sink_score(fact, now=now, bonus=bonus) < int(threshold)


def sink_filter(facts, threshold, now=None, bonus=0):
    """把该下沉的从**常驻**列表里摘掉 ✓ 其余保持原顺序 ✓

    · `threshold <= 0` ⇒ **关闭下沉** ✓（原样返回 ✓ 便于随时回退 ✓）
    · `bonus`：本轮被命中的那批传 `RELEVANCE_BONUS` ✓（相对更不容易被沉 ✓）
    · **不删数据** ✓ 被摘掉的仍在轮换候选池里（调用方保证 ✓）⇒ 被「用上」就能浮回来 ✓
    """
    if not threshold or int(threshold) <= 0:
        return list(facts or [])
    now = now or time.time()
    return [
        f
        for f in (facts or [])
        if not should_sink(f, threshold, now=now, bonus=bonus)
    ]


def low_value_self(fact, self_id, drop_max_importance):
    """★ 2026-10-10：**唯一判据** —— 自我认知类事实且重要度不够 ⇒ 不参与任何召回路径 ✓

    用户诉求：bot 的自述（碎碎念）别污染真内容 ✓ 默认门槛 6 ✓（0 = 关闭 = 旧行为 ✓）
    使用点（**一处定义、两处使用** ✓）：
      1) `self_only_last` —— 常驻/打包路径 ✓
      2) `main.rotation_extras` 的 fact_pool —— **轮换槽**路径 ✓
         （轮换池是独立 SQL 查询 ✗ 不过 pack_facts ⇒ 必须单独接 ✓
           这正是"轮换槽要不要也过滤"的答案：**要** ✓）
    取舍：只挡"召回/被动注入" ✓；面板、画像、主动检索**仍能查到** ✓（不丢记忆 ✓）
    """
    if not drop_max_importance or not self_id:
        return False
    try:
        imp = int(fact.get("importance") or 5)
    except Exception:
        imp = 5
    return fact.get("src_user") == self_id and imp <= int(drop_max_importance)


def self_only_last(facts, self_id="", drop_max_importance=0):
    """把"最新来源是助手自己"的事实**稳定地排到最后** ✓（2026-09-18，回声防线的排序侧 ✓）

    背景：`v2.18.9` 已经在渲染时给这类事实打 `self` 旗标 ✓，但**排序没动** ✗
    ⇒ 它们照样占常驻版面 ✓ —— 用户担心"bot 自己说错的话反过来误导自己" ✓
    做法：**只降序、不删除、不隐藏** ✓（主动召回仍能搜到 ✓ 只是不主动占版面 ✓）
    判据与渲染侧的 self 旗标**完全一致**（`src_user == self_id`）✓ 一处定义、两处使用 ✓
    用**稳定分区**（保持原有相对顺序 ✓）所以不会打乱重要度/时间排序的语义 ✓

    ★ 2026-10-10 新增 `drop_max_importance`（用户诉求 ✓）：
      自我认知类事实（`src_user == self_id`）里**重要度 <= N 的直接不参与召回** ✓
      —— bot 的自述碎碎念不再污染真内容 ✓
      取舍：只影响**召回/被动注入** ✓；面板、画像、主动检索**仍能查到** ✓（不丢记忆 ✓）
      `0` = 关闭 => 与旧行为**逐字节一致** ✓（老用户零变化 ✓）
    """
    if drop_max_importance and self_id:
        # ★ 2026-10-10：改调**唯一判据** low_value_self ✓（与轮换池同一函数 ✓ 不会走偏 ✓）
        facts = [f for f in (facts or []) if not low_value_self(f, self_id, drop_max_importance)]
    if not self_id:
        return list(facts or [])
    others, selves = [], []
    for fact in (facts or []):
        (selves if fact.get("src_user") == self_id else others).append(fact)
    return others + selves


def pack_facts(facts, current_sid="", short=None, view=FACT_VIEW_GROUPED, codes=None,
               self_id="", links=None, self_drop_max=0):
    facts = self_only_last(facts, self_id, self_drop_max)      # ★ 自述降序 +（可选）低价值自述不召回 ✓
    """事实渲染入口：grouped=分组视图（默认 ✓）/ flat=旧的扁平视图（逐字节不变 ✓）。"""
    if view == FACT_VIEW_FLAT:
        return bot_facts(facts, current_sid, short=short, self_id=self_id)
    return bot_facts_grouped(facts, current_sid, codes=codes, links=links, self_id=self_id)


def short_names(names, codes):
    """{短码: [真实 id, "名字|别名"]} —— 分组视图下主体只给短码，名字在这里一次给全 ✓。"""
    table = {}
    for item in names or []:
        real = item.get("id")
        if not real:
            continue
        label = "|".join([item.get("name") or "", *(item.get("aliases") or [])]).strip("|")
        table[codes.get(real, real)] = [real, label]
    return table


FACT_RELATION_RULE = (
    "关系：主体>关系>客体；省略主体即本组主体；末位 self=自己曾经说过的，不是外部证据。"
)



def archives_flat(value):
    """档案区取值：兼容「扁平列表」与「{permanent, recent} 分组」两种形态 ✓"""
    if isinstance(value, dict):
        return list(value.get("permanent") or []) + list(value.get("recent") or [])
    return list(value or [])

# ── 给模型看的一行真实示例（由渲染器产出 ✓ 所以永远不会与格式脱节）──
def grouped_example():
    """示例用的固定样本：主体短码 n1 + 两条事实（含重要度、关系、时间）。"""
    return bot_facts_grouped(
        [
            {
                "category": "profile",
                "subject": "qq:1",
                "content": "某条画像事实（示例）",
                "importance": 7,
                "event_at": 1755648000,
            },
            {
                "category": "event",
                "subject": "qq:1",
                "content": "某条事件事实（示例）",
                "importance": 6,
                "event_at": 1755648000,
            },
        ],
        "",
        codes={"qq:1": "n1"},
    )


GROUPED_EXAMPLE_LINE = (
    "读取示例（真实渲染输出）："
    + __import__("json").dumps(grouped_example(), ensure_ascii=False, separators=(",", ":"))
    + " —— 组键是主体短码，组内每行按 [类别, 内容, 重要度?, 关系?, 时间?, 谁说的?] 读；"
    "names 里查短码对应的账号与名字。\n"
)


# v2.18.12：媒体判定的**正确口径** = 「剥掉引用壳与媒体块之后，还剩不剩实质文字」
# 旧口径要求"整条只有 [表情]/[图片] 标记" ✗ → 外面套一层 [Reply …] / ↩N
# 或换成英文 [Sticker …] 就漏过去了 ✓（日志实测：贴纸描述被当成正文召回 ✓）
# v2.18.12/14 媒体判定的**正确口径** = 「剥掉引用壳 / 结构化 at 壳 / 媒体块之后，
# 还剩不剩实质文字」✗ 旧口径要求"整条只有 [表情]/[图片] 标记" ✓ 太窄 ✗
# ⚠️ @… **不放进下面这些模式** ✗ 放进去会被 sub 直接删掉 ✓
#    那样就轮不到 media_only 里用**名字表**定性了 ✓（@他就好了 曾被整条吃掉 ✓）
# ⚠️ 正则里**不要写行内注释** ✗ 注释会打断字符串的隐式拼接 → 语法错误 ✓（踩过 ✓）
_ENVELOPE = re.compile(
    r"\[Reply[^\]]*\]|\[Reply[^\]]*$"
    r"|\[CQ:at[^\]]*\]|\[at[^\]]*\]|<at[^>]*>"
    r"|↩\S+",
    re.I,
)
_MEDIA_BLOCK = re.compile(
    r"\[(?:表情|图片|贴纸|语音|视频|文件|Sticker|sticker|face|image|video|voice)[^\]]*\]",
    re.I,
)


# v2.18.19：**召回侧**的短化 ✓ —— 与发给压缩模型的 `model_text` **分开** ✗
#  · 压缩模型要描述（那是压缩的原料 ✓ 删了就永远提取不出图片相关事实 ✗）
#  · 主模型不要视觉细节 ✗ ⇒ 图片/贴纸一律转占位 `[Image]` ✓
#  · 没有 content 的"纯引用壳"（`[Reply ID: -71，[Sticker …]` / `[Reply -208950819]`）
#    是真·噪声 ✗（既看不到原消息 ✓ 又占字符 ✗）⇒ 转 `[Reply]` ✓
# 一个正则覆盖**两种**引用壳 ✓ 免得"带 content 的那支"漏下孤立的 `]` ✗（实测踩过 ✓）
# **两段式** ✓ 顺序不能反 ✗（反过来"带 content"那支会被普通壳整段吃掉 ✓ 原文就丢了 ✗）
# ① 带 content 的（能显示引用的原消息 ✓ 那是有用信息 ✓）→ 保壳+原文 ✓
_REPLY_WITH_CONTENT = re.compile(
    r"\[Reply(?:\s+ID)?[:\s,，]*\s*-?\d+\s*content:\s*([^\[\]]*?)\s*\]",
    re.I,
)
# ② 容忍**一层嵌套** ✓ —— 真实形态：`[Reply ID: -71，[Sticker 一张动漫风格的插画]]`
#    （用户日志实测 ✓）里层已经先被转成 `[Image]` ✓ 所以这层要能吃下 `[…]` ✓
_REPLY_SHELL = re.compile(r"\[Reply(?:[^\[\]]|\[[^\[\]]*\])*\]", re.I)
_MEDIA_INLINE = re.compile(r"\[(?:" + _MEDIA_WORDS + r")[^\]]*\]", re.I)


def recall_text(text, limit=0):
    """把一条记忆短化成**给主模型看**的样子 ✓（壳与媒体转占位 ✓）

    ⚠️ 压缩侧**不要**用它 ✗ —— 那边必须保留描述 ✓（见 `model_text` ✓）
    """
    out = str(text or "")
    # ⚠️ 顺序很重要 ✗：必须先媒体、再带 content 的引用壳、最后收其余的壳 ✓
    out = _MEDIA_INLINE.sub(" [Image] ", out)                    # ① 里层媒体 → 占位 ✓
    out = _REPLY_WITH_CONTENT.sub(                               # ② 能显示原消息的 → 保留 ✓
        lambda m: " [Reply] " + m.group(1).strip() + " ", out
    )
    out = _REPLY_SHELL.sub(" [Reply] ", out)                     # ③ 只有 id 的 → 纯占位 ✓
    out = re.sub(r"\s+", " ", out).strip()
    if limit and len(out) > limit:
        return out[:limit].rstrip() + "…"
    return out


def is_tool_step(row):
    """是不是"工具步"记录 ✓（capture 时打了 `category='tool'` ✓）

    v2.18.19：工具步对**主模型**是过程噪声 ✗ ⇒ 召回侧全链路过滤 ✓
    但对**压缩模型**是上下文 ✗ ⇒ 压缩侧保留（转短占位 ✓）
    """
    return str((row or {}).get("category") or "") == "tool"




# ★ 2026-09-18（用户实测）：**未闭合/被截断的壳** ✗
#   例：`[Reply ID: -13`（没有右括号 ✓ 是数据被截断的样子 ✓）
#   `_ENVELOPE` 要求闭合 ⇒ 剥不掉它 ⇒ 会被误判成"有内容" ✗
#   ⇒ 轮换槽曾注入这种废条目 ✓ 这里补一个"尾部未闭合标记"的剥离 ✓
#   ⚠️ 只剥**行尾未闭合**的 ✓ —— `[Reply x] 你好呀`（闭合 + 有正文 ✓）绝不能被误杀 ✓
_ENVELOPE_OPEN = re.compile(r"\[(?:Reply|At\b|CQ:at\b)[^\]]*$|<at[^>]*$", re.I)


def media_only(content, names=()):
    """是不是"只有引用壳/at 壳/媒体块、没有实质文字"的消息 ✓（默认不进召回 ✓ 数据保留 ✓）

    v2.18.14：**@ 的定性改用"已知名字表"** ✗ 不再靠猜长度 ✓
      · `@X` 里 X 是**已知成员名**（或纯数字 QQ 号）→ 那是 at 壳 ✓
      · 否则 → 那是**正文** ✓（例如 `@他就好了` ✓ 绝不能被吃掉 ✓）
    结构化壳（[Reply]/[CQ:at]/<at>）与媒体块不依赖名字表 ✓ 空表也成立 ✓
    """
    raw = content or ""
    # ① 以**媒体占位符**开头的（含描述被截断、没有闭合括号的 ✗）直接判为媒体 ✓
    #    注意**不包含**引用壳 ✓ 所以 `[Reply x] 你好呀` 不会被误杀 ✓
    if _MEDIA_HEAD_ANY.match(raw):
        return True
    # ★ 2026-09-19：结构化 at 壳先剥掉 ✓（**不依赖名单** ✓）
    #   ⇒ `[At 123]` 这种"只有 at"的消息才判得出来 ✓
    raw = _AT_SHELL.sub(" ", raw)
    text = _ENVELOPE_OPEN.sub(" ", raw)          # 先剥"行尾未闭合"的壳 ✓（截断残留 ✓）
    text = _ENVELOPE.sub(" ", text)
    text = _MEDIA_BLOCK.sub(" ", text)
    kept = []
    for part in text.split():
        if part.startswith("@") and len(part) > 1:
            # 削掉尾部标点再比对名字 ✓（`@小明！` 也是裸 at ✓；而 `@小明，你好` 削完
            # 仍不等于成员名 ✓ → 整条保留 ✓ 真话不会被吃掉 ✓）
            who = part[1:].rstrip("，。！？、,.!?~～:：;；\"'）)】]")
            if who.isdigit() or who in names:
                continue          # 已知的 at 壳 ✓ 丢掉
        # ★ 2026-09-18：**纯括号/标点的残渣不算内容** ✗
        #   例：完整壳被剥掉后只剩 `[]` / `()` ⇒ 也该判为"只有壳" ✓
        if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", part):
            continue
        kept.append(part)
    return not kept
