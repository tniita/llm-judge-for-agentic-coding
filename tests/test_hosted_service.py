"""Foundry Hosted Agent 版の評価サービス（hosted/src/llm-judge-service）の回帰テスト。

Foundry・GitHub・可視化基盤は偽物に差し替え、状態はテンポラリのファイルに置く。
HTTP の入口のテストは azure-ai-agentserver-invocations が入っている場合だけ実行する
（uv pip install -r <(python -c ...) などで hosted/src/llm-judge-service/pyproject.toml の依存を入れる）。
"""

import ast
import contextlib
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT / "hosted" / "src" / "llm-judge-service"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(SERVICE_DIR))

import config  # noqa: E402
import judge_client  # noqa: E402
import run_eval  # noqa: E402
import run_pairwise  # noqa: E402
import service as svc  # noqa: E402
from store import FileStore  # noqa: E402

try:
    import azure.ai.agentserver.invocations  # noqa: F401
    from starlette.testclient import TestClient
    HAS_AGENTSERVER = True
except ImportError:  # pragma: no cover
    HAS_AGENTSERVER = False

ABS_NAMES = run_eval.evaluator_names()
PAIR_NAME = config.PAIRWISE["name"]


class FakeJudge:
    """Foundry の評価の代わり。foundry_eval.run_evaluation と同じ形の結果ディレクトリを作る。"""

    def __init__(self, root: Path):
        self.root = root
        self.version, self.evaluator_version, self.rubric = "2026-09-29", "2", config.rubric_fingerprint()
        self.scores, self.pair_score, self.calls, self.fail = {}, 1, [], None

    def condition(self):
        return {"judge": {"deployment": "judge", "model": "judge", "version": self.version},
                "evaluator_versions": {n: self.evaluator_version for n in [*ABS_NAMES, PAIR_NAME]},
                "rubric": self.rubric}

    def _write(self, names, repeats, rows_per_trial):
        eval_id = f"eval_{len(self.calls) - 1}"
        out = self.root / eval_id
        out.mkdir(parents=True)
        for i in range(1, repeats + 1):
            (out / f"trial{i}.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows_per_trial), encoding="utf-8")
        (out / "run_meta.json").write_text(json.dumps({
            "eval_id": eval_id, "judge": {"deployment": "judge", "model": "judge", "version": self.version},
            "evaluator_versions": {n: self.evaluator_version for n in names}, "rubric": self.rubric,
            "repeats": repeats, "report_url": f"https://portal/{eval_id}"}), encoding="utf-8")
        return out

    def judge_rows(self, rows, *, name, repeats):
        self.calls.append(("judge", name))
        if self.fail:
            raise RuntimeError(self.fail)
        trial = [{
            # Foundry の出力は入力全体とプロンプトを含む。サービスは集計に必要な項目だけを残す
            "datasource_item": run_eval.to_item(row),
            "results": [{"name": key, "status": "completed", "score": self.scores.get(row["run_id"], 5),
                         "passed": True, "reason": "[confidence: high] R1: met", "sample": {"input": "x" * 1000}}
                        for key in config.EVALUATORS],
        } for row in rows]
        return self._write(ABS_NAMES, repeats, trial)

    def judge_pairs(self, pairs, dataset, *, name, repeats):
        """点数の高い実装を位置に関係なく選ぶ。同点なら pair_score（1 = 常に A を選ぶ → 順序で判定が逆転）。"""
        self.calls.append(("pairwise", name))

        def score(item):
            a, b = (self.scores.get(item[k].removeprefix("diff-"), 5) for k in ("response_a", "response_b"))
            return self.pair_score if a == b else 1 if a > b else 5
        trial = [{"datasource_item": item, "results": [
            {"name": "pairwise", "status": "completed", "score": score(item), "reason": "[confidence: high] A"}]}
            for item in run_pairwise.to_items(pairs, dataset)]
        return self._write([PAIR_NAME], repeats, trial)


class FakePublisher:
    def __init__(self):
        self.docs = []

    def publish(self, docs):
        self.docs += docs
        return len(docs)


class FakeRepo:
    def __init__(self):
        self.files, self.issues, self.comments = {}, {}, []

    def put_file(self, path, text, message):
        self.files[path] = text

    def get_file(self, path):
        return self.files.get(path)

    def create_issue(self, title, body):
        number = len(self.issues) + 1
        self.issues[number] = {"title": title, "body": body, "state": "open"}
        return {"number": number, "url": f"https://github.com/o/r/issues/{number}"}

    def issue_state(self, number):
        return self.issues[number]["state"]

    def comment(self, number, body):
        self.comments.append((number, body))

    def file_url(self, path):
        return f"https://github.com/o/r/tree/HEAD/{path}"


def row(run_id, task="T1"):
    return {"run_id": run_id, "query": f"{task} R1: x", "response": f"diff-{run_id}", "ci_summary": "pass",
            "pr_description": "p", "project_skills": "SKILL"}


def manifest(run_id, variant, task="T1", attempt=1, hard_gate="pass"):
    return {"run_id": run_id, "task_id": task, "variant": variant, "attempt": attempt, "hard_gate": hard_gate,
            "cost_usd": 0.1}


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="llm-judge-service-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)
        self.judge = FakeJudge(self.root / "foundry")
        self.publisher = FakePublisher()
        self.repo = None
        self.queue = []
        self.service = self.make_service()

    def make_service(self, repo=None):
        self.repo = repo
        return svc.Service(FileStore(self.root / "state"), self.judge, self.publisher, repo,
                           now=lambda: self.clock, start_job=self.queue.append, heartbeat_seconds=0)

    def drain(self):
        while self.queue:
            self.service.execute(self.queue.pop(0))

    def create(self, eid="exp1", **extra):
        body = {"op": "create_experiment", "experiment_id": eid, "project": "shop", "variants": ["fixed", "fusion"],
                **extra}
        return self.service.handle(body)

    def submit(self, run_id, variant, task="T1", eid="exp1", **kw):
        m = manifest(run_id, variant, task, **kw)
        body = {"op": "submit_run", "experiment_id": eid, "manifest": m,
                "row": row(run_id, task) if m["hard_gate"] == "pass" else None}
        return self.service.handle(body, invocation_id=f"inv-{run_id}-{len(self.queue)}")

    def run_doc(self, run_id, eid="exp1"):
        return self.service.store.get(eid, f"run:{run_id}")

    def task_doc(self, task="T1", eid="exp1"):
        return self.service.store.get(eid, f"task:{task}")


class ExperimentTests(ServiceTestBase):
    def test_experiment_fixes_definition_and_condition(self):
        status, body = self.create()
        self.assertEqual((status, body["created"]), (200, True))
        condition = body["experiment"]["condition"]
        self.assertEqual(condition["judge"]["version"], "2026-09-29")
        self.assertEqual(condition["rubric"], config.rubric_fingerprint())
        self.assertIn(PAIR_NAME, condition["evaluator_versions"])
        self.assertEqual(body["experiment"]["definition"]["judge_repeats"], 3)
        # 同じ定義なら既存を返し、評価条件は作り直さない
        self.judge.version = "2027-01-01"
        status, again = self.create()
        self.assertEqual((again["created"], again["experiment"]["condition"]["judge"]["version"]), (False, "2026-09-29"))
        with self.assertRaisesRegex(svc.ConflictError, "定義は変えられません"):
            self.create(gap=10)

    def test_invalid_definitions_are_rejected(self):
        with self.assertRaisesRegex(svc.ServiceError, "方式名"):
            self.create(eid="fixed-vs-fusion")
        with self.assertRaisesRegex(svc.ServiceError, "2 つ以上"):
            self.service.handle({"op": "create_experiment", "experiment_id": "e", "project": "p", "variants": ["a"]})
        with self.assertRaisesRegex(svc.ServiceError, "attempts"):
            self.create(attempts=0)
        with self.assertRaisesRegex(svc.NotFound, "base"):
            self.create(base_experiment_id="base")
        with self.assertRaisesRegex(svc.ServiceError, "op は"):
            self.service.handle({"op": "unknown"})

    def test_judge_version_check_failure_becomes_conflict(self):
        def stop():
            raise SystemExit("Judge のモデルバージョンが想定と異なります")
        self.judge.condition = stop
        with self.assertRaisesRegex(svc.ConflictError, "モデルバージョン"):
            self.create()


class JudgeAndCompareTests(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.create()

    def test_full_flow_from_submission_to_task_comparison(self):
        status, body = self.submit("r1", "fixed")
        self.assertEqual((status, body["status"], body["invocation_id"]), (202, "queued", "inv-r1-0"))
        self.submit("r2", "fusion")
        self.drain()

        for run_id in ("r1", "r2"):
            run = self.run_doc(run_id)
            self.assertEqual(run["state"], "judged")
            self.assertEqual(len(run["trials"]), 3)
            first = run["trials"][0][0]
            self.assertEqual(first["datasource_item"], {"run_id": run_id})
            self.assertNotIn("sample", first["results"][0])
            self.assertEqual(run["summary"]["total"], 100)

        job = self.service.job_status("inv-r1-0")
        self.assertEqual((job["status"], job["result"]["status"]), ("completed", "judged"))
        self.assertEqual(job["result"]["report_url"], "https://portal/eval_0")

        task = self.task_doc()
        self.assertEqual(task["status"], "complete")
        self.assertTrue(task["marker"].startswith("llm-judge-compare-T1-"))
        pair = task["pairwise"]["summary"]["pairs"][0]
        # 同点で、偽の Judge は両方の順序で A を選ぶ → 位置で判定が逆転し、優位度は 0
        self.assertEqual((pair["pref"], pair["position_inconsistent"]), (0.0, True))
        self.assertEqual(self.judge.calls[-1], ("pairwise", "exp1-T1-pairwise"))
        # 判定の逆転と、方式間の総合点差が gap 未満 → 人間レビュー（レビュー用リポジトリがないので資料を保存）
        review = self.service.store.get("exp1", task["review_id"])
        self.assertEqual((review["status"], review["runs"], review["pairs"]), ("manual", 2, 1))
        self.assertIn("pairs_review.csv", review["files"])
        self.assertNotIn("pairs/_key.json", review["files"])
        self.assertIn("review.csv", review["files"])
        self.assertFalse(any("fixed" in t or "fusion" in t for t in review["files"].values()))

        types = [d["type"] for d in self.publisher.docs]
        self.assertEqual(types.count("attempt"), 2)
        self.assertEqual(types.count("run"), 2)
        self.assertIn("comparison", types)
        self.assertIn("pair", types)
        self.assertNotIn("variant", types)
        self.assertTrue(all(d["experiment_id"] == "exp1" for d in self.publisher.docs))

    def test_resubmitting_the_same_run_is_deduplicated(self):
        self.submit("r1", "fixed")
        status, body = self.submit("r1", "fixed")
        self.assertEqual((status, body["deduplicated"], body["invocation_id"]), (202, True, "inv-r1-0"))
        self.assertEqual(len(self.queue), 1)
        self.drain()
        status, body = self.submit("r1", "fixed")
        self.assertEqual((status, body["state"], body["deduplicated"]), (200, "judged", True))
        self.assertEqual([c for c in self.judge.calls if c[0] == "judge"], [("judge", "exp1-r1")])

    def test_new_content_during_judging_supersedes_old_job(self):
        self.submit("r1", "fixed")
        first = self.queue.pop(0)
        m = manifest("r1", "fixed")
        self.service.handle({"op": "submit_run", "experiment_id": "exp1", "manifest": m,
                             "row": {**row("r1"), "response": "diff-v2"}}, invocation_id="inv-new")
        self.service.execute(first)
        self.assertEqual(self.service.job_status(first.removeprefix("job:"))["result"]["status"], "superseded")
        self.drain()
        self.assertEqual(self.run_doc("r1")["row"]["response"], "diff-v2")
        self.assertEqual(self.run_doc("r1")["state"], "judged")

    def test_validation_of_runs(self):
        with self.assertRaisesRegex(svc.ServiceError, "比較対象ではありません"):
            self.submit("r1", "other")
        with self.assertRaisesRegex(svc.ServiceError, "attempt"):
            self.submit("r1", "fixed", attempt=2)
        with self.assertRaisesRegex(svc.ServiceError, "task_id"):
            self.submit("r1", "fixed", task="T-1")
        with self.assertRaisesRegex(svc.ServiceError, "row"):
            self.service.handle({"op": "submit_run", "experiment_id": "exp1", "manifest": manifest("r1", "fixed")})
        with self.assertRaisesRegex(svc.NotFound, "create_experiment"):
            self.submit("r1", "fixed", eid="missing")

    def test_gate_failure_is_recorded_and_wins_for_the_other_variant(self):
        status, body = self.submit("r1", "fixed", hard_gate="fail")
        self.assertEqual((status, body["state"]), (200, "gate_failed"))
        self.assertEqual([d["type"] for d in self.publisher.docs], ["attempt"])
        self.submit("r2", "fusion")
        self.drain()
        task = self.task_doc()
        self.assertEqual(task["status"], "complete")
        self.assertIsNone(task["pairwise"])
        outcome = task["absolute"]["variants"]["comparisons"][0]["tasks"][0]["outcome"]
        self.assertEqual(outcome, "b_only_passed")
        self.assertNotIn(("pairwise", "exp1-T1-pairwise"), self.judge.calls)

    def test_compare_waits_then_times_out_and_is_not_repeated(self):
        self.submit("r1", "fixed")
        self.drain()
        self.assertEqual(self.task_doc()["status"], "waiting")
        self.clock += timedelta(hours=25)
        status, body = self.service.handle({"op": "compare_task", "experiment_id": "exp1", "task_id": "T1"}, "c1")
        self.drain()
        self.assertEqual(self.service.job_status("c1")["result"]["status"], "insufficient")
        self.submit("r2", "fusion")
        self.drain()
        marker = self.task_doc()["marker"]
        self.service.handle({"op": "compare_task", "experiment_id": "exp1", "task_id": "T1"}, "c2")
        self.drain()
        self.assertEqual(self.service.job_status("c2")["result"], {"task_id": "T1", "status": "already_compared",
                                                                   "marker": marker})
        pairwise_calls = [c for c in self.judge.calls if c[0] == "pairwise"]
        self.service.handle({"op": "compare_task", "experiment_id": "exp1", "task_id": "T1", "force": True}, "c3")
        self.drain()
        self.assertEqual(len([c for c in self.judge.calls if c[0] == "pairwise"]), len(pairwise_calls) + 1)

    def test_condition_change_stops_judging_and_sweep_retries(self):
        self.judge.version = "2027-01-01"
        self.submit("r1", "fixed")
        self.drain()
        run = self.run_doc("r1")
        self.assertEqual(run["state"], "judge_failed")
        self.assertIn("評価条件が実験 exp1 の作成時と異なる", run["error"])
        self.assertEqual(self.service.job_status("inv-r1-0")["status"], "failed")
        self.assertFalse(any(c[0] == "judge" for c in self.judge.calls))

        self.judge.version = "2026-09-29"
        self.service.handle({"op": "sweep", "experiment_id": "exp1"}, "s1")
        self.drain()
        self.assertEqual(self.service.job_status("s1")["result"]["experiments"]["exp1"]["retried_runs"], 1)
        self.assertEqual(self.run_doc("r1")["state"], "judged")
        self.assertEqual(self.run_doc("r1")["retries"], 1)

    def test_results_with_unexpected_condition_are_rejected(self):
        original = self.judge.judge_rows

        def drifted(rows, *, name, repeats):
            out = original(rows, name=name, repeats=repeats)
            meta = json.loads((out / "run_meta.json").read_text())
            meta["evaluator_versions"][ABS_NAMES[0]] = "9"
            (out / "run_meta.json").write_text(json.dumps(meta))
            return out
        self.judge.judge_rows = drifted
        self.submit("r1", "fixed")
        self.drain()
        self.assertIn("採点結果の評価条件が実験と異なります", self.run_doc("r1")["error"])

    def test_retries_are_bounded(self):
        self.judge.fail = "Foundry の一時的なエラー"
        self.submit("r1", "fixed")
        self.drain()
        for i in range(4):
            self.service.handle({"op": "sweep", "experiment_id": "exp1"}, f"s{i}")
            self.drain()
        self.assertEqual(self.run_doc("r1")["retries"], self.service.max_judge_retries)
        self.assertEqual(len([c for c in self.judge.calls if c[0] == "judge"]), 1 + self.service.max_judge_retries)

    def test_sweep_fails_stale_jobs_and_requeues_the_run(self):
        self.submit("r1", "fixed")
        job_id = self.queue.pop(0)
        job = self.service.store.find(job_id)
        job.update(status="running", heartbeat_at=svc.iso(self.clock))
        self.service.store.upsert(job)
        run = self.run_doc("r1")
        run["state"] = "judging"
        self.service.store.upsert(run)
        self.clock += timedelta(hours=1)
        self.service.handle({"op": "sweep"}, "s1")
        self.drain()
        result = self.service.job_status("s1")["result"]["experiments"]["exp1"]
        self.assertEqual((result["stale_jobs"], result["retried_runs"]), (1, 1))
        self.assertEqual(self.service.store.find(job_id)["status"], "failed")
        self.assertEqual(self.run_doc("r1")["state"], "judged")

    def test_queued_compare_jobs_are_deduplicated_but_running_ones_are_not(self):
        _, first = self.service.handle({"op": "compare_task", "experiment_id": "exp1", "task_id": "T1"}, "c1")
        _, second = self.service.handle({"op": "compare_task", "experiment_id": "exp1", "task_id": "T1"}, "c2")
        self.assertEqual((second["invocation_id"], second["deduplicated"]), ("c1", True))
        job = self.service.store.find("job:c1")
        job["status"] = "running"
        self.service.store.upsert(job)
        _, third = self.service.handle({"op": "compare_task", "experiment_id": "exp1", "task_id": "T1"}, "c3")
        self.assertEqual((third["invocation_id"], third["deduplicated"]), ("c3", False))

    def test_cancel_only_queued_jobs(self):
        self.service.handle({"op": "compare_task", "experiment_id": "exp1", "task_id": "T1"}, "c1")
        self.assertEqual(self.service.cancel("c1")["status"], "cancelled")
        self.drain()
        self.assertEqual(self.service.job_status("c1")["status"], "cancelled")
        self.assertIsNone(self.service.cancel("missing"))


class ReviewTests(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.service = self.make_service(FakeRepo())
        self.create()
        self.judge.pair_score = 3  # 同点は「差なし」と判定させる（ペア比較はレビュー対象にならず、総合点差が gap 未満で Run だけが対象）

    def fill(self, path, column, value):
        rows = list(csv.DictReader(io.StringIO(self.repo.files[path].lstrip("\ufeff"))))
        for r in rows:
            r[column] = value
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        self.repo.files[path] = out.getvalue()

    def test_review_repo_issue_and_ingestion(self):
        self.judge.pair_score = 1  # 両方の順序で A を選ぶ → 判定が逆転し、ペア比較も人間レビュー
        self.submit("r1", "fixed")
        self.submit("r2", "fusion")
        self.drain()
        review = self.service.store.get("exp1", self.task_doc()["review_id"])
        self.assertEqual((review["status"], review["issue_number"], review["pairs"], review["runs"]), ("open", 1, 1, 2))
        base = review["path"]
        self.assertTrue(base.startswith("reviews/exp1/T1/llm-judge-compare-T1-"))
        self.assertIn(f"{base}/pairs_review.csv", self.repo.files)
        self.assertIn(f"{base}/review.csv", self.repo.files)
        self.assertNotIn(f"{base}/pairs/_key.json", self.repo.files)
        issue = self.repo.issues[1]
        self.assertIn("ペア比較 1 組", issue["body"])
        for text in [issue["title"], issue["body"], *self.repo.files.values()]:
            self.assertNotIn("fixed", text)
            self.assertNotIn("fusion", text)

        # Issue が開いている間は取り込まない
        self.service.handle({"op": "ingest_reviews", "experiment_id": "exp1"}, "i1")
        self.drain()
        self.assertEqual(self.service.job_status("i1")["result"]["waiting"], 1)

        # 記入せずに閉じた → 取り込むものがないと 1 回だけコメント
        self.repo.issues[1]["state"] = "closed"
        for i in range(2):
            self.service.handle({"op": "ingest_reviews"}, f"i-empty-{i}")
            self.drain()
        self.assertEqual(self.service.store.get("exp1", review["id"])["status"], "empty")
        self.assertEqual(len(self.repo.comments), 1)

        self.fill(f"{base}/pairs_review.csv", "better", "A")
        self.fill(f"{base}/review.csv", "requirements", "4")
        self.service.handle({"op": "ingest_reviews"}, "i2")
        self.drain()
        stored = self.service.store.get("exp1", review["id"])
        self.assertEqual(stored["status"], "ingested")
        kinds = sorted(d["review_kind"] for d in stored["decisions"])
        self.assertEqual(kinds, ["pair", "run", "run"])
        pair_decision = next(d for d in stored["decisions"] if d["review_kind"] == "pair")
        self.assertIn(pair_decision["better"], ("fixed", "fusion"))
        self.assertTrue(any(d["type"] == "human_review" for d in self.publisher.docs))
        self.assertIn("3 件の判断を取り込みました", self.repo.comments[-1][1])

        report = self.service.report({"experiment_id": "exp1"})
        self.assertEqual(report["human_reviews"]["decisions"], 3)
        self.assertEqual(sum(report["human_reviews"]["pair_agreement_with_judge"].values()), 1)

    def test_issue_is_created_once_per_comparison(self):
        self.submit("r1", "fixed")
        self.submit("r2", "fusion")
        self.drain()
        self.service.handle({"op": "compare_task", "experiment_id": "exp1", "task_id": "T1", "force": True}, "c")
        self.drain()
        self.assertEqual(len(self.repo.issues), 1)


class RejudgeAndReportTests(ServiceTestBase):
    def test_rejudge_under_new_condition_and_cross_task_report(self):
        self.create()
        self.judge.scores = {"r1": 5, "r2": 3, "r3": 5, "r4": 2}
        for run_id, variant, task in (("r1", "fixed", "T1"), ("r2", "fusion", "T1"),
                                      ("r3", "fixed", "T2"), ("r4", "fusion", "T2")):
            self.submit(run_id, variant, task)
        self.drain()
        report = self.service.report({"experiment_id": "exp1"})
        comparison = report["absolute"]["comparisons"][0]
        self.assertEqual((comparison["a"], comparison["wins_a"], comparison["wins_b"]), ("fixed", 2, 0))
        self.assertEqual(report["pairwise"][0]["wins_a"], 2)
        self.assertIn("p_value_sign_test", report["pairwise"][0])
        self.assertEqual({t["status"] for t in report["tasks"]}, {"complete"})

        # Judge のモデルが更新された → 旧実験では採点できない。新しい実験で同じ Run を採点し直す
        self.judge.version = "2027-01-01"
        status, body = self.submit("r9", "fixed", task="T3")
        self.drain()
        self.assertEqual(self.run_doc("r9")["state"], "judge_failed")
        self.create(eid="exp2", base_experiment_id="exp1")
        status, body = self.service.handle({"op": "rejudge_experiment", "experiment_id": "exp2",
                                            "from_experiment_id": "exp1"}, "rj")
        self.assertEqual(status, 202)
        self.drain()
        result = self.service.job_status("rj")["result"]
        self.assertEqual(len(result["submitted"]), 5)
        runs = self.service.store.query("run", "exp2")
        self.assertEqual({r["state"] for r in runs}, {"judged"})
        self.assertTrue(all(r["run_meta"]["judge"]["version"] == "2027-01-01" for r in runs))
        report2 = self.service.report({"experiment_id": "exp2"})
        self.assertEqual(report2["experiment"]["condition"]["judge"]["version"], "2027-01-01")
        self.assertEqual(report2["experiment"]["definition"]["base_experiment_id"], "exp1")
        with self.assertRaisesRegex(svc.ServiceError, "別の実験"):
            self.service.handle({"op": "rejudge_experiment", "experiment_id": "exp2", "from_experiment_id": "exp2"})

    def test_get_experiment_and_review_packets(self):
        self.create()
        self.submit("r1", "fixed")
        self.submit("r2", "fusion")
        self.drain()
        status, body = self.service.handle({"op": "get_experiment", "experiment_id": "exp1"})
        self.assertEqual(status, 200)
        self.assertEqual({(r["variant"], r["state"]) for r in body["runs"]}, {("fixed", "judged"), ("fusion", "judged")})
        self.assertEqual(body["tasks"][0]["status"], "complete")
        self.assertEqual(body["active_jobs"], 0)
        _, packets = self.service.handle({"op": "review_packets", "experiment_id": "exp1"})
        self.assertEqual(len(packets["reviews"]), 1)
        self.assertIn("review.csv", packets["reviews"][0]["files"])


@unittest.skipUnless(HAS_AGENTSERVER, "azure-ai-agentserver-invocations が入っていません")
class HttpTests(ServiceTestBase):
    def setUp(self):
        super().setUp()
        import main
        self.service.start_job = None
        self.app = main.create_app(self.service, max_jobs=1)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def post(self, body, invocation_id=None):
        headers = {"x-agent-invocation-id": invocation_id} if invocation_id else {}
        return self.client.post("/invocations", json=body, headers=headers)

    def wait(self, invocation_id):
        for _ in range(200):
            res = self.client.get(f"/invocations/{invocation_id}")
            if res.json()["status"] in ("completed", "failed", "cancelled"):
                return res.json()
            import time
            time.sleep(0.02)
        self.fail("ジョブが終わりませんでした")

    def test_http_flow(self):
        res = self.post({"op": "create_experiment", "experiment_id": "exp1", "project": "shop",
                         "variants": ["fixed", "fusion"]})
        self.assertEqual(res.status_code, 200)
        res = self.post({"op": "submit_run", "experiment_id": "exp1", "manifest": manifest("r1", "fixed"),
                         "row": row("r1")}, invocation_id="inv-1")
        self.assertEqual((res.status_code, res.json()["invocation_id"]), (202, "inv-1"))
        job = self.wait("inv-1")
        self.assertEqual((job["status"], job["result"]["status"]), ("completed", "judged"))
        self.assertEqual(self.client.get("/invocations/missing").status_code, 404)
        self.assertEqual(self.client.post("/invocations", content=b"{not json").status_code, 400)
        self.assertEqual(self.post({"op": "nope"}).status_code, 400)
        self.assertEqual(self.post({"op": "get_experiment", "experiment_id": "none"}).status_code, 404)
        res = self.post({"op": "create_experiment", "experiment_id": "exp1", "project": "shop",
                         "variants": ["fixed", "fusion"], "gap": 9})
        self.assertEqual((res.status_code, res.json()["error"]), (409, "conflict"))
        spec = self.client.get("/invocations/docs/openapi.json").json()
        self.assertIn("submit_run", spec["paths"]["/invocations"]["post"]["requestBody"]["content"]
                      ["application/json"]["schema"]["properties"]["op"]["enum"])
        self.assertEqual(self.client.get("/readiness").status_code, 200)


class ClientTests(unittest.TestCase):
    def test_agent_url_token_and_session(self):
        with patch.object(judge_client.Client, "_token", staticmethod(lambda: "tok")):
            client = judge_client.Client(agent="llm-judge-service", session_id="s1",
                                         project_endpoint="https://a.services.ai.azure.com/api/projects/p/")
        self.assertEqual(client._url(), "https://a.services.ai.azure.com/api/projects/p/agents/llm-judge-service"
                                        "/endpoint/protocols/invocations?api-version=v1&agent_session_id=s1")
        self.assertEqual(client._url("/abc"), "https://a.services.ai.azure.com/api/projects/p/agents/llm-judge-service"
                                              "/endpoint/protocols/invocations/abc?api-version=v1&agent_session_id=s1")
        local = judge_client.Client(base_url="http://localhost:8088/")
        self.assertEqual(local._url("/x"), "http://localhost:8088/invocations/x")
        with self.assertRaises(SystemExit):
            judge_client.Client()

    def test_submit_sends_rows_only_for_passing_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "m.jsonl").write_text(json.dumps(manifest("r1", "fixed")) + "\n" +
                                         json.dumps(manifest("r2", "fusion", hard_gate="fail")) + "\n")
            (tmp / "d.jsonl").write_text(json.dumps(row("r1")) + "\n")
            sent = []

            class Fake:
                def invoke(self, body):
                    sent.append(body)
                    return 200, {"status": "completed", "run_id": body["manifest"]["run_id"]}
            args = SimpleArgs(experiment_id="e", dataset=tmp / "d.jsonl", manifest=tmp / "m.jsonl", wait=False)
            with contextlib.redirect_stdout(io.StringIO()), patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(tmp / "s.md")}):
                judge_client.cmd_submit(Fake(), args)
            self.assertEqual([b["row"] and b["row"]["run_id"] for b in sent], ["r1", None])
            summary = (tmp / "s.md").read_text()
            self.assertNotIn("fixed", summary)
            self.assertIn("r1", summary)

    def test_params_are_parsed_as_json_when_possible(self):
        self.assertEqual(judge_client.parse_params(["force=true", "task_id=T1", "n=3"]),
                         {"force": True, "task_id": "T1", "n": 3})


class SimpleArgs:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class PackagingTests(unittest.TestCase):
    def test_kit_copy_does_not_need_excluded_scripts(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sync_kit", ROOT / "hosted" / "sync_kit.py")
        sync_kit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sync_kit)
        excluded = {Path(n).stem for n in sync_kit.EXCLUDE}
        service_modules = [SERVICE_DIR / f for f in ("service.py", "adapters.py", "main.py", "store.py")]
        included = [p for p in (ROOT / "scripts").glob("*.py") if p.name not in sync_kit.EXCLUDE]
        for path in [*included, *service_modules]:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                for name in names:
                    self.assertNotIn(name.split(".")[0], excluded, f"{path.name} が {name} を使っています")

    def test_kit_only_service_imports_in_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "hosted"
            import shutil
            shutil.copytree(ROOT / "hosted", target, ignore=shutil.ignore_patterns("kit", ".state", "__pycache__"))
            shutil.copytree(ROOT / "scripts", Path(tmp) / "scripts")
            shutil.copytree(ROOT / "evaluators", Path(tmp) / "evaluators")
            subprocess.run([sys.executable, str(target / "sync_kit.py")], check=True, capture_output=True)
            shutil.rmtree(Path(tmp) / "scripts")  # リポジトリの scripts/ がなくても kit/ だけで動く
            code = ("import service, adapters, config; "
                    "assert 'kit' in config.__file__, config.__file__; print(config.rubric_fingerprint())")
            out = subprocess.run([sys.executable, "-c", code], cwd=target / "src" / "llm-judge-service",
                                 env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                 check=True, capture_output=True, text=True)
            self.assertEqual(out.stdout.strip(), config.rubric_fingerprint())

    def test_azure_yaml_declares_invocations_and_no_secrets(self):
        text = (ROOT / "hosted" / "azure.yaml").read_text()
        for part in ("host: azure.ai.agent", "kind: hosted", "protocol: invocations", "entryPoint: main.py",
                     "run: python3 sync_kit.py", "LLM_JUDGE_GITHUB_TOKEN_SECRET_URI",
                     "host: azure.ai.routine", "type: invoke_agent_invocations_api", "op: sweep",
                     "type: github_issue", "issue_event: closed", "op: ingest_reviews"):
            self.assertIn(part, text)
        self.assertNotIn("name: LLM_JUDGE_GITHUB_TOKEN\n", text)
        self.assertNotIn("FOUNDRY_PROJECT_ENDPOINT", text)

    def test_empty_azd_variables_fall_back_to_defaults(self):
        code = "import config; print(config.JUDGE_DEPLOYMENT, config.JUDGE_MODEL_VERSION, bool(config.PROJECT_ENDPOINT))"
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT / "scripts", capture_output=True, text=True,
                             env={**os.environ, "JUDGE_DEPLOYMENT": "", "JUDGE_MODEL_VERSION": "",
                                  "AZURE_AI_PROJECT_ENDPOINT": "", "FOUNDRY_PROJECT_ENDPOINT": ""}, check=True)
        self.assertEqual(out.stdout.split(), ["gpt-6.1-sol", "2026-09-29", "True"])


if __name__ == "__main__":
    unittest.main()
