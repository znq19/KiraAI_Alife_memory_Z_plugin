"""Local relevance and safe factual projections; no embedding provider required."""

import re
import json
import time
from collections import OrderedDict

# Our own read tools return a distinctive JSON envelope. Those results are
# self-recall echoes: storing them as memories only bloats the next context.
_MEMORY_PAYLOAD_MARKERS = (
    '"archives_in_context"',
    '"children_total"',
    '"next_page"',
    '"subjects"',
    '"entities"',
    '"omitted_ids"',
    '"related_archives"',
)
TOOL_RESULT_PREFIX = "工具感知结果："


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
    result = {
        k: row[k]
        for k in (
            "id",
            "sid",
            "role",
            "level",
            "start",
            "end",
            "summary",
            "users",
            "speaker",
            "revision",
            "permanent",
        )
    }
    children = row.get("children", [])
    result.update(
        children=children[child_offset : child_offset + child_count],
        children_total=len(children),
        child_offset=child_offset,
        next_child_offset=child_offset + child_count
        if child_offset + child_count < len(children)
        else None,
    )
    result["parents"] = row.get("parents", [])
    if include_content:
        result["versions"] = row.get("versions", [])
        result["legacy_sources"] = row.get("legacy_sources", [])
    result["content_included"] = not children or include_content
    if result["content_included"]:
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


def trim_nested(text, reply_chars=40, desc_chars=100):
    """压缩嵌套的长文本：引用里的原文、表情/图片的视觉描述。

    只截断「嵌套段落」，正文摘要不动；括号用配对扫描，不会被内容里的 ``]`` 提前截断。
    日志实测：表情包的视觉描述平均 276 字符，是摘要里最大的单块开销。
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
            inner = text[reply.end() : end]
            out.append(f"[Reply {reply.group(1)}: {_clip(inner, reply_chars)}]")
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


def model_text(text, keep=(), reply_chars=40, desc_chars=100):
    """统一入口：剥思考块 + 剥包裹 + 压空白 + 截断嵌套长描述（只影响模型看到的样子）。

    这里**比落库多剥一层思考块**：落库要保原文（用户可能真的引用了 Bot 的思考块），
    但发给模型的东西不该带内部推理——存量清理万一没跑到，模型也不会被污染。
    """
    return trim_nested(clean_text(strip_reasoning(text), keep), reply_chars, desc_chars)


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


_REPLY_HEAD = re.compile(r"\[Reply ID:\s*(\d+)\s*content:\s*")
_MEDIA_HEAD = re.compile(r"\[(Sticker|Image)\s+")


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
    if ts <= 0:
        return ""
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
}
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

CATEGORY_LEGEND = "事实短码：" + " ".join(
    f"{code}={name}" for name, code in CATEGORY_CODES.items()
)


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
    tokens = query_tokens(query)
    lowered = squeeze(text).casefold()
    return sum(len(t) * (t in lowered) for t in tokens)


def short_day(ts):
    """给模型看的日期短码：今年不带年，跨年才带（省 token，且一眼能读）。"""
    try:
        stamp = time.localtime(float(ts))
    except (TypeError, ValueError, OSError):
        return ""
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


def bot_facts(facts, current_sid="", short=None):
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
            if fact.get("src_user"):
                item["by"] = fact["src_user"]
        source = fact.get("src") or (fact.get("sources") or [None])[-1]
        if source:
            # 短码映射里没有的（例如 sources 兜底值）就用原值，绝不输出 null
            item["src"] = (short(source) or source) if short else source
        created = fact.get("created")
        # 事件时间（这条事实讲的事发生在什么时候）优先；老数据没有 event_at 时退回 created。
        event_at = fact.get("event_at") or created
        event_end = fact.get("event_end") or event_at
        if isinstance(event_at, (int, float)) and event_at > 0:
            item["t"] = short_day(event_at)
            # 跨天的事（多条时间点合并进来的）给个区间，不然"塌成一点" ✗
            if isinstance(event_end, (int, float)) and event_end - event_at > 86400:
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
        view.append(item)
    return view


# ── v2.17.0：事实「按主体分组」视图（默认）──────────────────────────────
# 旧的扁平视图 bot_facts() 一律不动 ✓ 它是回滚路径（view="flat" 时逐字节一致 ✓）
# 组内位置固定：[类别, 内容, 重要性?, 关系?, 时间?, 谁说的?] ✓ 尾部为空就省略 ✗
# 注意：只允许从**尾部**省 —— 中间位有值、前一位没有时，前一位补 "" 占位 ✓

FACT_VIEW_GROUPED = "grouped"
FACT_VIEW_FLAT = "flat"


def _grouped_row(fact, codes, current_sid):
    """一条事实 → 位置化行（尾部省略）。"""
    category = fact.get("category") or ""
    row = [CATEGORY_CODES.get(category, category), str(fact.get("content") or "")]

    importance = fact.get("importance")
    row.append(importance if importance not in (None, "", 5) else "")

    rels = []
    for rel in fact.get("verified_relations") or fact.get("relations") or []:
        if not isinstance(rel, dict):
            continue
        head = codes.get(str(rel.get("subject") or ""), str(rel.get("subject") or ""))
        tail = codes.get(str(rel.get("object") or ""), str(rel.get("object") or ""))
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
    row.append(who)

    while row and row[-1] == "":
        row.pop()
    return row


def bot_facts_grouped(facts, current_sid="", codes=None):
    """按主体分组渲染事实（v2.17.0 默认视图）。

    形如::

        {"n1": [["pf", "周武是用户的大学室友", 7, "n1>朋友>n2", "08-20"]],
         "n2": [["ev", "昨天和周武一起吃饭", 6, "", "09-11"]]}

    - 组键 = **主体短码**（真实 id 见 names 表 ✓）→ 主体只出现一次 ✓
    - 组间/组内都按类别优先级排（画像/约定/偏好在前 ✓）
    - 关系用短码三元组 "主体>关系>客体" ✓ 多条用 ";" 连接 ✓
    """
    codes = codes or {}
    groups = {}
    ranks = {}
    for fact in facts or []:
        subject = str(fact.get("subject") or "")
        if not subject:
            continue
        key = codes.get(subject, subject)
        groups.setdefault(key, []).append((CATEGORY_RANK.get(fact.get("category") or "", 99), _grouped_row(fact, codes, current_sid)))
        ranks[key] = min(ranks.get(key, 99), CATEGORY_RANK.get(fact.get("category") or "", 99))
    out = {}
    for key in sorted(groups, key=lambda k: (ranks.get(k, 99), k)):
        rows = [row for _, row in sorted(groups[key], key=lambda pair: pair[0])]
        out[key] = rows
    return out


def pack_facts(facts, current_sid="", short=None, view=FACT_VIEW_GROUPED, codes=None):
    """事实渲染入口：grouped=分组视图（默认 ✓）/ flat=旧的扁平视图（逐字节不变 ✓）。"""
    if view == FACT_VIEW_FLAT:
        return bot_facts(facts, current_sid, short=short)
    return bot_facts_grouped(facts, current_sid, codes=codes)


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


FACT_GROUP_LEGEND = (
    "事实按主体分组：facts 的键是主体短码，组内每行 [类别, 内容, 重要性?, 关系?, 时间?, 谁说的?]，"
    "尾部省略；关系写作 主体>关系>客体（多条用 ; 分隔）；短码与名字见 names（短码→[真实ID, 名字]）"
)


CATEGORY_LEGEND = CATEGORY_LEGEND + "\n" + FACT_GROUP_LEGEND


def archives_flat(value):
    """档案区取值：兼容「扁平列表」与「{permanent, recent} 分组」两种形态 ✓"""
    if isinstance(value, dict):
        return list(value.get("permanent") or []) + list(value.get("recent") or [])
    return list(value or [])
