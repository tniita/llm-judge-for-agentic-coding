import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import aggregate
import calibration
import collect_task
import config
import foundry_eval
import make_row
import metrics
import publish_results
import run_pairwise


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def invoke(module, *args):
    with patch.object(sys, "argv", [module.__file__, *map(str, args)]), contextlib.redirect_stdout(io.StringIO()):
        module.main()


def complete_trial(run_id):
    return {
        "datasource_item": {"run_id": run_id},
        "results": [
            {"name": key, "status": "completed", "score": 5, "passed": True,
             "reason": "[confidence: high] R1: met"}
            for key in config.EVALUATORS
        ],
    }


class TemporaryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="llm-judge-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)


class MetricsTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        self.base = self.commit("base")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, message):
        self.git("add", "-A")
        self.git("-c", "user.name=Review test", "-c", "user.email=review@example.invalid",
                 "commit", "-qm", message + "\n\nCo-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>")
        return self.git("rev-parse", "HEAD")

    def compute(self, base=None, head="HEAD", cache=None):
        return metrics.compute(self.repo, base or self.base, head, metrics.DEFAULT_EXCLUDES, cache)

    def make_row(self, result, base=None, head="HEAD", hard_gate="pass"):
        write_json(self.root / "metrics.json", result)
        (self.root / "task.txt").write_text("R1: add a function", encoding="utf-8")
        (self.root / "ci.txt").write_text("pass", encoding="utf-8")
        invoke(make_row, "--run-id", "r1", "--task-id", "T1", "--variant", "fixed",
               "--repo", self.repo, "--base", base or self.base, "--head", head,
               "--hard-gate", hard_gate, "--task", self.root / "task.txt",
               "--ci", self.root / "ci.txt", "--metrics", self.root / "metrics.json",
               "--dataset", self.root / "dataset.jsonl", "--manifest", self.root / "manifest.jsonl")

    def test_diverged_base_uses_same_ancestor_as_pr_diff(self):
        self.git("checkout", "-qb", "feature")
        (self.repo / "b.py").write_text("def g():\n    return 2\n", encoding="utf-8")
        head = self.commit("feature")
        self.git("checkout", "main")
        (self.repo / "c.py").write_text("def h(x):\n    if x:\n        return 3\n    return 4\n", encoding="utf-8")
        main = self.commit("main advanced")
        result = self.compute(main, head)
        self.assertEqual(result["base"], self.base)
        self.assertEqual(result["changed_files"], ["b.py"])
        self.assertEqual(result["delta"]["complexity_total"], 1)
        diff = make_row.git_diff(self.repo, main, head, 3)
        self.assertIn("b/b.py", diff)
        self.assertNotIn("c.py", diff)
        self.make_row(result, main, head)
        self.assertEqual(make_row.read_skills(None, 40_000), "")

    def test_mismatched_metrics_are_rejected_before_append(self):
        result = self.compute()
        (self.repo / "b.py").write_text("def g():\n    return 2\n", encoding="utf-8")
        self.commit("new head")
        with self.assertRaisesRegex(SystemExit, "SHA"):
            self.make_row(result)
        self.assertFalse((self.root / "manifest.jsonl").exists())
        self.assertFalse((self.root / "dataset.jsonl").exists())

    def test_unchanged_repository_has_zero_numeric_deltas(self):
        result = self.compute()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["changed_files"], [])
        self.assertTrue(all(value == 0 for value in result["delta"].values() if value is not None))

    def test_parse_failure_is_unavailable_and_reaches_human_review(self):
        (self.repo / "a.py").write_text("def f(:\n", encoding="utf-8")
        self.commit("invalid head")
        result = self.compute()
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("a.py", result["parse_errors"]["head"])
        self.assertTrue(all(value is None for value in result["after"].values()))
        self.assertTrue(all(value is None for value in result["delta"].values()))
        self.assertEqual(result["functions"], [])
        self.assertIn("計測不能", metrics.format_delta(result))
        self.make_row(result)
        row = json.loads((self.root / "manifest.jsonl").read_text())
        self.assertEqual(row["metrics_status"], "unavailable")
        self.assertTrue(all(value is None for value in row["metrics_delta"].values()))
        write_jsonl(self.root / "manifest.jsonl", [
            row, {"run_id": "r2", "task_id": "T2", "variant": "fixed", "attempt": 1,
                  "hard_gate": "pass", "metrics_status": "completed", "metrics_delta": {"complexity_total": 7}},
        ])
        for i in range(1, 4):
            write_jsonl(self.root / f"trial{i}.jsonl", [complete_trial("r1"), complete_trial("r2")])
        invoke(aggregate, self.root, self.root / "manifest.jsonl")
        summaries = aggregate.load_jsonl(self.root / "summary.jsonl")
        self.assertIn("計測不能", summaries[0]["human_review"][0])
        self.assertIsNone(summaries[0]["metrics_delta"])
        stats = json.loads((self.root / "variants.json").read_text())
        self.assertEqual(stats["variants"]["fixed"]["mean_metrics_delta"]["complexity_total"], 7)
        docs = publish_results.build_documents("shop", [], self.root)
        props, measurements = publish_results.flatten(next(d for d in docs if d.get("run_id") == "r1"))
        self.assertEqual(props["metrics_status"], "unavailable")
        self.assertGreater(measurements["human_review.count"], 0)
        self.assertFalse(any(key.startswith("metrics_delta.") for key in measurements))

    def test_base_parse_failure_is_not_fabricated_growth(self):
        (self.repo / "a.py").write_text("def f(:\n", encoding="utf-8")
        invalid = self.commit("invalid base")
        (self.repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        self.commit("valid head")
        result = self.compute(invalid)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("base", result["parse_errors"])
        self.assertIsNone(result["delta"]["complexity_total"])

    def test_hard_gate_failure_is_still_recorded_with_measurement_errors(self):
        (self.repo / "a.py").write_text("def f(:\n", encoding="utf-8")
        self.commit("invalid")
        self.make_row(self.compute(), hard_gate="fail")
        row = json.loads((self.root / "manifest.jsonl").read_text())
        self.assertEqual(row["hard_gate"], "fail")
        self.assertEqual(row["metrics_status"], "unavailable")
        self.assertFalse((self.root / "dataset.jsonl").exists())

    def test_old_cache_is_invalidated_and_new_cache_reused(self):
        cache = self.root / "cache"
        cache.mkdir()
        blob = self.git("rev-parse", "HEAD:a.py")
        write_json(cache / f"v1-{blob}.json", {"functions": [], "imports": [], "lines": []})
        first = self.compute(cache=cache)
        self.assertEqual(first["before"]["complexity_total"], 1)
        self.assertEqual(first["cache"]["misses"], 1)
        second = self.compute(cache=cache)
        self.assertEqual(second["cache"]["misses"], 0)
        self.assertGreater(second["cache"]["hits"], 0)
        self.assertEqual(first["delta"], second["delta"])

    def test_missing_usage_is_not_saved_as_zero(self):
        self.make_row(self.compute())
        row = json.loads((self.root / "manifest.jsonl").read_text())
        self.assertNotIn("tokens_in", row)
        self.assertNotIn("tokens_out", row)
        self.assertNotIn("cost_usd", row)


class ComplexityTests(unittest.TestCase):
    def test_nested_function_changes_are_counted(self):
        before = "def outer(x):\n    def inner(y):\n        return y\n    return inner(x)\n"
        after = "def outer(x):\n    def inner(y):\n        if y:\n            return 1\n        return 0\n    return inner(x)\n"
        old = metrics.snapshot({"a.py": metrics.analyze_source(before)})
        new = metrics.snapshot({"a.py": metrics.analyze_source(after)})
        result = metrics.compare(old, new, {"a.py"})
        self.assertEqual(result["delta"]["complexity_total"], 1)
        self.assertEqual(result["functions"][0]["name"], "outer.inner")
        self.assertEqual(result["functions"][0]["delta"], 1)

    def test_local_classes_nested_classes_and_async_functions(self):
        source = (
            "def outer():\n"
            "    class Local:\n"
            "        async def work(self, x):\n"
            "            if x:\n"
            "                return 1\n"
            "            return 0\n"
            "    return Local\n"
            "class A:\n"
            "    class Inner:\n"
            "        def work(self):\n"
            "            return 1\n"
        )
        functions = metrics.analyze_source(source)["functions"]
        self.assertEqual({f["name"]: f["complexity"] for f in functions},
                         {"outer": 1, "outer.Local.work": 2, "A.Inner.work": 1})

    def test_same_names_in_different_scopes_and_property_accessors_do_not_collide(self):
        source = (
            "class A:\n"
            "    @property\n"
            "    def value(self):\n"
            "        return 1\n"
            "    @value.setter\n"
            "    def value(self, x):\n"
            "        if x:\n"
            "            return\n"
            "class B:\n"
            "    def value(self):\n"
            "        return 2\n"
        )
        snapshot = metrics.snapshot({"a.py": metrics.analyze_source(source)})
        self.assertEqual(set(snapshot["functions"]), {"a.py::A.value", "a.py::B.value"})
        self.assertEqual(snapshot["functions"]["a.py::A.value"]["definitions"], 2)
        self.assertEqual(snapshot["summary"]["complexity_total"], 4)
        self.assertEqual(snapshot["summary"]["complexity_max"], 2)

    def test_inserting_same_named_definition_does_not_shift_other_identities(self):
        before = ("class A:\n    def run(self):\n        return 1\n"
                  "def helper(x):\n    if x:\n        return 1\n    return 0\n")
        after = ("class A:\n    def run(self):\n        return 1\n    def run(self, x):\n        return x\n"
                 "def helper(x):\n    if x:\n        return 1\n    return 0\n")
        old = metrics.snapshot({"a.py": metrics.analyze_source(before)})
        new = metrics.snapshot({"a.py": metrics.analyze_source(after)})
        changes = metrics.compare(old, new, {"a.py"})["functions"]
        self.assertEqual([(f["name"], f["before"], f["after"]) for f in changes], [("A.run", 1, 2)])


class PublishingTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.absolute = self.root / "absolute"
        self.pairwise = self.root / "pairwise"
        self.absolute.mkdir()
        self.pairwise.mkdir()
        write_json(self.absolute / "run_meta.json", {"eval_id": "eval-absolute"})
        write_json(self.pairwise / "run_meta.json", {"eval_id": "eval-pairwise"})
        write_jsonl(self.absolute / "summary.jsonl", [
            {"run_id": "rA", "task_id": "T1", "variant": "fixed", "human_review": [], "total": 100},
            {"run_id": "rB", "task_id": "T1", "variant": "fusion", "human_review": [], "total": 100},
        ])
        self.pairs = [{"pair_id": "P001", "task_id": "T1", "variant_a": "fixed", "run_a": "rA",
                       "variant_b": "fusion", "run_b": "rB"}]
        write_jsonl(self.pairwise / "trial1.jsonl", [
            {"datasource_item": {"pair_key": "P001-1"}, "results": [{"score": 1, "reason": "[confidence: high] A"}]},
            {"datasource_item": {"pair_key": "P001-2"}, "results": [{"score": 1, "reason": "[confidence: high] A"}]},
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            run_pairwise.aggregate(self.pairwise, self.pairs)
        self.manifest = [{"run_id": "rA", "task_id": "T1", "variant": "fixed", "hard_gate": "pass"},
                         {"run_id": "failed", "task_id": "T2", "variant": "fusion", "hard_gate": "fail"}]

    def test_pairwise_only_publishing_needs_no_absolute_summary(self):
        docs = publish_results.build_documents("shop", self.manifest, None, self.pairwise)
        self.assertEqual({d["type"] for d in docs}, {"attempt", "pairwise", "pair"})
        self.assertTrue(any(d.get("hard_gate") == "fail" for d in docs))

    def test_results_dir_also_accepts_pairwise_only_output(self):
        docs = publish_results.build_documents("shop", [], self.pairwise)
        self.assertEqual({d["type"] for d in docs}, {"pairwise", "pair"})

    def test_combined_publication_preserves_evaluation_ids(self):
        docs = publish_results.build_documents("shop", self.manifest, self.absolute, self.pairwise)
        for doc in docs:
            if doc["type"] == "run":
                self.assertEqual(doc["eval_id"], "eval-absolute")
            elif doc["type"] in ("pair", "pairwise"):
                self.assertEqual(doc["eval_id"], "eval-pairwise")
        self.assertEqual(len({d["id"] for d in docs}), len(docs))

    def test_pair_review_signal_keeps_both_run_ids_and_event_fields(self):
        docs = publish_results.build_documents("shop", [], self.absolute, self.pairwise)
        pair = next(d for d in docs if d["type"] == "pair")
        self.assertTrue(pair["position_inconsistent"])
        self.assertEqual((pair["run_a"], pair["run_b"]), ("rA", "rB"))
        self.assertEqual(len(pair["human_review"]), 1)
        props, measurements = publish_results.flatten(pair)
        self.assertEqual(publish_results.EVENT_NAMES["pair"], "LlmJudgePair")
        self.assertEqual(props["run_a"], "rA")
        self.assertEqual(props["eval_id"], "eval-pairwise")
        self.assertEqual(measurements["human_review.count"], 1)
        query = next(item["content"]["query"] for item in
                     json.loads((ROOT / "infra/workbook.json").read_text())["items"]
                     if item.get("name") == "人間レビューに回す Run")
        for part in ('"LlmJudgePair"', "pack_array(customDimensions.run_a, customDimensions.run_b)", "mv-expand run_id"):
            self.assertIn(part, query)

    def test_legacy_pairwise_summary_still_routes_to_review(self):
        path = self.pairwise / "pairwise_summary.json"
        summary = json.loads(path.read_text())
        del summary["pairs"][0]["human_review"]
        write_json(path, summary)
        docs = publish_results.build_documents("shop", [], self.pairwise)
        self.assertEqual(len(next(d for d in docs if d["type"] == "pair")["human_review"]), 1)

    def test_consistent_pair_has_no_review_reason(self):
        path = self.pairwise / "pairwise_summary.json"
        summary = json.loads(path.read_text())
        summary["pairs"][0].update(position_inconsistent=False, human_review=[])
        write_json(path, summary)
        docs = publish_results.build_documents("shop", [], self.pairwise)
        self.assertEqual(next(d for d in docs if d["type"] == "pair")["human_review"], [])

    def test_duplicate_directory_is_not_published_twice(self):
        docs = publish_results.build_documents("shop", [], self.pairwise, self.pairwise)
        self.assertEqual(len(docs), 2)

    def test_missing_results_fail_explicitly(self):
        with self.assertRaisesRegex(SystemExit, "summary.jsonl"):
            publish_results.build_documents("shop", [], self.root / "missing")

    def test_pairwise_argument_rejects_absolute_only_directory(self):
        with self.assertRaisesRegex(SystemExit, "pairwise_summary.json"):
            publish_results.build_documents("shop", [], None, self.absolute)

    def test_pairwise_cli_dry_run_and_manifest_only(self):
        manifest = self.root / "manifest.jsonl"
        write_jsonl(manifest, self.manifest)
        invoke(publish_results, "--project", "shop", "--manifest", manifest,
               "--pairwise-dir", self.pairwise, "--dry-run")
        docs = publish_results.build_documents("shop", self.manifest, None)
        self.assertEqual(len(docs), 2)


class HumanPacketTests(TemporaryTest):
    def test_packet_contains_saved_inputs_not_live_skill_files(self):
        dataset = self.root / "dataset.jsonl"
        write_jsonl(dataset, [{"run_id": "r1", "query": "R1: task", "response": "diff",
                              "ci_summary": "pass", "pr_description": "pr",
                              "context": "CONTEXT_SNAPSHOT", "project_skills": "SKILL_SNAPSHOT",
                              "metrics_delta": "DELTA_SNAPSHOT", "variant": "SECRET_VARIANT"}])
        packet_dir = self.root / "packets"
        args = SimpleNamespace(dataset=dataset, seed=0, sample=1, flagged=None, packet_dir=packet_dir,
                               out=self.root / "review.csv")
        with contextlib.redirect_stdout(io.StringIO()):
            calibration.export(args)
        text = (packet_dir / "r1.md").read_text()
        for marker in ("CONTEXT_SNAPSHOT", "SKILL_SNAPSHOT", "DELTA_SNAPSHOT"):
            self.assertIn(marker, text)
        self.assertNotIn("SECRET_VARIANT", text)

    def test_old_dataset_without_context_and_skills_is_supported(self):
        text = calibration.run_section("実装", {"ci_summary": "pass", "response": "diff"}, include_context=True)
        self.assertIn("関連する既存コード\n\nなし", text)
        self.assertIn("プロジェクトの Skill（評価時の入力）\n\nなし", text)

    def test_pair_packets_include_the_same_inputs_as_the_pairwise_judge(self):
        rows = [{"run_id": run_id, "query": "R1: task", "response": f"DIFF_{run_id}", "ci_summary": f"CI_{run_id}",
                 "pr_description": "PR_TEXT", "context": "CONTEXT_SNAPSHOT", "project_skills": "SKILL_SNAPSHOT",
                 "metrics_delta": f"DELTA_{run_id}"} for run_id in ("rA", "rB")]
        write_jsonl(self.root / "dataset.jsonl", rows)
        pairwise_dir = self.root / "pairwise"
        pairwise_dir.mkdir()
        write_json(pairwise_dir / "pairs.json", [{"pair_id": "P001", "task_id": "T1", "variant_a": "fixed",
                                                  "run_a": "rA", "variant_b": "fusion", "run_b": "rB"}])
        args = SimpleNamespace(dataset=self.root / "dataset.jsonl", pairwise_dir=pairwise_dir, seed=0,
                               out=self.root / "pairs.csv", packet_dir=self.root / "pairs", flagged=False)
        with contextlib.redirect_stdout(io.StringIO()):
            calibration.export_pairs(args)
        text = (self.root / "pairs" / "Q01.md").read_text()
        self.assertEqual(text.count("SKILL_SNAPSHOT"), 1)
        for marker in ("DIFF_rA", "DIFF_rB", "CI_rA", "CI_rB", "DELTA_rA", "DELTA_rB"):
            self.assertIn(marker, text)
        # ペア比較の Judge が見ない入力・方式名は載せない
        for marker in ("CONTEXT_SNAPSHOT", "PR_TEXT", "fixed", "fusion"):
            self.assertNotIn(marker, text)
        judge_fields = set(config.PAIRWISE["fields"])
        self.assertTrue({"project_skills", "metrics_delta_a", "metrics_delta_b"} <= judge_fields)
        self.assertFalse({"context", "pr_description"} & judge_fields)


class TelemetryTests(unittest.TestCase):
    def test_missing_usage_is_not_flattened_as_zero(self):
        _, measurements = publish_results.flatten({"type": "attempt", "run_id": "r1",
                                                    "tokens_in": None, "tokens_out": 0, "cost_usd": None})
        self.assertNotIn("tokens_in", measurements)
        self.assertNotIn("cost_usd", measurements)
        self.assertEqual(measurements["tokens_out"], 0)

    def test_no_per_run_metric_dimension_in_either_apim_policy(self):
        dimensions = (ROOT / "infra/policies/dimensions.xml").read_text()
        self.assertNotIn('name="run_id"', dimensions)
        for filename in ("openai.xml", "anthropic.xml"):
            policy = (ROOT / "infra/policies" / filename).read_text().replace("__DIMENSIONS__", dimensions)
            self.assertNotIn("x-llm-judge-run-id", policy)
            self.assertIn('name="project"', policy)
            self.assertIn('name="variant"', policy)

    def test_per_run_tokens_remain_in_events_for_more_than_100_runs(self):
        manifest = [{"run_id": f"r{i}", "variant": "fixed", "hard_gate": "pass",
                     "tokens_in": i, "tokens_out": 2} for i in range(150)]
        docs = publish_results.build_documents("shop", manifest, None)
        self.assertEqual(len(docs), 150)
        props, measures = publish_results.flatten(docs[-1])
        self.assertEqual(props["run_id"], "r149")
        self.assertEqual(measures["tokens_in"], 149)
        with patch.object(publish_results.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b'{"itemsAccepted":150}'
            with contextlib.redirect_stdout(io.StringIO()):
                publish_results.send_appinsights(
                    "InstrumentationKey=test;IngestionEndpoint=https://example.invalid", docs)
            envelopes = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(len(envelopes), 150)
        event = envelopes[-1]["data"]["baseData"]
        self.assertEqual(event["name"], "LlmJudgeAttempt")
        self.assertEqual(event["properties"]["run_id"], "r149")
        self.assertEqual(event["measurements"]["tokens_in"], 149)
        query = (ROOT / "infra/queries.kql").read_text()
        self.assertIn('Name == "LlmJudgeAttempt"', query)
        self.assertIn("join kind=leftouter tokens on project, run_id", query)

    def test_workbook_token_queries_do_not_depend_on_run_metric_dimensions(self):
        workbook = json.loads((ROOT / "infra/workbook.json").read_text())
        for item in workbook["items"]:
            query = item.get("content", {}).get("query", "")
            if "customMetrics" in query:
                self.assertNotIn("run_id", query)
        self.assertTrue(any(item.get("name") == "Run ごとのトークン利用量（実行基盤）" for item in workbook["items"]))

    def test_anthropic_tokens_use_streaming_safe_native_policy(self):
        policy = (ROOT / "infra/policies/anthropic.xml").read_text()
        self.assertIn("<llm-emit-token-metric", policy)
        # 応答本文を読むポリシーはストリーミング応答をバッファリングする
        self.assertNotIn("Response.Body", policy)
        self.assertNotIn("<emit-metric", policy)
        bicep = (ROOT / "infra/main.bicep").read_text()
        allowed = bicep[bicep.index("@allowed(["):bicep.index("param apimSku")]
        self.assertNotIn("'Developer'", allowed)
        self.assertIn("'BasicV2'", allowed)
        workbook = (ROOT / "infra/workbook.json").read_text()
        self.assertIn('name == \\"Total Tokens\\"', workbook)


class PairwiseRoutingTests(TemporaryTest):
    pair = {"pair_id": "P001", "task_id": "T1", "variant_a": "a", "run_a": "r1", "variant_b": "b", "run_b": "r2"}

    def aggregate(self, rows):
        write_jsonl(self.root / "trial1.jsonl", rows)
        with contextlib.redirect_stdout(io.StringIO()):
            run_pairwise.aggregate(self.root, [self.pair])
        return json.loads((self.root / "pairwise_summary.json").read_text())

    @staticmethod
    def row(order, **result):
        return {"datasource_item": {"pair_key": f"P001-{order}"}, "results": [result]}

    def test_missing_order_is_not_counted_and_goes_to_human_review(self):
        summary = self.aggregate([
            self.row(1, score=1, status="completed", reason="[confidence: high] A"),
            self.row(2, score=None, status="failed"),
        ])
        pair = summary["pairs"][0]
        self.assertIsNone(pair["pref"])
        self.assertTrue(any("評価エラー" in r for r in pair["human_review"]))
        self.assertTrue(any("順序 2 の判定がなく" in r for r in pair["human_review"]))
        self.assertEqual(summary["comparisons"], [])

    def test_low_or_missing_confidence_goes_to_human_review(self):
        summary = self.aggregate([
            self.row(1, score=1, reason="[confidence: low] A"),
            self.row(2, score=5, reason="B"),
        ])
        pair = summary["pairs"][0]
        self.assertEqual(pair["pref"], 1.0)
        self.assertTrue(any("confidence が low" in r and "1, 2" in r for r in pair["human_review"]))

    def test_consistent_complete_pair_is_decided_without_review(self):
        summary = self.aggregate([
            self.row(1, score=2, reason="[confidence: high] A"),
            self.row(2, score=4, reason="[confidence: medium] B"),
        ])
        self.assertEqual(summary["pairs"][0]["human_review"], [])
        self.assertEqual(summary["comparisons"][0]["wins_a"], 1)

    def test_judge_items_include_skills_and_both_metric_deltas(self):
        dataset = {r: {"query": "R1", "response": f"diff-{r}", "ci_summary": "pass",
                       "project_skills": "SKILL", "metrics_delta": f"delta-{r}"} for r in ("r1", "r2")}
        first, second = run_pairwise.to_items([self.pair], dataset)
        self.assertEqual((first["metrics_delta_a"], first["metrics_delta_b"]), ("delta-r1", "delta-r2"))
        self.assertEqual((second["metrics_delta_a"], second["metrics_delta_b"]), ("delta-r2", "delta-r1"))
        self.assertEqual(first["project_skills"], "SKILL")
        self.assertEqual(set(first) - {"pair_key"}, set(config.PAIRWISE["fields"]))
        prompt = config.build_pairwise_prompt()
        for field in ("{{project_skills}}", "{{metrics_delta_a}}", "{{metrics_delta_b}}"):
            self.assertIn(field, prompt)

    def test_legacy_rows_without_skills_or_metrics_still_compare(self):
        dataset = {r: {"query": "R1", "response": "d", "ci_summary": "pass"} for r in ("r1", "r2")}
        item = run_pairwise.to_items([self.pair], dataset)[0]
        self.assertEqual((item["project_skills"], item["metrics_delta_a"]), ("なし", "なし"))

    def test_different_skills_are_rejected(self):
        dataset = {r: {"query": "R1", "response": "d", "ci_summary": "pass", "project_skills": f"S-{r}"}
                   for r in ("r1", "r2")}
        with self.assertRaisesRegex(SystemExit, "Skill"):
            run_pairwise.to_items([self.pair], dataset)


class RateLimitRetryTests(unittest.TestCase):
    @staticmethod
    def row(key, status="completed", message=""):
        result = {"name": "x", "status": status, "score": 5 if status == "completed" else None}
        if message:
            result["sample"] = {"error": {"code": "FAILED_EXECUTION", "message": message}}
        return {"datasource_item": {"run_id": key}, "results": [result]}

    def test_only_transient_errors_are_rejudged_and_replaced(self):
        rows = [self.row("r1"), self.row("r2", "error", "Error code: 429 - RateLimitReached"),
                self.row("r3", "error", "Error code: 404 - api_not_supported"),
                self.row("r4", "error", "Request timed out.")]
        calls = []

        def rejudge(failed):
            calls.append([r["datasource_item"]["run_id"] for r in failed])
            return [self.row(r["datasource_item"]["run_id"]) for r in failed]

        with patch.object(foundry_eval.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            out, retried = foundry_eval.retry_transient(rows, "run_id", rejudge, retries=3, wait_sec=1)
        self.assertEqual(calls, [["r2", "r4"]])
        self.assertEqual(retried, 2)
        self.assertEqual([r["results"][0]["status"] for r in out], ["completed", "completed", "error", "completed"])

    def test_retries_stop_at_the_limit_and_keep_the_error(self):
        rows = [self.row("r1", "error", "429")]
        with patch.object(foundry_eval.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
            out, retried = foundry_eval.retry_transient(rows, "run_id", lambda failed: failed, retries=2, wait_sec=10)
        self.assertEqual(retried, 2)
        self.assertTrue(foundry_eval.transient_error(out[0]))
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [10, 20])
        with contextlib.redirect_stdout(io.StringIO()):
            _, retried = foundry_eval.retry_transient(rows, "run_id", lambda failed: failed, retries=0, wait_sec=1)
        self.assertEqual(retried, 0)


class SelfPreferenceTests(TemporaryTest):
    """評価対象が Judge と同系統のモデルを使ったときの扱い。"""

    def test_model_family_handles_hosts_routers_and_lookalikes(self):
        cases = {
            "gpt-6.1-sol": "openai", "gpt-6-luna": "openai", "o4-mini": "openai", "gpt-oss-120b": "openai",
            "claude-opus-5.5": "anthropic", "claude-opus-5-5": "anthropic", "grok-4.7": "xai",
            "FW-Kimi-K3": "moonshot", "Meta-Llama-3.3-70B-Instruct": "meta", "DeepSeek-V4-Pro": "deepseek",
            "Mistral-Large-3": "mistral", "MAI-Thinking-1": "microsoft", "Phi-4": "microsoft",
            "fusion": None, "model-router": None, "Microsoft-Decision-1": None, "openings": None, "": None,
        }
        for name, family in cases.items():
            self.assertEqual(config.model_family(name), family, name)
        self.assertEqual(config.same_family_models(["gpt-6-luna", "claude-opus-5.5"], "gpt-6.1-sol"), ["gpt-6-luna"])
        self.assertEqual(config.same_family_models(None, "gpt-6.1-sol"), [])
        self.assertEqual(config.same_family_models(["fusion"], "model-router"), [])

    def test_agent_models_go_to_manifest_and_are_redacted_from_dataset(self):
        task, ci, diff = self.root / "task.md", self.root / "ci.txt", self.root / "pr.diff"
        task.write_text("R1: x", encoding="utf-8")
        ci.write_text("pass", encoding="utf-8")
        diff.write_text("diff --git a/a.py b/a.py\n+# tuned for grok-4.7\n", encoding="utf-8")
        invoke(make_row, "--run-id", "r1", "--task-id", "T1", "--variant", "v", "--task", task, "--ci", ci,
               "--diff", diff, "--agent-models", "grok-4.7, claude-opus-5.5",
               "--dataset", self.root / "runs.jsonl", "--manifest", self.root / "manifest.jsonl")
        manifest = json.loads((self.root / "manifest.jsonl").read_text())
        self.assertEqual(manifest["agent_models"], ["grok-4.7", "claude-opus-5.5"])
        row = (self.root / "runs.jsonl").read_text()
        self.assertNotIn("agent_models", row)
        self.assertNotIn("grok", row)

    def absolute(self, totals, models, judge="gpt-6.1-sol"):
        out = self.root / "abs"
        out.mkdir(exist_ok=True)
        write_json(out / "run_meta.json", {"judge": {"deployment": judge, "model": judge, "version": "1"}})
        trials = []
        for run_id, score in totals.items():
            trial = complete_trial(run_id)
            for r in trial["results"]:
                r["score"] = score
            trials.append(trial)
        write_jsonl(out / "trial1.jsonl", trials)
        manifest = [{"run_id": r, "task_id": "T1", "variant": r, "attempt": 1, "hard_gate": "pass",
                     **({"agent_models": models[r]} if models.get(r) else {})} for r in totals]
        write_jsonl(self.root / "manifest.jsonl", manifest)
        invoke(aggregate, out, self.root / "manifest.jsonl", "--gap", "0")
        summary = {s["run_id"]: s for s in collect_task.load_jsonl(out / "summary.jsonl")}
        return summary, json.loads((out / "variants.json").read_text())["variants"]

    def test_absolute_win_by_same_family_side_goes_to_human_review(self):
        summary, variants = self.absolute({"fus": 5, "opus": 4}, {"fus": ["gpt-6-luna"], "opus": ["claude-opus-5.5"]})
        for s in summary.values():
            self.assertTrue(any("自己選好" in r and "gpt-6-luna" in r for r in s["human_review"]), s)
        self.assertEqual(summary["fus"]["judge_same_family"], ["gpt-6-luna"])
        self.assertEqual((variants["fus"]["judge_same_family_rate"], variants["opus"]["judge_same_family_rate"]),
                         (1.0, 0.0))

    def test_absolute_win_against_same_family_side_or_unknown_models_is_not_flagged(self):
        summary, _ = self.absolute({"fus": 4, "opus": 5}, {"fus": ["gpt-6-luna"], "opus": ["claude-opus-5.5"]})
        self.assertFalse(any("自己選好" in r for s in summary.values() for r in s["human_review"]))
        summary, variants = self.absolute({"fus": 5, "opus": 4}, {})
        self.assertFalse(any("自己選好" in r for s in summary.values() for r in s["human_review"]))
        self.assertIsNone(variants["fus"]["judge_same_family_rate"])
        summary, _ = self.absolute({"fus": 5, "opus": 4}, {"fus": ["gpt-6-luna"], "opus": ["claude-opus-5.5"]},
                                   judge="grok-4.7")
        self.assertFalse(any("自己選好" in r for s in summary.values() for r in s["human_review"]))

    def pairwise(self, score_order1, models_a, models_b, judge="gpt-6.1-sol"):
        write_json(self.root / "run_meta.json", {"judge": {"deployment": judge, "model": judge}})
        write_jsonl(self.root / "trial1.jsonl", [
            {"datasource_item": {"pair_key": "P001-1"},
             "results": [{"score": score_order1, "reason": "[confidence: high] x"}]},
            {"datasource_item": {"pair_key": "P001-2"},
             "results": [{"score": 6 - score_order1, "reason": "[confidence: high] x"}]},
        ])
        manifest = [{"run_id": "r1", "task_id": "T1", "variant": "a", "hard_gate": "pass", "agent_models": models_a},
                    {"run_id": "r2", "task_id": "T1", "variant": "b", "hard_gate": "pass", "agent_models": models_b}]
        write_jsonl(self.root / "manifest.jsonl", manifest)
        pairs = run_pairwise.build_pairs({"r1": {}, "r2": {}}, manifest)
        with contextlib.redirect_stdout(io.StringIO()):
            run_pairwise.aggregate(self.root, pairs)
        return json.loads((self.root / "pairwise_summary.json").read_text())

    def test_pair_favoring_same_family_side_goes_to_human_review(self):
        # 順序 1 で 1 点 = A（r1）が優位
        summary = self.pairwise(1, ["gpt-6-luna"], ["claude-opus-5.5"])
        pair = summary["pairs"][0]
        self.assertEqual(pair["models_a"], ["gpt-6-luna"])
        self.assertEqual(pair["judge_same_family"], {"a": ["gpt-6-luna"], "b": []})
        self.assertTrue(any("自己選好" in r for r in pair["human_review"]))
        self.assertEqual(summary["judge"], "gpt-6.1-sol")

    def test_pair_favoring_other_family_or_with_cross_family_judge_is_not_flagged(self):
        self.assertEqual(self.pairwise(5, ["gpt-6-luna"], ["claude-opus-5.5"])["pairs"][0]["human_review"], [])
        self.assertEqual(self.pairwise(1, ["gpt-6-luna"], ["claude-opus-5.5"], judge="grok-4.7")
                         ["pairs"][0]["human_review"], [])
        # 両方とも同系統ならバイアスは両側にかかるので、片側だけを疑う理由にはしない
        self.assertEqual(self.pairwise(1, ["gpt-6-luna"], ["gpt-5.6-sol"])["pairs"][0]["human_review"], [])

    def test_aggregate_only_mode_reads_models_from_manifest(self):
        write_json(self.root / "pairs.json", [PairwiseRoutingTests.pair])
        self.pairwise(1, ["gpt-6-luna"], ["claude-opus-5.5"])
        write_jsonl(self.root / "runs.jsonl", [])
        invoke(run_pairwise, self.root / "runs.jsonl", self.root / "manifest.jsonl", "--results-dir", self.root)
        pair = json.loads((self.root / "pairwise_summary.json").read_text())["pairs"][0]
        self.assertTrue(any("自己選好" in r for r in pair["human_review"]))


class HumanDecisionTests(TemporaryTest):
    def test_absolute_export_can_select_only_flagged_runs(self):
        rows = [{"run_id": r, "query": "q", "response": "d", "ci_summary": "c", "pr_description": "p"}
                for r in ("r1", "r2", "r3")]
        write_jsonl(self.root / "dataset.jsonl", rows)
        write_jsonl(self.root / "summary.jsonl", [
            {"run_id": "r1", "human_review": ["指標の悪化"]}, {"run_id": "r2", "human_review": []},
            {"run_id": "r3", "human_review": ["confidence が low"]},
        ])
        args = SimpleNamespace(dataset=self.root / "dataset.jsonl", seed=0, sample=None,
                               flagged=self.root / "summary.jsonl", packet_dir=None, out=self.root / "review.csv")
        with contextlib.redirect_stdout(io.StringIO()):
            calibration.export(args)
        with (self.root / "review.csv").open(encoding="utf-8-sig") as f:
            self.assertEqual(sorted(r["run_id"] for r in calibration.csv.DictReader(f)), ["r1", "r3"])

    def test_pair_export_can_select_only_flagged_pairs(self):
        write_jsonl(self.root / "dataset.jsonl", [
            {"run_id": r, "query": "q", "response": "d", "ci_summary": "c"} for r in ("r1", "r2", "r3", "r4")])
        pairs = [{"pair_id": "P001", "task_id": "T1", "variant_a": "a", "run_a": "r1", "variant_b": "b", "run_b": "r2"},
                 {"pair_id": "P002", "task_id": "T2", "variant_a": "a", "run_a": "r3", "variant_b": "b", "run_b": "r4"}]
        write_json(self.root / "pairs.json", pairs)
        write_json(self.root / "pairwise_summary.json", {"pairs": [
            {**pairs[0], "human_review": []}, {**pairs[1], "human_review": ["ペア比較: A/B の順序で判定が逆転"]}]})
        args = SimpleNamespace(dataset=self.root / "dataset.jsonl", pairwise_dir=self.root, seed=0,
                               out=self.root / "pairs.csv", packet_dir=self.root / "packets", flagged=True)
        with contextlib.redirect_stdout(io.StringIO()):
            calibration.export_pairs(args)
        key = json.loads((self.root / "packets" / "_key.json").read_text())
        self.assertEqual([v["pair_id"] for v in key.values()], ["P002"])

    def test_human_score_columns_match_rubric(self):
        self.assertEqual(list(publish_results.HUMAN_SCORE_KEYS), calibration.ORDINAL_KEYS)

    def test_run_decisions_are_published_with_join_key(self):
        with (self.root / "review.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = calibration.csv.DictWriter(f, fieldnames=["run_id", "packet_file", *calibration.SCORE_COLUMNS])
            writer.writeheader()
            writer.writerow({"run_id": "r1", "requirements": "4", "design": "2", "uncaught_critical": "0",
                             "comment": "N+1 query"})
            writer.writerow({"run_id": "r2"})
        docs = publish_results.human_run_documents(
            "shop", [{"run_id": "r1", "task_id": "T1", "variant": "fixed"}], self.root / "review.csv", "now")
        self.assertEqual(len(docs), 1)
        doc = docs[0]
        self.assertEqual((doc["type"], doc["review_key"], doc["variant"]), ("human_review", "r1", "fixed"))
        self.assertEqual(doc["scores"], {"requirements": 4.0, "design": 2.0})
        self.assertFalse(doc["uncaught_critical"])
        self.assertIn("uncaught_critical=問題あり", doc["decision"])
        self.assertEqual(publish_results.EVENT_NAMES["human_review"], "LlmJudgeHumanReview")

    def test_pair_decisions_resolve_blind_labels_to_variants(self):
        pairwise = self.root / "pairwise"
        pairwise.mkdir()
        write_json(pairwise / "run_meta.json", {"eval_id": "eval-pair"})
        write_json(pairwise / "pairwise_summary.json", {"pairs": [
            {"pair_id": "P001", "task_id": "T1", "variant_a": "fixed", "run_a": "rA",
             "variant_b": "fusion", "run_b": "rB"}]})
        write_json(self.root / "key.json", {"Q01": {"pair_id": "P001", "A": "rB", "B": "rA"},
                                            "Q02": {"pair_id": "P001", "A": "rA", "B": "rB"}})
        with (self.root / "pairs.csv").open("w", encoding="utf-8-sig", newline="") as f:
            f.write("review_id,packet_file,better,comment\nQ01,Q01.md,A,clear\nQ02,Q02.md,,\n")
        docs = publish_results.human_pair_documents("shop", self.root / "pairs.csv", self.root / "key.json",
                                                    pairwise, "now")
        self.assertEqual(len(docs), 1)
        self.assertEqual((docs[0]["better"], docs[0]["better_run"]), ("fusion", "rB"))
        self.assertEqual(docs[0]["review_key"], "eval-pair:P001")

    def test_dashboards_join_flagged_items_with_human_decisions(self):
        workbook = json.loads((ROOT / "infra/workbook.json").read_text())
        query = next(i["content"]["query"] for i in workbook["items"] if i.get("name") == "人間レビューに回す Run")
        kql = (ROOT / "infra/queries.kql").read_text()
        for text, name in ((query, "name"), (kql, "Name")):
            self.assertIn('"LlmJudgeHumanReview"', text)
            self.assertIn(f'review_key = iff({name} == "LlmJudgePair", strcat(eval_id, ":", review_id),', text)
            # 評価サービスの Run の人間レビューは実験ごとに分ける（<experiment_id>:<run_id>）
            self.assertIn('iff(isempty(experiment_id), review_id, strcat(experiment_id, ":", review_id))', text)
            self.assertIn("join kind=leftouter human on project, review_key", text)
            self.assertIn('"未レビュー"', text)

    def test_human_pair_reviews_require_key_and_pairwise_dir(self):
        manifest = self.root / "manifest.jsonl"
        write_jsonl(manifest, [])
        with self.assertRaisesRegex(SystemExit, "--pair-key"):
            invoke(publish_results, "--project", "shop", "--manifest", manifest,
                   "--human-pair-reviews", self.root / "pairs.csv", "--dry-run")


class MetricRobustnessTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        self.base = self.commit("base")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, message):
        self.git("add", "-A")
        self.git("-c", "user.name=Review test", "-c", "user.email=review@example.invalid", "commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def row_args(self, *extra, hard_gate="pass"):
        (self.root / "task.txt").write_text("R1: task", encoding="utf-8")
        (self.root / "ci.txt").write_text("pass", encoding="utf-8")
        return ["--run-id", "r1", "--task-id", "T1", "--variant", "fixed", "--hard-gate", hard_gate,
                "--task", self.root / "task.txt", "--ci", self.root / "ci.txt",
                "--dataset", self.root / "dataset.jsonl", "--manifest", self.root / "manifest.jsonl", *extra]

    def test_unrelated_histories_fail_with_clear_message(self):
        self.git("checkout", "-q", "--orphan", "other")
        self.git("rm", "-rqf", ".")
        (self.repo / "b.py").write_text("x = 1\n", encoding="utf-8")
        other = self.commit("unrelated")
        with self.assertRaisesRegex(SystemExit, "共通祖先"):
            metrics.compute(self.repo, self.base, other, metrics.DEFAULT_EXCLUDES, None)

    def test_metric_failure_is_recorded_as_unavailable_even_for_hard_gate_failure(self):
        invoke(make_row, *self.row_args("--metrics-error", "共通祖先がありません", hard_gate="fail"))
        row = json.loads((self.root / "manifest.jsonl").read_text())
        self.assertEqual(row["metrics_status"], "unavailable")
        self.assertEqual(row["metrics_errors"], {"compute": "共通祖先がありません"})
        self.assertTrue(all(v is None for v in row["metrics_delta"].values()))

    def test_metric_failure_reaches_judge_text_and_review(self):
        (self.repo / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        self.commit("head")
        invoke(make_row, *self.row_args("--repo", self.repo, "--base", self.base, "--head", "HEAD",
                                        "--metrics-error", "radon crashed"))
        data = json.loads((self.root / "dataset.jsonl").read_text())
        self.assertIn("計測不能", data["metrics_delta"])
        self.assertIn("radon crashed", data["metrics_delta"])
        manifest = json.loads((self.root / "manifest.jsonl").read_text())
        self.assertEqual(manifest["metrics_status"], "unavailable")

    def test_metrics_and_metrics_error_are_mutually_exclusive(self):
        write_json(self.root / "m.json", {})
        with self.assertRaisesRegex(SystemExit, "同時"):
            invoke(make_row, *self.row_args("--metrics", self.root / "m.json", "--metrics-error", "x"))

    def test_diff_file_mode_verifies_changed_python_files(self):
        (self.repo / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_a.py").write_text("def test():\n    pass\n", encoding="utf-8")
        (self.repo / "README.md").write_text("doc\n", encoding="utf-8")
        self.commit("head")
        write_json(self.root / "metrics.json", metrics.compute(self.repo, self.base, "HEAD", metrics.DEFAULT_EXCLUDES, None))
        (self.root / "pr.diff").write_text(self.git("diff", self.base, "HEAD") + "\n", encoding="utf-8")
        invoke(make_row, *self.row_args("--diff", self.root / "pr.diff", "--metrics", self.root / "metrics.json"))
        self.assertTrue((self.root / "dataset.jsonl").exists())

        (self.repo / "c.py").write_text("x = 1\n", encoding="utf-8")
        self.commit("other")
        (self.root / "other.diff").write_text(self.git("diff", "HEAD~1", "HEAD") + "\n", encoding="utf-8")
        args = self.row_args("--diff", self.root / "other.diff", "--metrics", self.root / "metrics.json")
        args[args.index("r1")] = "r2"
        with self.assertRaisesRegex(SystemExit, "変更対象が一致しません"):
            invoke(make_row, *args)
        self.assertEqual(len((self.root / "manifest.jsonl").read_text().splitlines()), 1)

    def test_cache_prunes_stale_versions_and_unused_blobs(self):
        cache = self.root / "cache"
        cache.mkdir()
        (cache / "v1-deadbeef.json").write_text("{}", encoding="utf-8")
        (cache / f"v{metrics.ANALYZER_VERSION}-unused.json").write_text("{}", encoding="utf-8")
        result = metrics.compute(self.repo, self.base, "HEAD", metrics.DEFAULT_EXCLUDES, cache, prune_cache=True)
        names = sorted(p.name for p in cache.iterdir())
        blob = self.git("rev-parse", "HEAD:a.py")
        self.assertEqual(names, [f"v{metrics.ANALYZER_VERSION}-{blob}.json"])
        self.assertEqual(result["cache"]["pruned"], 1)

    def test_ci_does_not_stop_when_metrics_cannot_be_computed(self):
        workflow = (ROOT / "ci/llm-judge.yml").read_text()
        self.assertIn('if base_sha=$(git merge-base "$BASE" "$head_sha"); then', workflow)
        self.assertIn('extra+=(--metrics-error "$METRICS_ERROR")', workflow)
        self.assertIn("--prune-cache", workflow)
        self.assertIn("if ! python", workflow)


class TaskCompareTests(TemporaryTest):
    """llm-judge-compare.yml: Run ごとの成果物を集め、全方式が揃ったら比較する。"""

    NOW = collect_task.parse_time("2026-11-02T12:00:00Z")

    def setUp(self):
        super().setUp()
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.index = []

    def add_artifact(self, run_id, variant, *, task="T1", attempt=1, hard_gate="pass",
                     created="2026-11-02T10:00:00Z", judged=True, judge_version="2026-09-29", score=5):
        directory = self.artifacts / f"a{len(self.index)}"
        directory.mkdir()
        write_jsonl(directory / "manifest.jsonl", [
            {"run_id": run_id, "task_id": task, "variant": variant, "attempt": attempt, "hard_gate": hard_gate}])
        if hard_gate == "pass":
            # llm-judge.yml は Judge の前に評価データを作るので、採点に失敗しても runs.jsonl は残る
            write_jsonl(directory / "runs.jsonl", [
                {"run_id": run_id, "query": "R1", "response": f"diff-{run_id}", "ci_summary": "pass"}])
        if judged and hard_gate == "pass":
            results = directory / "results" / f"eval_{run_id}"
            results.mkdir(parents=True)
            trial = complete_trial(run_id)
            for r in trial["results"]:
                r["score"] = score
            write_jsonl(results / "trial1.jsonl", [trial])
            write_json(results / "run_meta.json", {
                "eval_id": f"eval_{run_id}", "judge": {"deployment": "judge", "version": judge_version},
                "evaluator_versions": {"code_pr_requirements": "2"}, "repeats": 3})
        self.index.append({"name": f"llm-judge-run-{task}-{run_id}", "id": len(self.index),
                           "created_at": created, "dir": directory.name})
        write_json(self.artifacts / "_index.json", self.index)

    def merge(self, variants=("a", "b"), attempts=1, timeout_hours=24, compare_condition=""):
        return collect_task.merge("T1", list(variants), attempts, timeout_hours, self.artifacts,
                                  self.root / "out", self.NOW, compare_condition)

    def test_judge_failure_is_not_counted_as_arrived(self):
        self.add_artifact("r1", "a")
        self.add_artifact("r2", "b", judged=False)
        result = self.merge()
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["judge_failed"], [{"variant": "b", "attempt": 1, "run_id": "r2"}])
        # 待ち時間を過ぎても、採点結果のない Run は比較に入れない（方式が 1 つしか残らない）
        result = self.merge(timeout_hours=1)
        self.assertEqual((result["status"], result["pairwise"]), ("insufficient", False))
        self.assertEqual([p["run_id"] for p in result["present"]], ["r1"])
        self.assertEqual(collect_task.load_jsonl(self.root / "out" / "runs.jsonl")[0]["run_id"], "r1")
        # 採点し直した成果物が届けば揃う
        self.add_artifact("r2", "b", created="2026-11-02T11:00:00Z")
        result = self.merge()
        self.assertEqual((result["status"], result["judge_failed"]), ("complete", []))
        self.assertEqual({p["state"] for p in result["present"]}, {"judged"})

    def test_rejudging_the_same_run_changes_the_marker(self):
        self.add_artifact("r1", "a")
        self.add_artifact("r2", "b")
        first = self.merge()["marker"]
        self.add_artifact("r2", "b", created="2026-11-02T11:00:00Z", score=3)
        second = self.merge()
        self.assertEqual([p["run_id"] for p in second["present"]], ["r1", "r2"])
        self.assertNotEqual(first, second["marker"])
        # 比較側の条件（gap やペア比較の Judge）が変わっても比較し直す
        self.assertNotEqual(second["marker"], self.merge(compare_condition="gap=10")["marker"])

    def test_different_evaluation_conditions_are_not_compared(self):
        self.add_artifact("r1", "a")
        self.add_artifact("r2", "b", judge_version="2027-01-01")
        result = self.merge()
        self.assertEqual(result["status"], "condition_mismatch")
        self.assertEqual(len(result["conditions"]), 2)
        self.assertNotIn(result["status"], collect_task.READY_STATUSES)

    def test_aggregate_refuses_to_mix_evaluation_conditions(self):
        self.add_artifact("r1", "a")
        self.add_artifact("r2", "b", judge_version="2027-01-01")
        dirs = sorted(self.artifacts.glob("*/results/*"))
        write_jsonl(self.root / "manifest.jsonl", [
            {"run_id": "r1", "task_id": "T1", "variant": "a"}, {"run_id": "r2", "task_id": "T1", "variant": "b"}])
        with self.assertRaisesRegex(SystemExit, "評価条件"):
            invoke(aggregate, "--manifest", self.root / "manifest.jsonl", "--out-dir", self.root / "agg", *dirs)
        invoke(aggregate, "--manifest", self.root / "manifest.jsonl", "--out-dir", self.root / "agg",
               "--allow-mixed-conditions", *dirs)
        conditions = json.loads((self.root / "agg" / "variants.json").read_text())["conditions"]
        self.assertEqual({c["condition"]["judge"] for c in conditions}, {"judge@2026-09-29", "judge@2027-01-01"})
        (dirs[0] / "run_meta.json").unlink()
        with self.assertRaisesRegex(SystemExit, "run_meta.json"):
            invoke(aggregate, "--manifest", self.root / "manifest.jsonl", "--out-dir", self.root / "agg", *dirs)

    def test_compare_condition_includes_pairwise_judge_and_prompt(self):
        condition = collect_task.compare_condition("gap=5")
        self.assertTrue(condition.startswith("gap=5;"))
        self.assertIn(f"pairwise_judge={config.JUDGE_DEPLOYMENT}@{config.JUDGE_MODEL_VERSION}", condition)
        self.assertIn("pairwise_prompt=", condition)

    def test_artifact_names_identify_the_task_even_when_run_ids_have_hyphens(self):
        self.assertEqual(collect_task.artifact_task("llm-judge-run-T002-r-8f3a"), "T002")
        self.assertEqual(collect_task.compare_task("llm-judge-compare-T002-0123abcd"), "T002")
        for name in ("llm-judge-T002-r1", "llm-judge-run-T002", "llm-judge-compare-T002-x"):
            self.assertIsNone(collect_task.artifact_task(name))

    def test_complete_task_is_merged_for_pairwise_and_aggregate(self):
        self.add_artifact("r1", "a")
        self.add_artifact("r2", "b")
        result = self.merge()
        self.assertEqual((result["status"], result["pairwise"], result["missing"]), ("complete", True, []))
        out = self.root / "out"
        self.assertEqual(sorted(r["run_id"] for r in collect_task.load_jsonl(out / "runs.jsonl")), ["r1", "r2"])
        dirs = (out / "results_dirs.txt").read_text().split()
        self.assertEqual(len(dirs), 2)
        invoke(aggregate, "--manifest", out / "manifest.jsonl", "--out-dir", out / "absolute", *dirs)
        summary = collect_task.load_jsonl(out / "absolute" / "summary.jsonl")
        self.assertEqual(len(summary), 2)
        # 同点なので方式間の総合点差が gap 未満として人間レビューに回る
        self.assertTrue(all(any("方式間の総合点差" in r for r in s["human_review"]) for s in summary))
        comparison = json.loads((out / "absolute" / "variants.json").read_text())["comparisons"][0]
        self.assertEqual(comparison["tasks"][0]["outcome"], "tie")

    def test_waiting_until_all_variants_arrive_or_timeout(self):
        self.add_artifact("r1", "a")
        self.assertEqual(self.merge()["status"], "waiting")
        # 待ち時間を過ぎても方式が 1 つしかなければ比較しない
        self.assertEqual(self.merge(timeout_hours=1)["status"], "insufficient")

    def test_timeout_compares_what_arrived_and_records_missing(self):
        self.add_artifact("r1", "a")
        self.add_artifact("r2", "b")
        self.add_artifact("r3", "a", attempt=2)
        self.assertEqual(self.merge(attempts=2)["status"], "waiting")
        result = self.merge(attempts=2, timeout_hours=1)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["missing"], [{"variant": "b", "attempt": 2}])
        self.assertTrue(result["pairwise"])

    def test_hard_gate_failure_counts_as_arrived_but_skips_pairwise(self):
        self.add_artifact("r1", "a")
        self.add_artifact("r2", "b", hard_gate="fail")
        result = self.merge()
        self.assertEqual((result["status"], result["pairwise"], result["absolute_results"]), ("complete", False, 1))
        out = self.root / "out"
        invoke(aggregate, "--manifest", out / "manifest.jsonl", "--out-dir", out / "absolute",
               *(out / "results_dirs.txt").read_text().split())
        comparison = json.loads((out / "absolute" / "variants.json").read_text())["comparisons"][0]
        self.assertEqual(comparison["tasks"][0]["outcome"], "a_only_passed")

    def test_reruns_use_latest_artifact_and_other_tasks_or_variants_are_ignored(self):
        self.add_artifact("r1", "a", hard_gate="fail", created="2026-11-02T09:00:00Z")
        self.add_artifact("r1", "a", created="2026-11-02T10:00:00Z")  # 同じ Run の再実行
        self.add_artifact("r2", "b", created="2026-11-02T10:00:00Z")
        self.add_artifact("r3", "b", created="2026-11-02T11:00:00Z")  # 同じ枠の作り直し
        self.add_artifact("r4", "c")
        self.add_artifact("r5", "a", task="T2")
        result = self.merge()
        self.assertEqual({(p["variant"], p["run_id"], p["hard_gate"]) for p in result["present"]},
                         {("a", "r1", "pass"), ("b", "r3", "pass")})
        self.assertEqual(result["superseded"], ["r2"])
        self.assertTrue(any("r4" in n for n in result["notes"]))

    def test_marker_changes_when_compared_runs_change(self):
        self.add_artifact("r1", "a")
        self.add_artifact("r2", "b")
        first = self.merge()["marker"]
        self.assertEqual(self.merge()["marker"], first)
        self.add_artifact("r3", "b", created="2026-11-02T11:00:00Z")
        second = self.merge()["marker"]
        self.assertNotEqual(first, second)
        self.assertTrue(second.startswith("llm-judge-compare-T1-"))

    def test_pending_tasks_skip_already_compared_and_old_tasks(self):
        artifacts = [
            {"name": "llm-judge-run-T1-r1", "created_at": "2026-11-02T10:00:00Z"},
            {"name": "llm-judge-compare-T1-abc", "created_at": "2026-11-02T11:00:00Z"},
            {"name": "llm-judge-run-T2-r2", "created_at": "2026-11-02T10:00:00Z"},
            {"name": "llm-judge-compare-T2-abc", "created_at": "2026-11-02T09:00:00Z"},
            {"name": "llm-judge-run-T3-r3", "created_at": "2026-11-02T10:00:00Z"},
            {"name": "llm-judge-run-T4-r4", "created_at": "2026-10-01T10:00:00Z"},
            {"name": "unrelated", "created_at": "2026-11-02T10:00:00Z"},
        ]
        since = self.NOW - collect_task.timedelta(days=14)
        self.assertEqual(collect_task.pending_tasks(artifacts, since), ["T2", "T3"])

    def test_aggregate_requires_out_dir_for_multiple_results(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            aggregate.parse_args(["--manifest", "m.jsonl", "d1", "d2"])
        args = aggregate.parse_args(["results/x", "m.jsonl"])
        self.assertEqual((args.results_dirs, args.manifest, args.out_dir),
                         ([Path("results/x")], Path("m.jsonl"), Path("results/x")))

    def test_publish_types_filter_avoids_resending_run_events(self):
        absolute = self.root / "absolute"
        absolute.mkdir()
        write_json(absolute / "run_meta.json", {"eval_id": "llm-judge-compare-T1-abc"})
        write_jsonl(absolute / "summary.jsonl", [{"run_id": "r1", "human_review": []}])
        write_json(absolute / "variants.json", {"variants": {"a": {}}, "comparisons": [{"a": "a", "b": "b"}]})
        manifest = self.root / "manifest.jsonl"
        write_jsonl(manifest, [{"run_id": "r1", "task_id": "T1", "variant": "a"}])
        out = io.StringIO()
        with patch.object(sys, "argv", ["publish_results.py", "--project", "shop", "--manifest", str(manifest),
                                        "--results-dir", str(absolute), "--types", "comparison", "--dry-run"]), \
                contextlib.redirect_stdout(out):
            publish_results.main()
        lines = [json.loads(l) for l in out.getvalue().splitlines() if l.startswith("{")]
        self.assertEqual([d["type"] for d in lines], ["comparison"])
        self.assertEqual(lines[0]["eval_id"], "llm-judge-compare-T1-abc")
        with self.assertRaisesRegex(SystemExit, "未知の種類"):
            publish_results.parse_types("comparison,unknown")

    def test_workflows_are_wired_together(self):
        judge = (ROOT / "ci/llm-judge.yml").read_text()
        self.assertIn("name: llm-judge-run-${{ inputs.task_id }}-${{ inputs.run_id }}", judge)
        self.assertIn("gh workflow run llm-judge-compare.yml", judge)
        self.assertIn("cp -r \"$results_dir\" out/results/", judge)
        self.assertLess(judge.index("actions/upload-artifact"), judge.index("gh workflow run llm-judge-compare.yml"))
        compare = (ROOT / "ci/llm-judge-compare.yml").read_text()
        for part in ("collect_task.py\" merge", "--types comparison,pairwise,pair",
                     "name: ${{ steps.collect.outputs.marker }}", "mv compare/review/pairs/_key.json compare/pairs_key.json",
                     "group: llm-judge-compare-${{ matrix.task_id }}"):
            self.assertIn(part, compare)
        # 比較済みの印は最後に残す（途中で失敗したら次の起動でやり直す）
        self.assertGreater(compare.index("actions/upload-artifact"), compare.index("gh issue create"))


if __name__ == "__main__":
    unittest.main()
