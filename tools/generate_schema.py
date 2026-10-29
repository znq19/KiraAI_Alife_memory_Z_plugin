"""Generate the KiraAI configuration schema from the validated settings contract."""

import importlib.util
import json
import sys
from pathlib import Path

# 字段级提示语（前端占位/说明）——不写的话宿主会显示通用占位（如「使用默认向量模型」）易误导 ✗
_MODEL_HINTS = {
    "jev_model": "留空 = 该方式不启用（回退链仍可单独使用）。OpenAI 兼容中转用这一项；3.1.1+ 内置 TypeSafe 请用下一项。",
    "jev_decision_model": "留空 = 该方式不启用。与 OpenAI 兼容的「JEV 决策模型」二选一即可。",
    "jev_models": "按顺序点击 = 回退优先级（最多 10 个）。OpenAI 兼容中转用这一组；3.1.1+ 用内置 TypeSafe 时请用下面的决策链。",
    "jev_decision_models": "按顺序点击 = 回退优先级（最多 10 个）。只填这一条、其余 JEV 模型字段留空也能用（第 1 档自动当主档）。",
    "rerank_model": "留空 = 不重排（使用内置排序）。请选择在提供商里注册好的 Rerank 模型。",
    "embedding_model": "留空 = 不启用向量检索。请选择在提供商里注册好的向量模型。",
}

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("contracts", root / "contracts.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
schema = module.Settings.model_json_schema()
help_spec = importlib.util.spec_from_file_location(
    "setting_help", root / "setting_help.py"
)
help_module = importlib.util.module_from_spec(help_spec)
help_spec.loader.exec_module(help_module)
names = dict(
    zip(
        module.Settings.model_fields,
        [
            "启用记忆系统",
            "记录对话与感知",
            "旧历史播种",
            "注入最近原文",
            "持续上下文注入",
            "事实视图",
            "压缩分批方式",
            "每批压缩轮数",
            "窄查询自动扩词召回",
            "首层压缩阈值",
            "首层每批条数",
            "自动压缩概率",
            "最大压缩层级",
            "压缩与合并模型",
            "审计模型",
            "可选向量模型（启用后可能计费）",
            "启用向量检索（默认关闭）",
            "后台审计",
            "审计间隔（秒）",
            "每批审计事实数",
            "审计冷却（天）",
            "审计每日调用上限",
            "模型超时（秒）",
            "模型失败重试次数",
            "压缩带人设",
            "审计带人设",
            "注入形态",
            "后台并发数",
            "上下文字符预算",
            "估算 Token 警告线",
            "回忆提示词",
            "Bot可访问范围",
            "感知事实批量（×10）",
        "启用 JEV 决策层（可选，默认关闭）",
        "JEV 决策模型（OpenAI 兼容中转）",
        "JEV 决策模型（TypeSafe · 3.1.1+）",
        "JEV 接口地址（可留空）",
        "JEV API 密钥（可留空）",
        "JEV 模型名（可留空）",
        "JEV 超时（毫秒）",
        "JEV 采样比例（1=全部）",
        "JEV 用于召回筛选",
        "JEV 用于合并路由",
        "JEV 用于审计预筛",
        "JEV 用于重要度定级",
        "JEV 用于压缩前置筛选",
            "JEV 附加指令（可编辑）",
            "自我认知过滤总开关",
            "自我认知事实召回门槛",
            "覆盖判定扫描上限",
        "启用模型重排（可选，默认关闭）",
        "重排模型（Rerank）",
        "重排超时（毫秒）",
            "定时主动感知",
            "主动感知间隔（秒）",
            "主动感知随机偏移（秒）",
            "每轮最少会话数",
            "每轮最多会话数",
            "轮流挑选会话",
            "主动感知会话",
            "压缩补充要求",
            "自动安全迁移旧记忆",
            "迁移成功后互斥旧插件",
            "旧记忆迁移字符上限",
            "导入时的记忆年龄折算（天）",
            "每批压缩输入预算（字符）",
            "打开面板时播放载入动画",
            "载入动画重播冷却（秒）",
            "全局召回优先当前会话",
            "保存永久记忆前先查事实覆盖",
            "永久记忆定期整理",
            "记住后立即整理",
            "永久记忆条数上限",
            "永久记忆字符预算",
            "每次整理条数",
            "同一条整理间隔（天）",
            "允许 Bot 强制重提取单条事实",
            "Bot 强制重提取的间隔（分钟）",
            "跨类别重复阈值",
            "去重判定附原文证据",
            "轮换召回槽位（总开关）",
            "轮换槽位条数（事实槽）",
            "常驻下沉阈值",
            "轮换保留轮数",
            "轮换命中阈值",
            "轮换冷却轮数",
            "档案槽独立开关（不关事实槽）",
            "档案槽每批正文字数上限",
            "档案槽每批条数",
            "冷归档",
            "冷归档天数",
            "冷归档库文件路径",
            "冷归档自动执行",
            "每轮注入时间预算（毫秒）",
            "永久记忆跨会话去重阈值",
            "自动合并相似永久记忆",
            "检测到相似就强制合并",
            "永久记忆相似度阈值",
            "事实内容匹配门槛",
            "检索默认只搜常驻",
            "召回跳过表情/图片-only 消息",
            "归档转入冷归档天数",
            "每批压缩的原文上限（字符）",
            "陈旧记忆几天后开始消化",
            "会话闲置几小时后收尾",
            "单个压缩任务最多连压几批",
            "收尾压缩的冷却（分钟）",
            "写入时自动合并事实",
            "事实合并触发阈值",
            "事实合并字数（提示词）",
            "事实合并字数（硬上限）",
            "事实合并理由（提示词）",
            "事实合并理由（硬上限）",
            "事实合并每批簇数",
            "事实合并提示词",
            "跨会话合并（身份类）",
            "合并前不参与检索",
            "永久记忆合并字数（提示词）",
            "永久记忆合并字数（硬上限）",
            "永久记忆合并理由（提示词）",
            "永久记忆合并理由（硬上限）",
            "永久记忆合并提示词",
            "画像摘要条数",
            "相关人物召回",
            "相关人物最多几个",
            "主动召回精修方式",
            "注入事实条数上限",
            "注入预算模式",
            "JEV 保留线",
            "JEV 候选池倍数",
            "跨会话跳过折叠原文",
            "压缩/合并回退链（≤10，按序回退）",
            "审计回退链（≤10，按序回退）",
            "JEV 回退链（OpenAI 兼容中转，≤10）",
            "JEV 回退链（TypeSafe 决策模型，≤10）",
            "重排回退链（≤10，按序回退）",
        ],
    )
)
_missing = [k for k in schema["properties"] if k not in names]
_hidden = getattr(module, "HIDDEN_SETTINGS", frozenset())
fields = {}
for key, p in schema["properties"].items():
    if key in _hidden:
        continue
    kind = {
        "boolean": "switch",
        "number": "float",
        "integer": "integer",
        "array": "list",
        "string": "string",
    }[p["type"]]
    field = {
        "type": kind,
        # v2.18.19：**缺标签时不再崩** ✗ 而是用键名兜底并**报警** ✓
        # 原来 `names[key]` 直接 KeyError ✗ ⇒ 生成器整个跑不通 ✓
        # ⇒ 任何新增配置都只能手改 schema.json ✓（这正是之前 4 个新配置漏进去的根因 ✓）
        "name": names.get(key) or ("⚠️缺标签:" + key),
        "default": module.Settings().model_dump()[key],
        "description": help_module.HELP[key],
    }
    for constraint in ("minimum", "maximum"):
        if constraint in p:
            field[constraint] = p[constraint]
    if p["type"] == "array":
        field["item_type"] = "string"
    if "enum" in p:
        field["options"] = p["enum"]
    # ★ 2026-10-10：回退链 = 官方多选（3.0 原生渲染 ✓ 2.x 退化为文本 ✗ 无妨 ✓）
    if key.endswith("_models"):
        field["type"] = "multi_select"
        field["source"] = "model"
        field["model_type"] = {
            "jev_decision_models": "decision",
            "rerank_models": "rerank",
        }.get(key, "llm")
        field["max_items"] = 10
        field["hint"] = "按顺序点击 = 回退优先级（最多 10 个）。某个模型失败会自动换下一个；全部失败才回落上面的单模型字段。面板里可上下调序。"
    if key.endswith("_model"):
        field["type"] = "model_select"
        field["model_type"] = {
            "embedding_model": "embedding",
            "rerank_model": "rerank",
            "jev_decision_model": "decision",
        }.get(key, "llm")
    if key in _MODEL_HINTS:
        field["hint"] = _MODEL_HINTS[key]
    fields[key] = field
(root / "schema.json").write_text(
    json.dumps(
        {
            "alife": {
                "type": "section",
                "name": "Alife 完整记忆移植",
                "collapsed": False,
                "fields": fields,
            }
        },
        ensure_ascii=False,
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)

if _missing:
    print("⚠️ 以下配置缺中文标签（已用占位符 ✓ 请补进 names 表）：")
    for _k in _missing:
        print("   -", _k)
