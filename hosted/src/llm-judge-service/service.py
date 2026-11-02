"""LLM Judge の評価サービス（Foundry Hosted Agent の中身）。

GitHub Actions のワークフロー（ci/llm-judge.yml・ci/llm-judge-compare.yml）が行っていた処理を、
実験 ID 単位でまとめてサービスとして提供する。採点・集計・比較・レビュー資料の作成は scripts/ の関数をそのまま使う。

実験（experiment）:
  比較する方式・生成回数・待ち時間・gap などの定義と、作成時点の評価条件
  （Judge のデプロイとモデルバージョン・評価器のバージョン・ルーブリックのハッシュ・採点回数）を固定する。
  採点の前と後で評価条件を照合し、変わっていれば採点しない（同じ実験の中で物差しが混ざらない）。
  モデルを更新したら新しい実験を作り、rejudge_experiment で同じ Run を新しい条件で採点し直して比べる。

操作（POST /invocations の本文 {"op": ..., ...}）:
  create_experiment   実験を作る（同じ定義なら既存を返す。定義が違えば 409）            同期
  get_experiment      実験の定義・評価条件・Run とタスクの状態                          同期
  submit_run          Run を登録する。Hard Gate 不合格なら記録だけ、合格なら採点を始める   同期 / 202
  compare_task        タスクの方式比較（全方式が揃っていなければ何もしない）            202
  sweep               止まったジョブの片付け・採点のやり直し・未比較タスクの比較・レビューの取り込み  202
  ingest_reviews      レビュー用リポジトリで閉じられた Issue の判断を取り込む          202
  rejudge_experiment  別の実験の Run を、この実験の評価条件で採点し直す                202
  report              タスクをまたいだ方式比較（絶対評価・ペア比較・人間の判断）        同期
  review_packets      レビュー用リポジトリを使わない場合のレビュー資料                  同期
202 を返した操作は GET /invocations/{invocation_id} で状態と結果を取得する。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import tempfile
import threading
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from adapters import load_kit

load_kit()

import aggregate  # noqa: E402
import calibration  # noqa: E402
import collect_task  # noqa: E402
import config  # noqa: E402
import publish_results  # noqa: E402
import run_eval  # noqa: E402
import run_pairwise  # noqa: E402
from stats_utils import mean, sign_test  # noqa: E402
from store import Conflict, Store  # noqa: E402

EXPERIMENT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
VARIANT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
DEFAULTS = {"attempts": 1, "timeout_hours": 24.0, "gap": 5.0, "spread": 2.0, "judge_repeats": 3, "pairwise_repeats": 1}
TRIAL_RESULT_KEYS = ("name", "status", "score", "passed", "reason", "label", "threshold")
ACTIVE = ("queued", "running")
SERVICE_PARTITION = "_service"
# レビュー用リポジトリを使わない場合に、資料を状態の保存先に残す上限（Cosmos DB の文書は 2 MB まで）
MAX_STORED_PACKET_BYTES = 1_500_000
OPS_SYNC = ("create_experiment", "get_experiment", "report", "review_packets")
OPS_ASYNC = ("compare_task", "sweep", "ingest_reviews", "rejudge_experiment")


class ServiceError(Exception):
    status, code = 400, "invalid_request"


class NotFound(ServiceError):
    status, code = 404, "not_found"


class ConflictError(ServiceError):
    status, code = 409, "conflict"


class JudgeOutputError(RuntimeError):
    """Foundry の採点結果が欠けている（採点回数が足りない・点数のない評価器がある）。採点失敗として採点し直す。"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return path


def write_json(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def quiet(fn, *args, **kwargs):
    """scripts/ の関数は進捗を標準出力に出すので、サービスでは捨てる。"""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


def compact_trials(results_dir: Path, key: str) -> list[list[dict]]:
    """Foundry の出力から集計に使う項目だけを残す（入力全体やプロンプトを含むため、そのままでは大きい）。"""
    trials = []
    for path in sorted(results_dir.glob("trial*.jsonl"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0)):
        trials.append([
            {"datasource_item": {key: row.get("datasource_item", {}).get(key)},
             "results": [{k: r[k] for k in TRIAL_RESULT_KEYS if k in r} for r in row.get("results", [])]}
            for row in load_jsonl(path)
        ])
    return trials


def usable(result: dict) -> bool:
    return result.get("status", "completed") == "completed" and (
        result.get("score") is not None or result.get("passed") is not None)


def check_trials(trials: list[list[dict]], *, key: str, repeats: int, expected: dict[str, list[str]],
                 group=lambda item_key: item_key) -> None:
    """採点結果がそろっているかを確かめる。

    - 試行の数が採点回数と同じで、どの試行にも全項目の行がある
    - 項目（group でまとめたもの）× 評価器ごとに、点数のある結果が 1 つ以上ある
    一部の試行だけが失敗したものは、集計（aggregate.py / run_pairwise.py）が人間レビューに回すのでここでは通す。
    """
    if len(trials) != repeats:
        raise JudgeOutputError(f"採点結果の試行が {len(trials)} 回分しかありません（{repeats} 回必要）")
    counts: Counter = Counter()
    for i, rows in enumerate(trials, start=1):
        keys = {row.get("datasource_item", {}).get(key) for row in rows}
        missing_rows = sorted(set(expected) - keys)
        if missing_rows:
            raise JudgeOutputError(f"試行 {i} に {missing_rows} の採点結果がありません")
        for row in rows:
            item = row["datasource_item"].get(key)
            for result in row.get("results", []):
                if usable(result):
                    counts[(group(item), result.get("name"))] += 1
    groups = {group(k): names for k, names in expected.items()}
    empty = sorted(f"{g}/{n}" for g, names in groups.items() for n in names if counts[(g, n)] == 0)
    if empty:
        raise JudgeOutputError(f"点数のない採点結果があります: {empty}")


def write_results(directory: Path, trials: list[list[dict]], run_meta: dict) -> Path:
    for i, rows in enumerate(trials, start=1):
        write_jsonl(directory / f"trial{i}.jsonl", rows)
    write_json(directory / "run_meta.json", run_meta)
    return directory


class Service:
    def __init__(self, store: Store, judge, publisher=None, review_repo=None, *, now=utcnow,
                 start_job=None, stale_after=timedelta(minutes=45), max_judge_retries=2,
                 heartbeat_seconds: float = 60):
        self.store, self.judge, self.publisher, self.review_repo = store, judge, publisher, review_repo
        self.now = now
        self.start_job = start_job or (lambda job_id: None)
        self.stale_after, self.max_judge_retries, self.heartbeat_seconds = stale_after, max_judge_retries, heartbeat_seconds
        self._task_locks: dict[tuple[str, str], threading.Lock] = defaultdict(threading.Lock)
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------------ 入口

    def handle(self, body: dict, invocation_id: str | None = None) -> tuple[int, dict]:
        op = body.get("op")
        if op == "submit_run":
            return self.submit_run(body, invocation_id)
        if op in OPS_SYNC:
            return 200, getattr(self, op)(body)
        if op in OPS_ASYNC:
            return self._accept(op, body, invocation_id)
        raise ServiceError(f"op は {['submit_run', *OPS_SYNC, *OPS_ASYNC]} のどれかを指定してください: {op!r}")

    def job_status(self, invocation_id: str) -> dict | None:
        job = self.store.find(f"job:{invocation_id}")
        return None if job is None else self._job_view(job)

    def cancel(self, invocation_id: str) -> dict | None:
        job = self.store.find(f"job:{invocation_id}")
        if job is None:
            return None
        if job["status"] == "queued":
            job.update(status="cancelled", finished_at=iso(self.now()))
            self.store.upsert(job)
        return self._job_view(job)

    @staticmethod
    def _job_view(job: dict) -> dict:
        return {k: job.get(k) for k in ("invocation_id", "op", "experiment_id", "status", "result", "error",
                                         "created_at", "started_at", "finished_at")}

    # ------------------------------------------------------------------ 実験

    def _experiment(self, experiment_id) -> dict:
        if not isinstance(experiment_id, str) or not EXPERIMENT_ID_RE.match(experiment_id):
            raise ServiceError("experiment_id には英数字・_・.・- だけを使ってください（64 文字まで）")
        exp = self.store.get(experiment_id, "experiment")
        if exp is None:
            raise NotFound(f"実験 {experiment_id} がありません。create_experiment で作ってください")
        return exp

    def _definition(self, body: dict) -> dict:
        eid = body.get("experiment_id")
        if not isinstance(eid, str) or not EXPERIMENT_ID_RE.match(eid):
            raise ServiceError("experiment_id には英数字・_・.・- だけを使ってください（64 文字まで）")
        project = body.get("project")
        if not isinstance(project, str) or not project.strip():
            raise ServiceError("project（可視化基盤での絞り込みに使うプロジェクト名）が必要です")
        variants = body.get("variants")
        if (not isinstance(variants, list) or len(variants) < 2 or len(set(variants)) != len(variants)
                or not all(isinstance(v, str) and VARIANT_RE.match(v) for v in variants)):
            raise ServiceError("variants には比較する方式を 2 つ以上（英数字・_・.・-、重複なし）指定してください")
        # 実験 ID はレビュー用リポジトリの Issue に載るので、方式名を含めるとブラインドが崩れる
        leaked = [v for v in variants if v.lower() in eid.lower()]
        if leaked:
            raise ServiceError(f"experiment_id に方式名 {leaked} を含めないでください（レビュー資料に載るため）")
        d = {"experiment_id": eid, "project": project.strip(), "variants": variants}
        for key, default in DEFAULTS.items():
            value = body.get(key, default)
            try:
                value = type(default)(value)
            except (TypeError, ValueError):
                raise ServiceError(f"{key} の値が不正です: {value!r}") from None
            if value < 0 or (key != "gap" and value == 0):
                raise ServiceError(f"{key} の値が不正です: {value!r}")
            d[key] = value
        d["description"] = str(body.get("description") or "")
        base = body.get("base_experiment_id")
        if base:
            self._experiment(base)
        d["base_experiment_id"] = base or None
        return d

    def create_experiment(self, body: dict) -> dict:
        definition = self._definition(body)
        eid = definition["experiment_id"]
        existing = self.store.get(eid, "experiment")
        if existing is not None:
            if existing["definition"] != definition:
                raise ConflictError("実験の定義は変えられません。新しい experiment_id で作り直してください"
                                    f"（既存: {json.dumps(existing['definition'], ensure_ascii=False)}）")
            return {"experiment": existing, "created": False}
        doc = {"id": "experiment", "kind": "experiment", "experiment_id": eid, "definition": definition,
               "condition": self._current_condition(), "created_at": iso(self.now()), "status": "active"}
        try:
            self.store.create(doc)
        except Conflict:
            return self.create_experiment(body)
        return {"experiment": doc, "created": True}

    def get_experiment(self, body: dict) -> dict:
        exp = self._experiment(body.get("experiment_id"))
        eid = exp["experiment_id"]
        runs = self.store.query("run", eid)
        states = Counter((r["variant"], r["state"]) for r in runs)
        return {
            "experiment": exp,
            "runs": [{"variant": v, "state": s, "count": n} for (v, s), n in sorted(states.items())],
            "tasks": [{k: t.get(k) for k in ("task_id", "status", "marker", "compared_at", "checked_at", "review_id")}
                      for t in sorted(self.store.query("task", eid), key=lambda t: t["task_id"])],
            "active_jobs": sum(len(self.store.query("job", eid, status=s)) for s in ACTIVE),
        }

    # ------------------------------------------------------------------ 評価条件

    def _current_condition(self) -> dict:
        try:
            return self.judge.condition()
        except SystemExit as exc:
            # foundry_eval.check_judge は Judge のバージョンが想定と違うと SystemExit で止める
            raise ConflictError(str(exc)) from None

    def _check_condition(self, exp: dict) -> None:
        current, fixed = self._current_condition(), exp["condition"]
        diffs = []
        for key in ("deployment", "version"):
            if current["judge"].get(key) != fixed["judge"].get(key):
                diffs.append(f"Judge の {key}: {fixed['judge'].get(key)} → {current['judge'].get(key)}")
        for name, version in fixed["evaluator_versions"].items():
            if current["evaluator_versions"].get(name) != version:
                diffs.append(f"評価器 {name}: {version} → {current['evaluator_versions'].get(name)}")
        if current.get("rubric") != fixed.get("rubric"):
            diffs.append(f"ルーブリック: {fixed.get('rubric')} → {current.get('rubric')}")
        if diffs:
            raise ConflictError(f"評価条件が実験 {exp['experiment_id']} の作成時と異なるため採点しません: "
                                + " / ".join(diffs) + "。新しい実験を作り、rejudge_experiment で採点し直してください")

    @staticmethod
    def _expected_meta(exp: dict, *, absolute: bool) -> dict:
        c, d = exp["condition"], exp["definition"]
        names = run_eval.evaluator_names() if absolute else [config.PAIRWISE["name"]]
        return {"judge": f"{c['judge']['deployment']}@{c['judge']['version']}",
                "evaluator_versions": dict(sorted((n, c["evaluator_versions"].get(n)) for n in names)),
                "rubric": c.get("rubric"),
                "repeats": d["judge_repeats"] if absolute else d["pairwise_repeats"]}

    def _verify_results(self, results_dir: Path, exp: dict, *, absolute: bool) -> None:
        actual, expected = aggregate.evaluation_condition(results_dir), self._expected_meta(exp, absolute=absolute)
        if actual != expected:
            raise ConflictError(f"採点結果の評価条件が実験と異なります: {actual} ≠ {expected}")

    # ------------------------------------------------------------------ ジョブ

    def _active_job(self, experiment_id: str, key: str, statuses=ACTIVE) -> dict | None:
        lock = self.store.get(experiment_id, f"lock:{key}")
        if lock is None:
            return None
        job = self.store.get(experiment_id, lock["job_id"])
        if job is None or job["status"] not in statuses:
            return None
        # 止まったジョブを重複判定に使うと、同じ操作が二度と始まらない（sweep 自身が止まった場合も含む）
        if self._is_stale(job):
            self._fail_stale(job)
            return None
        return job

    def _is_stale(self, job: dict) -> bool:
        last = parse_iso(job.get("heartbeat_at") or job["created_at"])
        return job["status"] in ACTIVE and self.now() - last > self.stale_after

    def _fail_stale(self, job: dict) -> None:
        now = iso(self.now())
        job.update(status="failed", error="中断されました（ハートビートが途絶えたか、開始されませんでした）",
                   finished_at=now, updated_at=now)
        self.store.upsert(job)
        if job["op"] == "judge":
            self._mark_judge_failed(job["experiment_id"], job["params"], job["error"])

    def _enqueue(self, op: str, experiment_id: str, key: str, params: dict, invocation_id: str | None = None,
                 dedupe_statuses=ACTIVE) -> tuple[dict, bool]:
        active = self._active_job(experiment_id, key, dedupe_statuses)
        if active is not None:
            return active, False
        invocation_id = invocation_id or uuid.uuid4().hex
        now = iso(self.now())
        job = {"id": f"job:{invocation_id}", "kind": "job", "experiment_id": experiment_id,
               "invocation_id": invocation_id, "op": op, "key": key, "params": params, "status": "queued",
               "created_at": now, "updated_at": now, "started_at": None, "finished_at": None,
               "heartbeat_at": None, "result": None, "error": None}
        self.store.create(job)
        self.store.upsert({"id": f"lock:{key}", "kind": "lock", "experiment_id": experiment_id, "key": key,
                           "job_id": job["id"]})
        self.start_job(job["id"])
        return job, True

    def _accept(self, op: str, body: dict, invocation_id: str | None) -> tuple[int, dict]:
        if op in ("sweep", "ingest_reviews") and not body.get("experiment_id"):
            eid, key, params = SERVICE_PARTITION, op, {}
        else:
            exp = self._experiment(body.get("experiment_id"))
            eid = exp["experiment_id"]
            if op == "compare_task":
                task_id = body.get("task_id")
                if not isinstance(task_id, str) or not collect_task.TASK_ID_RE.match(task_id):
                    raise ServiceError("task_id には英数字・_・. だけを使ってください")
                key, params = f"compare:{task_id}", {"task_id": task_id, "force": bool(body.get("force"))}
            elif op == "rejudge_experiment":
                source = self._experiment(body.get("from_experiment_id"))["experiment_id"]
                if source == eid:
                    raise ServiceError("from_experiment_id には別の実験を指定してください")
                key, params = f"rejudge:{source}", {"from_experiment_id": source}
            else:
                key, params = op, {}
        # 比較は実行中のものとは重複させない（実行中に届いた Run を取りこぼさないよう、終わった後にもう一度比較する）
        dedupe = ("queued",) if op == "compare_task" else ACTIVE
        job, created = self._enqueue(op, eid, key, params, invocation_id, dedupe)
        return 202, {"invocation_id": job["invocation_id"], "op": op, "status": job["status"],
                     "deduplicated": not created}

    def execute(self, job_doc_id: str) -> None:
        job = self.store.find(job_doc_id)
        if job is None or job["status"] != "queued":
            return
        job.update(status="running", started_at=iso(self.now()), heartbeat_at=iso(self.now()))
        self.store.upsert(job)
        stop = threading.Event()
        beat = threading.Thread(target=self._heartbeat, args=(job["experiment_id"], job["id"], stop), daemon=True)
        if self.heartbeat_seconds > 0:
            beat.start()
        handlers = {"judge": self._run_judge, "compare_task": self._run_compare, "sweep": self._run_sweep,
                    "ingest_reviews": self._run_ingest, "rejudge_experiment": self._run_rejudge}
        try:
            result, status, error = handlers[job["op"]](job), "completed", None
        except (Exception, SystemExit) as exc:  # noqa: BLE001 — ジョブの失敗として記録する
            result, status, error = None, "failed", f"{type(exc).__name__}: {exc}"[:4000]
            if job["op"] == "judge":
                self._mark_judge_failed(job["experiment_id"], job["params"], error)
        finally:
            stop.set()
            if beat.is_alive():
                beat.join()
        now = iso(self.now())
        job.update(status=status, result=result, error=error, finished_at=now, updated_at=now, heartbeat_at=now)
        self.store.upsert(job)

    def _heartbeat(self, experiment_id: str, job_doc_id: str, stop: threading.Event) -> None:
        while not stop.wait(self.heartbeat_seconds):
            current = self.store.get(experiment_id, job_doc_id)
            # 片付けられた（failed になった）ジョブを running に戻さない
            if current is None or current["status"] != "running":
                return
            current.update(heartbeat_at=iso(self.now()), updated_at=iso(self.now()))
            self.store.upsert(current)

    # ------------------------------------------------------------------ Run の登録と採点

    def submit_run(self, body: dict, invocation_id: str | None = None) -> tuple[int, dict]:
        exp = self._experiment(body.get("experiment_id"))
        eid, d = exp["experiment_id"], exp["definition"]
        manifest, row = body.get("manifest"), body.get("row")
        if not isinstance(manifest, dict):
            raise ServiceError("manifest（make_row.py が対応表に書く 1 行）が必要です")
        run_id, task_id, variant = manifest.get("run_id"), manifest.get("task_id"), manifest.get("variant")
        if not isinstance(run_id, str) or not collect_task.RUN_ID_RE.match(run_id):
            raise ServiceError("run_id には英数字・_・.・- だけを使ってください")
        if not isinstance(task_id, str) or not collect_task.TASK_ID_RE.match(task_id):
            raise ServiceError("task_id には英数字・_・. だけを使ってください")
        if variant not in d["variants"]:
            raise ServiceError(f"方式 {variant!r} は実験 {eid} の比較対象ではありません: {d['variants']}")
        try:
            attempt = int(manifest.get("attempt") or 1)
        except (TypeError, ValueError):
            raise ServiceError("attempt は整数で指定してください") from None
        if not 1 <= attempt <= d["attempts"]:
            raise ServiceError(f"attempt は 1〜{d['attempts']} で指定してください")
        hard_gate = manifest.get("hard_gate", "pass")
        if hard_gate not in ("pass", "fail"):
            raise ServiceError("hard_gate は pass か fail です")
        if hard_gate == "pass":
            if not isinstance(row, dict) or row.get("run_id") != run_id:
                raise ServiceError("Hard Gate 合格の Run には、同じ run_id の評価データ（row）が必要です")
        else:
            row = None
        manifest = {**manifest, "attempt": attempt, "hard_gate": hard_gate}
        input_hash = digest({"manifest": manifest, "row": row})
        doc_id = f"run:{run_id}"
        existing = self.store.get(eid, doc_id)
        if existing and existing["input_hash"] == input_hash:
            if existing["state"] in ("judged", "gate_failed"):
                # 採点は済んでいても、可視化基盤への送信が残っていれば送り直す
                publish = self._deliver(eid, doc_id) if existing.get("publish_status") == "pending" else None
                return 200, {"run_id": run_id, "state": existing["state"], "status": "completed", "deduplicated": True,
                             "published": publish or self._publish_view(existing)}
            active = self._active_job(eid, f"judge:{run_id}:{input_hash}")
            if active is not None:
                return 202, {"invocation_id": active["invocation_id"], "op": "judge", "run_id": run_id,
                             "status": active["status"], "deduplicated": True}
        now = iso(self.now())
        run = {"id": doc_id, "kind": "run", "experiment_id": eid, "run_id": run_id, "task_id": task_id,
               "variant": variant, "attempt": attempt, "hard_gate": hard_gate, "manifest": manifest, "row": row,
               "input_hash": input_hash, "state": "gate_failed" if hard_gate == "fail" else "queued",
               "submitted_at": (existing or {}).get("submitted_at") or now, "updated_at": now,
               "retries": (existing or {}).get("retries", 0) if existing and existing["input_hash"] == input_hash else 0,
               "eval_id": None, "run_meta": None, "trials": None, "summary": None, "error": None,
               "publish": None, "publish_status": None}
        if hard_gate == "fail":
            self._stage_publish(run, exp, publish_results.build_documents(d["project"], [manifest], None), {"attempt"})
        self.store.upsert(run)
        if hard_gate == "fail":
            published = self._deliver(eid, doc_id)
            self._spawn_compare(eid, task_id)
            return 200, {"run_id": run_id, "state": "gate_failed", "status": "completed", "deduplicated": False,
                         "published": published}
        job, _ = self._enqueue("judge", eid, f"judge:{run_id}:{input_hash}",
                               {"run_id": run_id, "input_hash": input_hash}, invocation_id)
        return 202, {"invocation_id": job["invocation_id"], "op": "judge", "run_id": run_id,
                     "status": job["status"], "deduplicated": False}

    def _mark_judge_failed(self, eid: str, params: dict, error: str) -> None:
        run = self.store.get(eid, f"run:{params.get('run_id')}")
        if run and run["input_hash"] == params.get("input_hash") and run["state"] in ("queued", "judging"):
            run.update(state="judge_failed", error=error, updated_at=iso(self.now()))
            self.store.upsert(run)

    def _run_judge(self, job: dict) -> dict:
        eid, params = job["experiment_id"], job["params"]
        exp = self._experiment(eid)
        run = self.store.get(eid, f"run:{params['run_id']}")
        if run is None or run["input_hash"] != params["input_hash"]:
            return {"status": "superseded", "message": "同じ Run の新しい内容が登録されたため採点しません"}
        self._check_condition(exp)
        run.update(state="judging", updated_at=iso(self.now()))
        self.store.upsert(run)

        results_dir = self.judge.judge_rows([run["row"]], name=f"{eid}-{run['run_id']}",
                                            repeats=exp["definition"]["judge_repeats"])
        self._verify_results(results_dir, exp, absolute=True)
        run_meta = json.loads((results_dir / "run_meta.json").read_text(encoding="utf-8"))
        trials = compact_trials(results_dir, "run_id")
        check_trials(trials, key="run_id", repeats=exp["definition"]["judge_repeats"],
                     expected={run["run_id"]: list(config.EVALUATORS)})
        with tempfile.TemporaryDirectory(prefix="llm-judge-") as tmp:
            tmp = Path(tmp)
            results = write_results(tmp / "results" / run_meta["eval_id"], trials, run_meta)
            manifest_path = write_jsonl(tmp / "manifest.jsonl", [run["manifest"]])
            out = tmp / "run"
            quiet(aggregate.main, ["--manifest", str(manifest_path), "--out-dir", str(out),
                                   "--spread", str(exp["definition"]["spread"]), str(results)])
            write_json(out / "run_meta.json", run_meta)
            summary = next(iter(load_jsonl(out / "summary.jsonl")), None)
            docs = publish_results.build_documents(exp["definition"]["project"], [run["manifest"]], out)

        latest = self.store.get(eid, run["id"])
        if latest is None or latest["input_hash"] != params["input_hash"]:
            return {"status": "superseded", "message": "採点中に同じ Run の新しい内容が登録されました"}
        latest.update(state="judged", eval_id=run_meta["eval_id"], run_meta=run_meta, trials=trials,
                      summary=summary, error=None, judged_at=iso(self.now()), updated_at=iso(self.now()))
        # 送る内容を採点結果と同じ文書に保存してから送る（送信に失敗しても sweep と再登録で送り直せる）
        self._stage_publish(latest, exp, docs, {"attempt", "run"})
        self.store.upsert(latest)
        published = self._deliver(eid, latest["id"])
        self._spawn_compare(eid, latest["task_id"])
        return {"status": "judged", "run_id": latest["run_id"], "eval_id": run_meta["eval_id"],
                "total": (summary or {}).get("total"), "human_review": (summary or {}).get("human_review"),
                "report_url": run_meta.get("report_url"), "published": published}

    # ------------------------------------------------------------------ タスクの比較

    def _spawn_compare(self, eid: str, task_id: str) -> None:
        self._enqueue("compare_task", eid, f"compare:{task_id}", {"task_id": task_id, "force": False},
                      dedupe_statuses=("queued",))

    def _task_lock(self, eid: str, task_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._task_locks[(eid, task_id)]

    def _compare_condition(self, exp: dict) -> str:
        d = exp["definition"]
        return collect_task.compare_condition(
            f"experiment={d['experiment_id']};gap={d['gap']};spread={d['spread']};pairwise_repeats={d['pairwise_repeats']}")

    @staticmethod
    def _materialize(runs: list[dict], root: Path) -> Path:
        """Run を llm-judge.yml の成果物と同じ形に書き出す（collect_task.merge をそのまま使うため）。"""
        artifacts, index = root / "artifacts", []
        for i, run in enumerate(sorted(runs, key=lambda r: (r["submitted_at"], r["run_id"]))):
            directory = artifacts / f"r{i}"
            write_jsonl(directory / "manifest.jsonl", [run["manifest"]])
            if run.get("row") is not None:
                write_jsonl(directory / "runs.jsonl", [run["row"]])
            if run["state"] == "judged":
                write_results(directory / "results" / run["eval_id"], run["trials"], run["run_meta"])
            index.append({"name": f"{collect_task.RUN_PREFIX}{run['task_id']}-{run['run_id']}", "id": i,
                          "created_at": run["submitted_at"], "dir": directory.name})
        write_json(artifacts / "_index.json", index)
        return artifacts

    def _run_compare(self, job: dict) -> dict:
        exp = self._experiment(job["experiment_id"])
        return self._compare_task(exp, job["params"]["task_id"], force=job["params"].get("force", False))

    def _compare_task(self, exp: dict, task_id: str, *, force: bool = False) -> dict:
        eid, d = exp["experiment_id"], exp["definition"]
        with self._task_lock(eid, task_id):
            task = self.store.get(eid, f"task:{task_id}") or {
                "id": f"task:{task_id}", "kind": "task", "experiment_id": eid, "task_id": task_id, "markers": []}
            with tempfile.TemporaryDirectory(prefix="llm-judge-") as tmp:
                tmp = Path(tmp)
                artifacts = self._materialize(self.store.query("run", eid, task_id=task_id), tmp)
                out = tmp / "compare"
                collect = collect_task.merge(task_id, d["variants"], d["attempts"], d["timeout_hours"],
                                             artifacts, out, self.now(), self._compare_condition(exp))
                task.update(status=collect["status"], checked_at=iso(self.now()),
                            collect={k: collect[k] for k in ("present", "missing", "judge_failed", "superseded",
                                                             "notes", "first_seen", "fingerprint")})
                if collect["status"] not in collect_task.READY_STATUSES:
                    self.store.upsert(task)
                    return {"task_id": task_id, "status": collect["status"], "notes": collect["notes"]}
                if collect["marker"] in task["markers"] and not force:
                    self.store.upsert(task)
                    return {"task_id": task_id, "status": "already_compared", "marker": collect["marker"]}
                result = self._compare_ready(exp, task_id, collect, out, tmp)
            task.update(marker=collect["marker"], markers=[*task["markers"], collect["marker"]][-20:],
                        compared_at=iso(self.now()), absolute=result.pop("absolute"),
                        pairwise=result.pop("pairwise"), review_id=result.get("review_id"))
            self._stage_publish(task, exp, result.pop("docs"), {"comparison", "pairwise", "pair"})
            self.store.upsert(task)
            result["published"] = self._deliver(eid, task["id"])
            return {"task_id": task_id, "status": "compared", "collect_status": collect["status"], **result}

    def _compare_ready(self, exp: dict, task_id: str, collect: dict, out: Path, tmp: Path) -> dict:
        eid, d = exp["experiment_id"], exp["definition"]
        dataset_rows, manifest_rows = load_jsonl(out / "runs.jsonl"), load_jsonl(out / "manifest.jsonl")
        dataset = {r["run_id"]: r for r in dataset_rows}
        pairwise, pairwise_dir = None, None
        if collect["pairwise"]:
            self._check_condition(exp)
            pairs = run_pairwise.build_pairs(dataset, manifest_rows)
            pairwise_dir = self.judge.judge_pairs(pairs, dataset, name=f"{eid}-{task_id}-pairwise",
                                                  repeats=d["pairwise_repeats"])
            write_json(pairwise_dir / "pairs.json", pairs)
            self._verify_results(pairwise_dir, exp, absolute=False)
            pair_trials = compact_trials(pairwise_dir, "pair_key")
            # 片方の順序だけの欠けは run_pairwise.aggregate が人間レビューに回す。組ごとに点数が 1 つもなければ失敗
            check_trials(pair_trials, key="pair_key", repeats=d["pairwise_repeats"],
                         expected={f"{p['pair_id']}-{o}": ["pairwise"] for p in pairs for o in ("1", "2")},
                         group=lambda item_key: item_key.rsplit("-", 1)[0])
            quiet(run_pairwise.aggregate, pairwise_dir, pairs)
            pairwise = {"pairs": pairs,
                        "summary": json.loads((pairwise_dir / "pairwise_summary.json").read_text(encoding="utf-8")),
                        "run_meta": json.loads((pairwise_dir / "run_meta.json").read_text(encoding="utf-8")),
                        "trials": pair_trials}

        absolute_dir = out / "absolute"
        results_dirs = (out / "results_dirs.txt").read_text(encoding="utf-8").split()
        quiet(aggregate.main, ["--manifest", str(out / "manifest.jsonl"), "--out-dir", str(absolute_dir),
                               "--gap", str(d["gap"]), "--spread", str(d["spread"]), *results_dirs])
        write_json(absolute_dir / "run_meta.json", {"eval_id": collect["marker"]})
        summary_rows = load_jsonl(absolute_dir / "summary.jsonl")
        variants = json.loads((absolute_dir / "variants.json").read_text(encoding="utf-8"))

        review = self._build_review(exp, task_id, collect["marker"], dataset_rows, summary_rows, pairwise,
                                    pairwise_dir, tmp)
        docs = publish_results.build_documents(d["project"], manifest_rows, absolute_dir, pairwise_dir)
        return {"marker": collect["marker"], "pairwise_eval_id": (pairwise or {}).get("run_meta", {}).get("eval_id"),
                "review_id": review and review["id"], "review_issue": review and review.get("issue_url"),
                "human_review": {"pairs": review["pairs"] if review else 0, "runs": review["runs"] if review else 0},
                "docs": docs, "absolute": {"summary": summary_rows, "variants": variants}, "pairwise": pairwise}

    # ------------------------------------------------------------------ 人間レビュー

    def _build_review(self, exp, task_id, marker, dataset_rows, summary_rows, pairwise, pairwise_dir, tmp: Path):
        eid = exp["experiment_id"]
        review_dir, dataset_path = tmp / "review", write_jsonl(tmp / "review_runs.jsonl", dataset_rows)
        review_dir.mkdir()
        key = {}
        if pairwise_dir is not None:
            quiet(calibration.export_pairs, SimpleNamespace(
                dataset=dataset_path, pairwise_dir=pairwise_dir, seed=0, out=review_dir / "pairs_review.csv",
                packet_dir=review_dir / "pairs", flagged=True))
            key_path = review_dir / "pairs" / "_key.json"
            key = json.loads(key_path.read_text(encoding="utf-8"))
            key_path.unlink()
            if not key:
                (review_dir / "pairs_review.csv").unlink()
                (review_dir / "pairs").rmdir()
        # Run 単体の理由（ばらつき・low confidence・重大問題・指標の悪化）と、方式間の総合点差が小さいものの両方
        flagged = [s for s in summary_rows if s.get("human_review") and s["run_id"] in {r["run_id"] for r in dataset_rows}]
        if flagged:
            quiet(calibration.export, SimpleNamespace(
                dataset=dataset_path, flagged=write_jsonl(tmp / "flagged.jsonl", flagged), sample=None, seed=0,
                out=review_dir / "review.csv", packet_dir=review_dir / "runs"))
        if not key and not flagged:
            return None
        files = {str(p.relative_to(review_dir)): p.read_text(encoding="utf-8")
                 for p in sorted(review_dir.rglob("*")) if p.is_file()}
        review_id = f"review:{marker}"
        review = self.store.get(eid, review_id) or {
            "id": review_id, "kind": "review", "experiment_id": eid, "task_id": task_id, "marker": marker,
            "created_at": iso(self.now()), "issue_number": None, "issue_url": None, "decisions": []}
        review.update(pairs=len(key), runs=len(flagged), pairs_key=key, path=f"reviews/{eid}/{task_id}/{marker}",
                      pairwise_eval_id=(pairwise or {}).get("run_meta", {}).get("eval_id"),
                      pairwise_pairs=(pairwise or {}).get("summary", {}).get("pairs", []))
        if self.review_repo is not None:
            for rel, text in files.items():
                self.review_repo.put_file(f"{review['path']}/{rel}", text, f"llm-judge: {marker} のレビュー資料")
            if not review.get("issue_number"):
                issue = self.review_repo.create_issue(
                    f"[llm-judge] {eid} / {task_id} のレビュー（{marker}）", self._issue_body(review, task_id))
                review.update(issue_number=issue["number"], issue_url=issue["url"])
            review.update(status="open", files=None)
        else:
            size = sum(len(t.encode("utf-8")) for t in files.values())
            review.update(status="manual", files=files if size <= MAX_STORED_PACKET_BYTES else None,
                          files_note=None if size <= MAX_STORED_PACKET_BYTES else
                          f"資料が大きい（{size} バイト）ため保存していません。レビュー用リポジトリを設定してください")
        self.store.upsert(review)
        return review

    def _repo(self):
        if self.review_repo is None:
            raise RuntimeError("レビュー用リポジトリが設定されていません")
        return self.review_repo

    def _issue_body(self, review: dict, task_id: str) -> str:
        base = self._repo().file_url(review["path"])
        lines = [f"タスク `{task_id}` の実装を、方式名を伏せた資料でレビューしてください。", ""]
        if review["pairs"]:
            lines.append(f"- **ペア比較 {review['pairs']} 組**: [pairs/]({base}/pairs) の資料を読み、"
                         f"[pairs_review.csv]({base}/pairs_review.csv) の `better` 列に A / B / same を入れる")
        if review["runs"]:
            lines.append(f"- **絶対評価 {review['runs']} 件**: [runs/]({base}/runs) の資料を読み、"
                         f"[review.csv]({base}/review.csv) に観点ごとの点数（0〜5）と uncaught_critical"
                         "（1=問題なし / 0=問題あり）を入れる")
        lines += ["", "採点基準は評価器のルーブリック（evaluators/*.md）と同じです。",
                  "記入したシートをこのリポジトリにコミットしてから、この Issue を閉じてください。閉じると判断が取り込まれます。"]
        return "\n".join(lines)

    def review_packets(self, body: dict) -> dict:
        exp = self._experiment(body.get("experiment_id"))
        filters = {"task_id": body["task_id"]} if body.get("task_id") else {}
        reviews = self.store.query("review", exp["experiment_id"], **filters)
        return {"reviews": [{k: r.get(k) for k in ("marker", "status", "pairs", "runs", "path", "issue_url",
                                                    "files", "files_note")} for r in reviews]}

    def _run_ingest(self, job: dict) -> dict:
        if self.review_repo is None:
            return {"status": "skipped", "message": "レビュー用リポジトリが設定されていません"}
        experiments = ([self._experiment(job["experiment_id"])] if job["experiment_id"] != SERVICE_PARTITION
                       else self.store.query("experiment"))
        ingested, waiting = [], 0
        for exp in experiments:
            for status in ("open", "empty"):
                for review in self.store.query("review", exp["experiment_id"], status=status):
                    if self.review_repo.issue_state(review["issue_number"]) != "closed":
                        waiting += 1
                        continue
                    ingested.append(self._ingest_review(exp, review))
        return {"ingested": [r for r in ingested if r["decisions"]], "empty": [r for r in ingested if not r["decisions"]],
                "waiting": waiting}

    def _ingest_review(self, exp: dict, review: dict) -> dict:
        eid, project = exp["experiment_id"], exp["definition"]["project"]
        now = iso(self.now())
        docs = []
        with tempfile.TemporaryDirectory(prefix="llm-judge-") as tmp:
            tmp = Path(tmp)
            if review["runs"]:
                text = self._repo().get_file(f"{review['path']}/review.csv")
                if text:
                    (tmp / "review.csv").write_text(text, encoding="utf-8")
                    manifest = [r["manifest"] for r in self.store.query("run", eid)]
                    docs += publish_results.human_run_documents(project, manifest, tmp / "review.csv", now)
            if review["pairs"]:
                text = self._repo().get_file(f"{review['path']}/pairs_review.csv")
                if text:
                    (tmp / "pairs.csv").write_text(text, encoding="utf-8")
                    pairwise_dir = tmp / "pairwise"
                    write_json(pairwise_dir / "pairwise_summary.json", {"pairs": review["pairwise_pairs"]})
                    write_json(pairwise_dir / "run_meta.json", {"eval_id": review["pairwise_eval_id"]})
                    docs += publish_results.human_pair_documents(
                        project, tmp / "pairs.csv", write_json(tmp / "key.json", review["pairs_key"]), pairwise_dir, now)
        decisions = [{k: d.get(k) for k in ("review_kind", "review_key", "run_id", "pair_id", "task_id", "decision",
                                             "better", "scores", "uncaught_critical", "comment")} for d in docs]
        previous = review.get("status")
        review.update(decisions=decisions, status="ingested" if decisions else "empty", ingested_at=now)
        if decisions:
            self._stage_publish(review, exp, docs, {"human_review"})
        self.store.upsert(review)
        if decisions:
            self._deliver(eid, review["id"])
            self._repo().comment(review["issue_number"], f"llm-judge: {len(decisions)} 件の判断を取り込みました。")
        elif previous != "empty":
            self._repo().comment(review["issue_number"], "llm-judge: 記入された判断が見つかりませんでした。"
                                     "シートを記入してコミットしてから、Issue を開き直して閉じてください。")
        return {"marker": review["marker"], "task_id": review["task_id"], "decisions": len(decisions)}

    # ------------------------------------------------------------------ 定期処理・再採点

    def _run_sweep(self, job: dict) -> dict:
        experiments = ([self._experiment(job["experiment_id"])] if job["experiment_id"] != SERVICE_PARTITION
                       else [e for e in self.store.query("experiment") if e.get("status") == "active"])
        now, summary = self.now(), {}
        # 止まったジョブは実験をまたいで（_service の sweep / ingest_reviews も含めて）片付ける
        stale = Counter()
        for status in ACTIVE:
            for other in self.store.query("job", None, status=status):
                if other["id"] != job["id"] and self._is_stale(other):
                    self._fail_stale(other)
                    stale[other["experiment_id"]] += 1
        for exp in experiments:
            eid = exp["experiment_id"]
            retried = 0
            for run in self.store.query("run", eid, state="judge_failed"):
                if run.get("retries", 0) >= self.max_judge_retries:
                    continue
                run.update(retries=run.get("retries", 0) + 1, state="queued", updated_at=iso(now))
                self.store.upsert(run)
                self._enqueue("judge", eid, f"judge:{run['run_id']}:{run['input_hash']}",
                              {"run_id": run["run_id"], "input_hash": run["input_hash"]})
                retried += 1
            tasks = sorted({r["task_id"] for r in self.store.query("run", eid)})
            for task_id in tasks:
                self._spawn_compare(eid, task_id)
            delivered = pending = 0
            for kind in ("run", "task", "review"):
                for doc in self.store.query(kind, eid, publish_status="pending"):
                    result = self._deliver(eid, doc["id"])
                    delivered += result["status"] == "sent"
                    pending += result["status"] == "pending"
            summary[eid] = {"stale_jobs": stale.pop(eid, 0), "retried_runs": retried, "compare_tasks": len(tasks),
                            "publish_delivered": delivered, "publish_pending": pending}
        ingest = self._run_ingest(job) if self.review_repo is not None else None
        return {"experiments": summary, "stale_jobs_elsewhere": sum(stale.values()), "reviews": ingest}

    def _run_rejudge(self, job: dict) -> dict:
        target = self._experiment(job["experiment_id"])
        source = job["params"]["from_experiment_id"]
        submitted, skipped = [], []
        for run in sorted(self.store.query("run", source), key=lambda r: r["run_id"]):
            try:
                status, body = self.submit_run({"experiment_id": target["experiment_id"], "manifest": run["manifest"],
                                                "row": run["row"]})
            except ServiceError as exc:
                skipped.append({"run_id": run["run_id"], "reason": str(exc)})
                continue
            submitted.append({"run_id": run["run_id"], "status": body["status"],
                              "invocation_id": body.get("invocation_id")})
        return {"from_experiment_id": source, "submitted": submitted, "skipped": skipped}

    # ------------------------------------------------------------------ レポート

    def report(self, body: dict) -> dict:
        exp = self._experiment(body.get("experiment_id"))
        eid, d = exp["experiment_id"], exp["definition"]
        runs = self.store.query("run", eid)
        tasks = sorted(self.store.query("task", eid), key=lambda t: t["task_id"])
        reviews = self.store.query("review", eid)

        absolute = None
        counted = [r for r in runs if r["state"] in ("judged", "gate_failed")]
        if counted:
            with tempfile.TemporaryDirectory(prefix="llm-judge-") as tmp:
                tmp = Path(tmp)
                dirs = [str(write_results(tmp / "results" / r["eval_id"], r["trials"], r["run_meta"]))
                        for r in counted if r["state"] == "judged"]
                manifest_path = write_jsonl(tmp / "manifest.jsonl", [r["manifest"] for r in counted])
                quiet(aggregate.main, ["--manifest", str(manifest_path), "--out-dir", str(tmp / "out"),
                                       "--gap", str(d["gap"]), "--spread", str(d["spread"]), *dirs])
                absolute = json.loads((tmp / "out" / "variants.json").read_text(encoding="utf-8"))

        # ペア比較: タスクごとに両方の順序がそろった組の優位度を平均し、タスク単位の勝敗で符号検定する
        by_task = defaultdict(list)
        for task in tasks:
            for pair in ((task.get("pairwise") or {}).get("summary") or {}).get("pairs", []):
                if pair.get("pref") is not None:
                    by_task[(pair["variant_a"], pair["variant_b"], task["task_id"])].append(pair["pref"])
        pairwise = defaultdict(lambda: {"wins_a": 0, "wins_b": 0, "ties": 0, "prefs": []})
        for (va, vb, _), prefs in by_task.items():
            c, m = pairwise[(va, vb)], mean(prefs)
            if m is None:
                continue
            c["prefs"].append(m)
            c["wins_a"] += m > 0
            c["wins_b"] += m < 0
            c["ties"] += m == 0

        decisions = [dec for r in reviews for dec in r.get("decisions", [])]
        pair_prefs = {(r.get("pairwise_eval_id"), p["pair_id"]): p for r in reviews for p in r.get("pairwise_pairs", [])}
        agreement = Counter()
        for r in reviews:
            for dec in r.get("decisions", []):
                if dec.get("review_kind") != "pair":
                    continue
                pair = pair_prefs.get((r.get("pairwise_eval_id"), dec.get("pair_id")))
                if pair is None or pair.get("pref") is None:
                    agreement["judge_undecided"] += 1
                    continue
                judge = "same" if pair["pref"] == 0 else pair["variant_a"] if pair["pref"] > 0 else pair["variant_b"]
                agreement["agree" if judge == dec.get("better") else "disagree"] += 1

        return {
            "experiment": {k: exp[k] for k in ("experiment_id", "definition", "condition", "created_at")},
            "runs": [{"variant": v, "state": s, "count": n}
                     for (v, s), n in sorted(Counter((r["variant"], r["state"]) for r in runs).items())],
            "tasks": [{k: t.get(k) for k in ("task_id", "status", "marker", "compared_at")} for t in tasks],
            "absolute": absolute,
            "pairwise": [{"a": va, "b": vb, **{k: v for k, v in c.items() if k != "prefs"},
                          "mean_pref_a": mean(c["prefs"]), "p_value_sign_test": sign_test(c["wins_a"], c["wins_b"])}
                         for (va, vb), c in pairwise.items()],
            "human_reviews": {
                "reviews": Counter(r.get("status") for r in reviews),
                "decisions": len(decisions),
                "pair_agreement_with_judge": dict(agreement),
            },
        }

    # ------------------------------------------------------------------ 可視化基盤

    def _stage_publish(self, doc: dict, exp: dict, docs: list[dict], types: set[str]) -> None:
        """可視化基盤に送る内容と送信先を、元の文書（Run・タスク・レビュー）に保存する。送るのは _deliver。

        文書の ID と Run の人間レビューの結合キーは実験ごとに分ける（同じ Run を別の実験で採点し直しても上書きしない）。
        """
        sinks = self.publisher.sinks() if self.publisher is not None else []
        selected = publish_results.scope_documents([d for d in docs if d["type"] in types], exp["experiment_id"])
        if not sinks or not selected:
            doc.update(publish=None, publish_status=None)
            return
        doc.update(publish={"docs": selected, "pending": list(sinks), "attempts": 0, "errors": None,
                            "staged_at": iso(self.now()), "sent_at": None}, publish_status="pending")

    def _deliver(self, experiment_id: str, doc_id: str) -> dict:
        """保存した内容を、まだ届いていない送信先にだけ送る。失敗しても例外にせず、次の sweep で送り直す。"""
        doc = self.store.get(experiment_id, doc_id)
        if doc is None or not doc.get("publish") or self.publisher is None:
            return {"status": "disabled"}
        publish = doc["publish"]
        errors = {}
        for sink in list(publish["pending"]):
            try:
                self.publisher.send(sink, publish["docs"])
                publish["pending"].remove(sink)
            except Exception as exc:  # noqa: BLE001 — 送信先ごとに記録して送り直す
                errors[sink] = f"{type(exc).__name__}: {exc}"[:1000]
        publish.update(attempts=publish["attempts"] + 1, errors=errors or None,
                       sent_at=None if publish["pending"] else iso(self.now()))
        doc.update(publish=publish, publish_status="pending" if publish["pending"] else "sent")
        self.store.upsert(doc)
        return self._publish_view(doc)

    @staticmethod
    def _publish_view(doc: dict) -> dict:
        publish = doc.get("publish")
        if not publish:
            return {"status": "disabled"}
        return {"status": doc["publish_status"], "documents": len(publish["docs"]), "pending": publish["pending"],
                "attempts": publish["attempts"], "errors": publish["errors"]}


OPENAPI_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "LLM Judge service", "version": "1.0.0",
             "description": "コーディングエージェントの PR を実験単位で採点・比較する。詳細は README の「Foundry Hosted Agent」"},
    "paths": {
        "/invocations": {"post": {
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["op"],
                "properties": {
                    "op": {"type": "string", "enum": ["submit_run", *OPS_SYNC, *OPS_ASYNC]},
                    "experiment_id": {"type": "string"},
                    "project": {"type": "string"}, "variants": {"type": "array", "items": {"type": "string"}},
                    "attempts": {"type": "integer"}, "timeout_hours": {"type": "number"},
                    "gap": {"type": "number"}, "spread": {"type": "number"},
                    "judge_repeats": {"type": "integer"}, "pairwise_repeats": {"type": "integer"},
                    "manifest": {"type": "object", "description": "make_row.py の対応表の 1 行"},
                    "row": {"type": "object", "description": "make_row.py の評価データの 1 行（Hard Gate 合格時）"},
                    "task_id": {"type": "string"}, "force": {"type": "boolean"},
                    "from_experiment_id": {"type": "string"},
                }}}}},
            "responses": {"200": {"description": "同期の操作の結果"},
                          "202": {"description": "受け付けた。GET /invocations/{invocation_id} で状態を取得する"}},
        }},
        "/invocations/{invocation_id}": {"get": {"responses": {"200": {"description": "ジョブの状態と結果"},
                                                               "404": {"description": "ジョブがない"}}}},
    },
}
