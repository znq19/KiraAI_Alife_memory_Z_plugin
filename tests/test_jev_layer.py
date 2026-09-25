"""JEV 决策层与模型重排：路由组合、失败即回退、留痕安全、全关零影响。

对应《方案 v4》：每个阈值都来自真机实测（2026-09-24，约 150 条中英标注样本）。
本文件**不需要网络**：所有后端都用桩件替换，只验证契约与组合逻辑。
"""

import asyncio
import importlib
import json
import logging
import sys
import tempfile
import types
from pathlib import Path

import unittest

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("alife_jev_test")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_jev_test", package)

try:  # 真实框架可用则直接用；否则给 logging 打个最小桩
    import core.logging_manager  # noqa: F401
except Exception:  # pragma: no cover
    corepkg = types.ModuleType("core")
    corepkg.__path__ = []
    logmod = types.ModuleType("core.logging_manager")
    logmod.get_logger = lambda *a, **k: logging.getLogger("jev-test")
    sys.modules.setdefault("core", corepkg)
    sys.modules["core.logging_manager"] = logmod

md = importlib.import_module("alife_jev_test.mdecide")
rx = importlib.import_module("alife_jev_test.rerankx")

run = asyncio.run


def S(**kw):
    """构造一个假的 Settings（只有属性读取）。"""

    class _S:
        pass

    obj = _S()
    for k, v in kw.items():
        setattr(obj, k, v)
    return obj


class RouteCase(unittest.TestCase):
    """合并三岔路由：**同一件事必决（merge/drop），不是同一件事绝不动。**"""

    def test_measured_cases(self):
        cases = [
            # (same, new, dilute, 期望)  —— 全部来自真机实测
            (0.91, 0.26, 0.26, "drop"),    # 纯重复「用户不能吃花生」
            (0.89, 0.97, 0.46, "merge"),   # 同事实 + 重伤信息
            (0.93, 0.52, 0.43, "drop"),    # 同事实但无实质新信息
            (0.80, 0.88, 0.31, "merge"),   # 同事实 + 应用细节
            (0.87, 0.98, 0.58, "merge"),   # 同事实 + 严重程度/备药（稀释贴边，实测 0.58）
            (0.05, 0.95, 0.77, "keep"),    # 不同事实（腰果过敏）
            (0.30, 0.88, 0.84, "keep"),    # 冲突/一次性 ⇒ 交给审计
        ]
        for same, new, dilute, want in cases:
            self.assertEqual(md.route_merge(same, new, dilute), want,
                             f"same={same} new={new} dilute={dilute}")

    def test_never_keep_a_true_duplicate(self):
        """★ 用户的核心诉求：真重复（same 高）必须落到 merge 或 drop，**不允许 keep**。"""
        for new in (0.0, 0.1, 0.3, 0.59, 0.6, 0.9, 1.0):
            self.assertIn(md.route_merge(0.95, new, 0.1), ("merge", "drop"))

    def test_dilution_blocks_merge(self):
        """会稀释就不许并进正文（保护高重要度事实）——既不合并也不删，保持原样。"""
        self.assertEqual(md.route_merge(0.9, 0.95, 0.8), "keep")

    def test_missing_signal_is_keep(self):
        for same, new in ((None, 0.9), (0.9, None), (None, None)):
            self.assertEqual(md.route_merge(same, new, 0.1), "keep")

    def test_importance_mapping_only_lowers(self):
        """★ 只压不抬：核心/重要/一般 不动（None），只有低价值档才下调。"""
        self.assertIsNone(md.importance_of("核心"))
        self.assertIsNone(md.importance_of("重要"))
        self.assertIsNone(md.importance_of("一般"))
        self.assertIsNone(md.importance_of(None))       # 未知 ⇒ 不动最安全
        self.assertEqual(md.importance_of("次要"), 3)    # 约半月自然沉
        self.assertEqual(md.importance_of("无价值"), 1)  # 立即沉（2+10 < 15）
        # 与下沉公式一致：≥8 才会触发"永不沉"，而本机制永不产生 ≥8 的值
        self.assertLess(max(md.IMPORTANCE_LEVELS.values()), 8)

    def test_candidates_are_self_contained(self):
        qs = md.build_merge_route("主事实内容", [("c0", "候选甲内容"), ("c1", "候选乙内容")])
        for key, text in (("same_c0", "候选甲内容"), ("new_c0", "候选甲内容"),
                          ("dilute_c0", "候选甲内容"), ("same_c1", "候选乙内容")):
            self.assertIn(text, qs[key]["instructions"], "候选必须写进问题里（非生成模型没有指代消解）")
            self.assertIn("主事实内容", qs[key]["instructions"])

    def test_choice_criteria_are_flat(self):
        q = md.build_importance([("i0", "某条事实")])["imp_i0"]
        self.assertEqual(q["type"], "choice")
        self.assertTrue(all(isinstance(v, str) for v in q["criteria"].values()),
                        "choice 的 criteria 必须扁平 {选项: 描述}，嵌套会直接报错")

    def test_recall_and_audit_keys(self):
        hits = [("a", "记忆一"), ("b", "记忆二")]
        qs = md.build_recall_filter("上下文", hits)
        self.assertEqual(sorted(qs), ["hit_a", "hit_b"])
        pairs = [("p1_2", "事实甲", "事实乙")]
        self.assertEqual(list(md.build_audit_pairs(pairs)), ["bad_p1_2"])

    def test_parse_helpers(self):
        ans = {"a": {"type": "noul", "noul": 0.7}, "b": {"type": "choice", "choice": "核心",
                                                         "confidence": 0.9}}
        self.assertAlmostEqual(md.parse_noul(ans, "a"), 0.7)
        self.assertEqual(md.parse_choice(ans, "b"), ("核心", 0.9))
        self.assertIsNone(md.parse_noul(ans, "missing"))


class ConfigCase(unittest.TestCase):
    def test_endpoint_normalisation(self):
        self.assertEqual(md.endpoint_of("https://api.typesafe.ai"),
                         "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(md.endpoint_of("https://api.typesafe.ai/"),
                         "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(md.endpoint_of("https://api.typesafe.ai/v1"),
                         "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(md.endpoint_of("https://relay.example/v1"),
                         "https://relay.example/v1/systemone")

    def test_split_uuid(self):
        self.assertEqual(md.split_uuid("prov:model"), ("prov", "model"))
        self.assertEqual(md.split_uuid("prov:model:extra"), ("prov", "model:extra"))
        self.assertEqual(md.split_uuid(""), ("", ""))

    def test_explicit_fields_win(self):
        cfg = md.resolve_config(S(jev_enabled=True, jev_base_url="https://x/v1",
                                  jev_api_key="k", jev_model_name="m"))
        self.assertTrue(cfg.ready)
        self.assertEqual(cfg.base_url, "https://x/v1")
        self.assertEqual(cfg.model, "m")

    def test_falls_back_to_selected_model(self):
        class Info:
            provider_config = {"base_url": "https://api.example/v1", "api_key": "sk-x"}

        class Mgr:
            def get_model_info(self, pid, mid, model_type=None):
                self.seen = (pid, mid)
                return Info()

        mgr = Mgr()
        cfg = md.resolve_config(S(jev_enabled=True, jev_model="prov:jev-latest"), mgr)
        self.assertEqual(mgr.seen, ("prov", "jev-latest"))
        self.assertEqual(cfg.base_url, "https://api.example/v1")
        self.assertEqual(cfg.api_key, "sk-x")
        self.assertEqual(cfg.model, "jev-latest")
        self.assertTrue(cfg.ready)

    def test_env_reference_and_no_key(self):
        import os

        os.environ["JEV_TEST_KEY"] = "sk-from-env"
        cfg = md.resolve_config(S(jev_enabled=True, jev_api_key="$$JEV_TEST_KEY"))
        self.assertEqual(cfg.api_key, "sk-from-env")
        off = md.resolve_config(S(jev_enabled=False, jev_api_key=""))
        self.assertFalse(off.ready)

    def test_provider_lookup_failure_is_harmless(self):
        class Bad:
            def get_model_info(self, *a, **k):
                raise RuntimeError("boom")

        cfg = md.resolve_config(S(jev_enabled=True, jev_model="p:m"), Bad())
        self.assertFalse(cfg.ready)          # 解析失败 ⇒ 不可用，绝不抛


class ClientCase(unittest.TestCase):
    def test_oct_normalises_answers(self):
        client = md.JevClient("https://x", "k", "m", timeout=1.0)
        client._post_sync = lambda payload, timeout: {          # type: ignore[assignment]
            "answers": {"q1": {"type": "noul", "noul": 0.8}}, "usage": {"input_tokens": 311}}
        data = run(client.call("state", {"q1": {}}))
        self.assertEqual(data["answers"]["q1"]["noul"], 0.8)
        self.assertEqual(client.tokens, 311)

    def test_failures_never_raise_and_break_the_circuit(self):
        client = md.JevClient("https://x", "k", "m", timeout=0.5)

        def boom(payload, timeout):
            raise OSError("network down")

        client._post_sync = boom                                   # type: ignore[assignment]
        for _ in range(3):
            self.assertIsNone(run(client.call("s", {"q": {}})))
        self.assertFalse(client.ready, "连续失败 3 次后应熔断（冷却期直接返回 None）")

    def test_missing_key_is_not_ready(self):
        self.assertFalse(md.JevClient("https://x", "", "m").ready)

    def test_bad_payload_shape(self):
        client = md.JevClient("https://x", "k", "m")
        client._post_sync = lambda p, t: {"error": "nope"}          # type: ignore[assignment]
        self.assertIsNone(run(client.call("s", {"q": {}})))


class DecisionsCase(unittest.TestCase):
    def test_disabled_returns_none_everywhere(self):
        d = md.Decisions(S(jev_enabled=False))
        self.assertFalse(d.ready)
        self.assertIsNone(run(d.recall_filter("ctx", [("a", "x")])))
        self.assertIsNone(run(d.merge_route("p", [("c", "x")])))
        self.assertIsNone(run(d.importance([("i", "x")])))
        self.assertIsNone(run(d.recall_trigger("你好")))
        self.assertIsNone(run(d.audit_prescreen([("p", "a", "b")])))
        self.assertEqual(d.calls, 0)

    def test_no_key_but_enabled_still_safe(self):
        d = md.Decisions(S(jev_enabled=True, jev_api_key=""))
        self.assertIsNone(run(d.merge_route("p", [("c", "x")])))
        self.assertEqual(d.calls, 0)

    def test_partial_answers_mean_abstain(self):
        d = md.Decisions(S(jev_enabled=True, jev_api_key="k"))
        d._client = md.JevClient("https://x", "k", "m")
        d._client._post_sync = lambda p, t: {                       # type: ignore[assignment]
            "answers": {"hit_a": {"noul": 0.9}}, "usage": {"input_tokens": 10}}
        self.assertIsNone(run(d.recall_filter("ctx", [("a", "x"), ("b", "y")])),
                          "有一条没解析出来就整体弃权，不许半套结果")

    def test_merge_route_end_to_end(self):
        d = md.Decisions(S(jev_enabled=True, jev_api_key="k"))
        d._client = md.JevClient("https://x", "k", "m")
        d._client._post_sync = lambda p, t: {                       # type: ignore[assignment]
            "answers": {
                "same_c0": {"noul": 0.91}, "new_c0": {"noul": 0.20}, "dilute_c0": {"noul": 0.3},
                "same_c1": {"noul": 0.88}, "new_c1": {"noul": 0.90}, "dilute_c1": {"noul": 0.3},
            }, "usage": {"input_tokens": 100}}
        route = run(d.merge_route("主事实", [("c0", "纯重复"), ("c1", "有新信息")]))
        self.assertEqual(route, {"c0": "drop", "c1": "merge"})

    def test_importance_gating_by_confidence(self):
        d = md.Decisions(S(jev_enabled=True, jev_api_key="k"))
        d._client = md.JevClient("https://x", "k", "m")
        d._client._post_sync = lambda p, t: {                       # type: ignore[assignment]
            "answers": {"imp_a": {"choice": "核心", "confidence": 0.9},
                        "imp_c": {"choice": "一般", "confidence": 0.9},
                        "imp_b": {"choice": "无价值", "confidence": 0.9},
                        "imp_d": {"choice": "无价值", "confidence": 0.1}},
            "usage": {"input_tokens": 10}}
        out = run(d.importance([("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")]))
        self.assertEqual(out, {"b": 1},
                         "只压不抬：核心/一般 不写回；无价值=1；置信 0.1 不足 ⇒ 跳过")


class MergeThresholdCase(unittest.TestCase):
    """★ 用户实测反馈：换词重复（不吃香菜 / 讨厌香菜）曾被阈值误杀 ⇒ keep ✗（既不合并也不丢）。

    真机实测（2026-09-25，放宽判据 + 阈值 0.5 后）：
      换词重复 same=0.68 new=0.77 ⇒ merge ✓
      纯重复   same=0.94 new=0.11 ⇒ drop（回收站）✓
      真不同事 same≤0.12           ⇒ keep ✓（不误合）
    """

    def test_paraphrase_duplicate_merges(self):
        self.assertEqual(md.route_merge(0.68, 0.77, 0.58), "merge",
                         "换词重复必须能合并（曾经的回归 ✗）")

    def test_pure_repeat_goes_to_recycle(self):
        self.assertEqual(md.route_merge(0.94, 0.11, 0.35), "drop")

    def test_clearly_different_kept(self):
        for same in (0.05, 0.07, 0.12):
            self.assertEqual(md.route_merge(same, 0.85, 0.87), "keep",
                             "明确不同事 ⇒ 不动（不误合、不误删）")
        self.assertEqual(md.SAME_HIGH, 0.50, "阈值 0.5 是有实测依据的，别随手改")


class MixedRefineCase(unittest.TestCase):
    """★ 一次调用同时给「事实 + 档案」两组候选打分（省一次往返）。

    事实通道此前完全没经过 JEV ✗，而扩池又把事实池放大 3 倍
    ⇒ 必须能在同一次调用里把两组一起收回来，否则注入 token 会变多 ✗
    """

    def test_mixed_candidate_call_returns_both_groups(self):
        calls = {"n": 0}

        def fake_post(payload, timeout):
            calls["n"] += 1
            qs = payload.get("questions") or {}
            ans = {}
            for k in qs:                      # 事实高分、档案低分（够分开即可）
                ans[k] = {"type": "noul", "noul": 0.9 if k.startswith("f") else 0.2}
            return {"answers": ans, "usage": {"input_tokens": 10}}

        c = md.JevClient("https://x.invalid", "k", "jev-latest")
        c._post_sync = fake_post                # type: ignore[assignment]
        d = md.Decisions(md.JevConfig(enabled=True, base_url="https://x.invalid",
                                      api_key="k", model="jev-latest"), None, None)
        d._client = c
        items = [("f%d" % i, "事实%d" % i) for i in range(4)] + \
                [("r%d" % i, "档案%d" % i) for i in range(3)]
        out = run(d.recall_filter("看看这些", items, want_trigger=True))
        self.assertEqual(calls["n"], 1, "两组候选必须**一次调用**完成 ✗")
        keys = [k for k, _s in (out or [])]
        self.assertTrue(any(k.startswith("f") for k in keys), "事实组必须被打分")
        self.assertTrue(any(k.startswith("r") for k in keys), "档案组必须被打分")
        self.assertGreaterEqual(min(s for _k, s in out), 0.0)

class ProviderCompatCase(unittest.TestCase):
    """★ 任意提供商类型下注册的 JEV 都要能连上（用户要求）。"""

    def _resolve(self, provider_config):
        class Mgr:
            def get_model_info(self, pid, mid):
                class Info:
                    pass

                info = Info()
                info.provider_config = provider_config
                return info

        class S:
            jev_enabled = True
            jev_model = "someprovider:jev-latest"
            jev_base_url = ""
            jev_api_key = ""
            jev_model_name = ""
            jev_timeout_ms = 4000
            jev_sample = 1.0

        return md.resolve_config(S(), Mgr())

    def test_endpoint_normalisation(self):
        cases = {
            "https://api.typesafe.ai": "https://api.typesafe.ai/v1/systemone",
            "https://api.typesafe.ai/": "https://api.typesafe.ai/v1/systemone",
            "https://api.typesafe.ai/v1": "https://api.typesafe.ai/v1/systemone",
            "https://x/v1/chat/completions": "https://x/v1/systemone",
            "https://x/v1/systemone": "https://x/v1/systemone",
            "https://x/openai/v1/": "https://x/openai/v1/systemone",
        }
        for raw, want in cases.items():
            self.assertEqual(md.endpoint_of(raw), want, raw)

    def test_config_key_variants(self):
        """不同提供商/第三方插件的键名差异都要认得。"""
        for cfg in (
            {"base_url": "https://a.example", "api_key": "k1"},          # openai 系
            {"api_base": "https://b.example", "apikey": "k2"},           # 别名
            {"host": "https://c.example", "token": "k3"},                # ollama 风
            {"endpoint": "https://d.example", "key": "k4"},              # azure 风
            {"openai": {"base_url": "https://e.example", "api_key": "k5"}},  # 嵌套
        ):
            c = self._resolve(cfg)
            self.assertTrue(c.base_url.startswith("https://"), cfg)
            self.assertTrue(c.api_key.startswith("k"), cfg)
            self.assertEqual(c.model, "jev-latest")

    def test_client_uses_normalised_endpoint(self):
        """★ 实测过的坑：OpenAI 型提供商的 base_url 带 /v1，直接拼会变成 /v1/v1 ✗"""
        self.assertEqual(
            md.JevClient("https://api.x.com/v1", "k", "jev-latest").endpoint,
            "https://api.x.com/v1/systemone",
        )
        self.assertEqual(
            md.JevClient("https://api.x.com", "k", "jev-latest").endpoint,
            "https://api.x.com/v1/systemone",
        )

    def test_missing_key_falls_back_to_default_endpoint(self):
        c = self._resolve({})
        self.assertEqual(c.base_url, md.DEFAULT_BASE_URL)   # 官方默认，不炸
        self.assertFalse(c.ready, "没 key ⇒ 视为不可用（不影响其它功能）")


class PayloadShapeCase(unittest.TestCase):
    """/v1/systemone 的请求体形状（实测约束，别改坏）。"""

    def test_payload_never_includes_stream(self):
        """★ 实测：body 里带 "stream" ⇒ HTTP 400 ✗（systemone 不接受该字段）。

        而 `Accept: text/event-stream` 头是无害的（实测 200）。
        所以请求体必须**只有** model / state / questions 三个键。
        """
        captured = {}
        c = md.JevClient("https://example.invalid", "k", "jev-latest")

        def fake(payload, timeout):
            captured.update(payload)
            return {"answers": {}, "usage": {}}

        c._post_sync = fake                                   # type: ignore[assignment]
        run(c.call("state", {"q": {"type": "noul", "instructions": "x",
                                  "criteria": {"true": "a", "false": "b"}}}))
        self.assertEqual(set(captured), {"model", "state", "questions"})
        self.assertNotIn("stream", captured, "带了 stream 字段会 400 ✗")


class DecisionLogCase(unittest.TestCase):
    def test_writes_jsonl_and_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = md.DecisionLog(Path(tmp) / "sub" / "s.jsonl")
            log.write("recall", {"sid": "s1", "after": ["a"]})
            lines = (Path(tmp) / "sub" / "s.jsonl").read_text(encoding="utf-8").strip().split("\n")
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["kind"], "recall")

    def test_bad_path_is_swallowed(self):
        log = md.DecisionLog(Path("/proc/definitely/not/writable.jsonl"))
        for _ in range(5):
            log.write("x", {"a": 1})       # 不许抛
        self.assertGreaterEqual(log.errors, 1)


class _Result:
    def __init__(self, index, score):
        self.index, self.score, self.text = index, score, ""


class _Client:
    def __init__(self, results=None, exc=None, delay=0.0):
        self._results, self._exc, self._delay = results, exc, delay

    async def rerank(self, query, documents, top_n=None, **kw):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc:
            raise self._exc
        return self._results


class _Mgr:
    def __init__(self, client=None, exc=None):
        self._client, self._exc = client, exc

    def get_model_client(self, pid, mid, model_type=None):
        if self._exc:
            raise self._exc
        return self._client


class RerankCase(unittest.TestCase):
    ITEMS = [("a", "甲"), ("b", "乙"), ("c", "丙")]

    def test_disabled_without_config(self):
        r = rx.Reranker("", None, None)
        self.assertFalse(r.ready)
        self.assertIsNone(run(r.rank("q", self.ITEMS)))

    def test_orders_by_score(self):
        client = _Client([_Result(2, 0.2), _Result(0, 0.9), _Result(1, 0.5)])
        r = rx.Reranker("p:m", _Mgr(client), None)
        self.assertEqual(run(r.rank("q", self.ITEMS)), ["a", "b", "c"])

    def test_backend_error_falls_back(self):
        r = rx.Reranker("p:m", _Mgr(exc=RuntimeError("no provider")), None)
        self.assertIsNone(run(r.rank("q", self.ITEMS)))

    def test_client_error_falls_back(self):
        r = rx.Reranker("p:m", _Mgr(_Client(exc=RuntimeError("500"))), None)
        self.assertIsNone(run(r.rank("q", self.ITEMS)))

    def test_garbage_results_fall_back(self):
        class Bad:
            async def rerank(self, q, d, top_n=None, **kw):
                return ["not-a-result"]

        self.assertIsNone(run(rx.Reranker("p:m", _Mgr(Bad()), None).rank("q", self.ITEMS)))

    def test_timeout_is_bounded(self):
        r = rx.Reranker("p:m", _Mgr(_Client([_Result(0, 1.0)], delay=2.0)), None, timeout=0.3)
        self.assertIsNone(run(r.rank("q", self.ITEMS)), "超时 ⇒ 保持原顺序（绝不阻塞）")

    def test_index_out_of_range_is_ignored(self):
        client = _Client([_Result(99, 0.9), _Result(1, 0.5)])
        r = rx.Reranker("p:m", _Mgr(client), None)
        self.assertEqual(run(r.rank("q", self.ITEMS)), ["b"])

    def test_jev_backend_used_when_no_provider(self):
        class D:
            ready = True

            def __init__(self):
                self.called = False

            async def recall_filter(self, context, hits, timeout=None):
                self.called = True
                return [("c", 0.9), ("a", 0.4)]

        d = D()
        r = rx.Reranker("", None, d)
        self.assertTrue(r.ready)
        self.assertEqual(run(r.rank("q", self.ITEMS)), ["c", "a"])
        self.assertTrue(d.called)


class OffParityCase(unittest.TestCase):
    """★ 硬约束：默认配置下不动任何东西（不调用、不排序、不抛异常）。"""

    def test_defaults_are_off(self):
        cfg = md.resolve_config(S())            # 等价于「用户什么都没配」
        self.assertFalse(cfg.enabled)
        self.assertFalse(cfg.ready)

    def test_ready_predicate_matrix(self):
        rows = [
            (dict(jev_enabled=False, jev_api_key="k"), False),
            (dict(jev_enabled=True, jev_api_key=""), False),
            (dict(jev_enabled=True, jev_api_key="k"), True),
        ]
        for kw, want in rows:
            self.assertEqual(md.resolve_config(S(**kw)).ready, want, str(kw))


if __name__ == "__main__":
    unittest.main(verbosity=1)


class AuxRebuildCase(unittest.TestCase):
    """★ 回归守护：配置开启后**重建**必须能拿到就绪的决策层。

    用户实测的问题：面板里开了配置，但 self.decisions 还是开启前构建的（ready=False）
    ⇒ 所有 JEV 入口静默 return，后台一行日志都没有 ✗
    修法是"配置变化后重建"（_build_aux / _ensure_aux）。这里守住机制本身：
    同样的构造代码，配置开了就必须 ready；关了就必须 not ready（且不抛异常）。
    """

    class _S:
        def __init__(self, **kw):
            base = dict(jev_enabled=False, jev_model="", jev_base_url="", jev_api_key="",
                        jev_model_name="", jev_timeout_ms=5000, jev_sample=1.0)
            base.update(kw)
            for k, v in base.items():
                setattr(self, k, v)

    def test_rebuild_after_enabling_config(self):
        off = md.Decisions(self._S(), None, None)
        self.assertFalse(off.ready, "未启用 ⇒ 未就绪")
        on = md.Decisions(self._S(jev_enabled=True, jev_api_key="k",
                                  jev_model_name="jev-latest"), None, None)
        self.assertTrue(on.ready, "启用且密钥解析成功 ⇒ 必须就绪（否则就是静默失效 ✗）")
        self.assertTrue(on.cfg.base_url.startswith("http"))

    def test_ready_requires_key(self):
        """开了但密钥没解析到 ⇒ 未就绪（此时应打 warning 提示，而不是静默 ✗）。"""
        d = md.Decisions(self._S(jev_enabled=True), None, None)
        self.assertFalse(d.ready)


class MergeInheritCase(unittest.TestCase):
    """★ 第四态 inherit：判不准的一律**交回大模型**（= 原行为）。

    实测依据（2026-09-25）：想加的「同话题」判据不可靠 ——
      同主题不同角度 0.39 vs 互不相干 0.36（只差 0.03 ✗）
    ⇒ 用它会把这个区间的无关事实也合进来（稀释 ✗）
    ⇒ 改为：只在两端出手（明确同/明确异），中间带交回大模型 ✓
    """

    def test_ambiguous_band_inherits(self):
        for same in (0.20, 0.30, 0.40, 0.49):
            self.assertEqual(md.route_merge_soft(same, 0.85, 0.87), "inherit",
                             "模糊带必须交回大模型，而不是替它决定")

    def test_clearly_different_still_skips_llm(self):
        for same in (0.05, 0.12):
            self.assertEqual(md.route_merge_soft(same, 0.85, 0.87), "keep",
                             "明确无关 ⇒ 省掉大模型调用")

    def test_confident_cases_unchanged(self):
        self.assertEqual(md.route_merge_soft(0.68, 0.77, 0.58), "merge")
        self.assertEqual(md.route_merge_soft(0.94, 0.11, 0.35), "drop")
        self.assertEqual(md.route_merge_soft(None, 0.9, 0.1), "keep")


class MergeHintReuseCase(unittest.TestCase):
    """★ 预筛已问过 same ⇒ 三问时必须**跳过重复提问**（省约 1/3 的 JEV 用量）。

    时机是严格串行的：预筛（大模型之前）→ 大模型 → 三问（大模型之后）→ 落库。
    这里守住"第三问不重复问 same"，并确认 hint 的分数被真正采纳 ✓
    """

    def test_hint_skips_same_question_and_is_used(self):
        seen = []

        def fake_post(payload, timeout):
            qs = payload.get("questions") or {}
            seen.append(set(qs))
            # new 高（有新信息）、dilute 低（不稀释）⇒ 期望 merge ✓
            ans = {k: {"type": "noul", "noul": 0.9 if k.startswith("new_") else 0.5}
                   for k in qs}
            return {"answers": ans, "usage": {"input_tokens": 5}}

        c = md.JevClient("https://x.invalid", "k", "jev-latest")
        c._post_sync = fake_post                 # type: ignore[assignment]
        d = md.Decisions(md.JevConfig(enabled=True, base_url="https://x.invalid",
                                      api_key="k", model="jev-latest"), None, None)
        d._client = c
        out = run(d.merge_route("主事实", [("1", "候选")], hints={"1": 0.88}))
        self.assertEqual(seen[0], {"new_1", "dilute_1"},
                         "有预筛结果时**不能再问 same**（否则白花 ✗）")
        # hint(0.88 ⇒ 同一件事) + new 0.5 + dilute 0.5 ⇒ 合并 ✓
        self.assertEqual(out, {"1": "merge"})

    def test_without_hint_still_asks_same(self):
        seen = []

        def fake_post(payload, timeout):
            qs = payload.get("questions") or {}
            seen.append(set(qs))
            return {"answers": {k: {"type": "noul", "noul": 0.5} for k in qs},
                    "usage": {"input_tokens": 5}}

        c = md.JevClient("https://x.invalid", "k", "jev-latest")
        c._post_sync = fake_post                 # type: ignore[assignment]
        d = md.Decisions(md.JevConfig(enabled=True, base_url="https://x.invalid",
                                      api_key="k", model="jev-latest"), None, None)
        d._client = c
        run(d.merge_route("主事实", [("1", "候选")]))
        self.assertEqual(seen[0], {"same_1", "new_1", "dilute_1"}, "没有预筛时三问齐全 ✓")
