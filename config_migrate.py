"""Versioned one-shot configuration migrations.

Only keys that still hold the *previous default* are rewritten, so anything the
user customised is never touched. This keeps existing installs on the current
defaults (including prompt wording) without overriding personal choices.
"""

from .contracts import FACT_MERGE_PROMPT, RECORD_MERGE_PROMPT

CURRENT_VERSION = 8

# v2.18.9 之前的 fact_merge_prompt 默认文案 ✓
# 只用来把"从没改过措辞"的存量配置升到新文案 ✓ 用户自己改过的一律不碰 ✓
_OLD_FACT_MERGE_PROMPT = '输入是若干组相似事实（groups[]）：同组同主体，类别可能相同也可能不同；组内 facts 按时间从新到旧排列，facts[0] 最新；若附了 evidence，那是这些事实的来源原文，用它核对。\n为每一组输出一条结果，数量与顺序与输入完全一致。【必须给出动作，没有 keep】三种动作：\n- merge：确认是同一件事 → 以 facts[0] 为基准合并，把其余事实独有的人名、数字、日期、否定、条件、状态补进去；冲突以时间较晚者为准；不同对象要分别写明，不得丢弃独有信息。content 必须自包含，不写“同上”、不引用 ID、不写“根据记录”之类元话，不得编造。\n- relabel：确实是同一件事，但两边内容各自都成立、无需合并 → 只统一类别（给 category）。\n- drop：其中若干条是纯冗余或错误记录 → 保留 target_id，其余进 source_ids 被删除（可恢复）。\n跨类别时（组内 category 不一致）必须给 category，写明统一后的类别；合并前会先把整组统一到该类别。\n硬性字数：每条 content ≤ {content_max} 字，reason ≤ {reason_max} 字；超出即判定失败。\n只输出 JSON：{"groups":[{"target_id":"…","source_ids":["…"],"action":"merge|relabel|drop","category":"…","content":"…","reason":"…"}]}\ntarget_id 取要保留的那条 id；source_ids 至少一条，逐字复制。'

# v2.18.9 之后、2026-09-23 之前的 fact_merge_prompt 默认文案 ✓
# 只用来把"没改过措辞"的存量配置升到那一版 ✓ 用户自己改过的一律不碰 ✓
# （2026-09-23：合并提示词又要加"密度"一句 ⇒ 上一版目标值必须**固化**成字面量 ✗
#   否则 v5 会跟着最新文案一起变 ⇒ 老配置被一步跳到最新、v7 失去意义 ✓）
_V7_FACT_MERGE_PROMPT = '输入 groups[]：同组同主体，类别可能相同也可能不同；facts 按时间从新到旧排列，facts[0] 最新；若附了 evidence，那是这些事实的来源原文，用它核对。\n每组输出一条，数量与顺序与输入完全一致。【必须给出动作，没有 keep】三种动作：\n- merge：同一件事 → 以 facts[0] 为基准合并，把其余事实独有的人名、数字、日期、否定、条件、状态补进去；冲突以时间较晚者为准；不同对象分别写明，不得丢弃独有信息。合并结果不能比最长的一条明显更长，删掉重复表述。content 必须自包含，不写“同上”、不引用 ID、不写“根据记录”之类元话，不得编造。**合并后若原标签不再贴切，一并给 tags** ✗（不给就沿用原来的并集 ✓）\n**但“以时间较晚者为准”只适用于同一来源强度**：若附了 evidence，其中的 bot=1 表示那条原文是**助手自己说的** ✗ —— 助手更晚的转述**不能覆盖用户更早的原话** ✓ 冲突时一律以用户为准；没有证据就按原说法保留，别凭“更晚”擅自改结论。\n- relabel：同一件事，但两边内容各自都成立、无需合并 → 只统一类别（给 category）。\n- drop：若干条是纯冗余或错误 → 保留 target_id，其余进 source_ids 删除（可恢复）。\n跨类别（组内 category 不一致）必须给 category；合并前会先把整组统一到该类别。\n硬性字数：每条 content ≤ {content_max} 字，reason ≤ {reason_max} 字；超出即判定失败。\n只输出 JSON：{"groups":[{"target_id":"…","source_ids":["…"],"action":"merge|relabel|drop","category":"…","content":"…","reason":"…"}]}\ntarget_id 取要保留的那条 id；source_ids 至少一条，逐字复制。'

_V5_FACT_MERGE_PROMPT = '输入 groups[]：同组同主体，类别可能相同也可能不同；facts 按时间从新到旧排列，facts[0] 最新；若附了 evidence，那是这些事实的来源原文，用它核对。\n每组输出一条，数量与顺序与输入完全一致。【必须给出动作，没有 keep】三种动作：\n- merge：同一件事 → 以 facts[0] 为基准合并，把其余事实独有的人名、数字、日期、否定、条件、状态补进去；冲突以时间较晚者为准；不同对象分别写明，不得丢弃独有信息。content 必须自包含，不写“同上”、不引用 ID、不写“根据记录”之类元话，不得编造。**合并后若原标签不再贴切，一并给 tags** ✗（不给就沿用原来的并集 ✓）\n**但“以时间较晚者为准”只适用于同一来源强度**：若附了 evidence，其中的 bot=1 表示那条原文是**助手自己说的** ✗ —— 助手更晚的转述**不能覆盖用户更早的原话** ✓ 冲突时一律以用户为准；没有证据就按原说法保留，别凭“更晚”擅自改结论。\n- relabel：同一件事，但两边内容各自都成立、无需合并 → 只统一类别（给 category）。\n- drop：若干条是纯冗余或错误 → 保留 target_id，其余进 source_ids 删除（可恢复）。\n跨类别（组内 category 不一致）必须给 category；合并前会先把整组统一到该类别。\n硬性字数：每条 content ≤ {content_max} 字，reason ≤ {reason_max} 字；超出即判定失败。\n只输出 JSON：{"groups":[{"target_id":"…","source_ids":["…"],"action":"merge|relabel|drop","category":"…","content":"…","reason":"…"}]}\ntarget_id 取要保留的那条 id；source_ids 至少一条，逐字复制。'

# version -> [(key, previous default, new default), ...]
# v2.18.9：合并提示词补了"标签跟随内容更新" ✓ 上一版默认文案要再升一次 ✓
_V4_FACT_MERGE_PROMPT = '输入是若干组相似事实（groups[]）：同组同主体，类别可能相同也可能不同；组内 facts 按时间从新到旧排列，facts[0] 最新；若附了 evidence，那是这些事实的来源原文，用它核对。\n为每一组输出一条结果，数量与顺序与输入完全一致。【必须给出动作，没有 keep】三种动作：\n- merge：确认是同一件事 → 以 facts[0] 为基准合并，把其余事实独有的人名、数字、日期、否定、条件、状态补进去；冲突以时间较晚者为准；不同对象要分别写明，不得丢弃独有信息。content 必须自包含，不写“同上”、不引用 ID、不写“根据记录”之类元话，不得编造。\n**但“以时间较晚者为准”只适用于同一来源强度**：若附了 evidence，其中的 bot=1 表示那条原文是**助手自己说的** ✗ —— 助手更晚的转述**不能覆盖用户更早的原话** ✓ 冲突时一律以用户为准；没有证据就按原事实里的说法保留，不要凭“更晚”擅自改结论。\n- relabel：确实是同一件事，但两边内容各自都成立、无需合并 → 只统一类别（给 category）。\n- drop：其中若干条是纯冗余或错误记录 → 保留 target_id，其余进 source_ids 被删除（可恢复）。\n跨类别时（组内 category 不一致）必须给 category，写明统一后的类别；合并前会先把整组统一到该类别。\n硬性字数：每条 content ≤ {content_max} 字，reason ≤ {reason_max} 字；超出即判定失败。\n只输出 JSON：{"groups":[{"target_id":"…","source_ids":["…"],"action":"merge|relabel|drop","category":"…","content":"…","reason":"…"}]}\ntarget_id 取要保留的那条 id；source_ids 至少一条，逐字复制。'

# v2.18.73 之前的 record_merge_prompt 默认文案 ✓（同上：只升"没改过"的存量 ✓）
_V6_RECORD_MERGE_PROMPT = 'records 按时间从新到旧排列，records[0] 是最新的那条。\n只输出一个 action：merge（不允许 keep）。\n以 records[0] 为基准：保留它的内容与结论，再把其余记录里独有的人名、群名、数字、QQ号、日期、状态补充进去。\n冲突之处以时间较晚的说法为准，不保留已被推翻的旧结论。\n涉及不同主体时必须在同一条 content 里分别写明，不得丢弃任何主体或任何独有信息。\ncontent 必须自包含：不写“同上”，不引用其他记录 ID，不写元话，不复述重复内容。\n不得编造原文没有的信息。\n硬性字数：content ≤ {content_max} 字，reason ≤ {reason_max} 字；超出即判定失败。\n只输出 JSON：{"action":"merge","content":"…","reason":"…","source_ids":["…"]}\nsource_ids 至少两条，逐字复制 records[].id；禁止编造 ID。'

MIGRATIONS = {
    2: [
        ("audit_interval", 1800, 7200),
    ],
    3: [
        # v2.6.0 把「检索默认只搜常驻」的默认值翻成关闭（归档也参与召回，更全），
        # 但老配置里多半存着旧默认 true。只改写「仍等于旧默认」的那一个键，
        # 用户自己设过的一律不动。
        ("search_active_only", True, False),
    ],
    4: [
        # v2.18.9：合并提示词新增"来源强度优先于时间"（助手更晚的转述不能覆盖
        # 用户更早的原话 ✓）。存量配置里若还存着旧默认文案 → 升级 ✓
        # 若用户自己改过措辞 → 保持不动 ✓
        ("fact_merge_prompt", _OLD_FACT_MERGE_PROMPT, _V4_FACT_MERGE_PROMPT),
    ],
    5: [
        # v2.18.9：提示词加了"标签跟随内容更新" ✓ 再升一次 ✓
        ("fact_merge_prompt", _V4_FACT_MERGE_PROMPT, _V5_FACT_MERGE_PROMPT),
    ],
    6: [
        # v2.18.56：「档案轮换槽」独立开关改为**默认关** ✓
        # 存量配置里存的都是旧默认 True（框架灌的默认值，或用户自己开的都算），
        # 一律一次性改成 False ✓ —— 想用的人自己再打开 ✓ 之后迁移不再跑 ✓ 不会被动过 ✓
        ("rotate_archive_enabled", True, False),
    ],
    7: [
        # 2026-09-23（用户拍板）：下沉参数重新标定 —— 旧默认 12 变成 15 ✓
        ("fact_sink_threshold", 12, 15),
        # 同批：两个"合并"提示词都加"合并结果不能比最长的一条明显更长，删掉重复表述。" ✓
        ("fact_merge_prompt", _V5_FACT_MERGE_PROMPT, FACT_MERGE_PROMPT),
        ("record_merge_prompt", _V6_RECORD_MERGE_PROMPT, RECORD_MERGE_PROMPT),
    ],
    8: [
        # v2.20.0（2026-09-25）：把 JEV 那套「判 drop 前先自问三问」借进合并提示词 ✓
        #   目标：**不开 JEV 时，合并模型自己也能判 drop**（纯冗余直接进回收站 ✓）
        #   存量配置里若还是上一版默认文案 ⇒ 升级 ✓；用户自己改过措辞 ⇒ 一个字不动 ✓
        ("fact_merge_prompt", _V7_FACT_MERGE_PROMPT, FACT_MERGE_PROMPT),
    ],

}


def migrate(config):
    """Return ``(changed_keys, updated_config)``; pure function, no I/O."""
    config = dict(config or {})
    meta = dict(config.get("alife_meta") or {})
    try:
        version = int(meta.get("config_version") or 1)
    except (TypeError, ValueError):
        version = 1
    if version >= CURRENT_VERSION:
        return [], config
    settings = dict(config.get("alife") or {})
    changed = []
    for step in range(version + 1, CURRENT_VERSION + 1):
        for key, previous, current in MIGRATIONS.get(step, ()):
            if key in settings and settings[key] == previous:
                settings[key] = current
                changed.append(key)
    config["alife"] = settings
    meta["config_version"] = CURRENT_VERSION
    config["alife_meta"] = meta
    return changed, config


def prompt_defaults():
    """Exposed for tests: the shipped merge prompt templates."""
    return {"fact_merge_prompt": FACT_MERGE_PROMPT, "record_merge_prompt": RECORD_MERGE_PROMPT}
