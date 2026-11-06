"""Foundry の評価 API を呼ぶ共通処理。Judge のバージョン確認と実行メタデータの記録も行う。"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from azure.ai.projects import AIProjectClient

from azure_auth import credential
from config import (
    FOUNDRY_PREVIEW_HEADERS,
    JUDGE_DEPLOYMENT,
    JUDGE_MODEL_VERSION,
    PROJECT_ENDPOINT,
    RESULTS_DIR,
    rubric_fingerprint,
)

TERMINAL = {"completed", "failed", "canceled"}
# Judge の一時的なエラー（レート制限 429・タイムアウト・5xx）で失敗した項目だけを、待ってから試行ごとに順番に採点し直す。
# 0 で採点し直さない
RETRIES = int(os.environ.get("LLM_JUDGE_RETRIES") or 5)
RETRY_WAIT_SEC = float(os.environ.get("LLM_JUDGE_RETRY_WAIT_SEC") or 60)
# 採点し直すときに 1 回の評価実行に入れる項目数。0 なら全部まとめる。TPM の小さい Judge では小さくする
RETRY_BATCH = int(os.environ.get("LLM_JUDGE_RETRY_BATCH") or 0)
TRANSIENT_RE = re.compile(r"\b(429|5\d\d)\b|RateLimit|timed out|timeout|temporarily unavailable", re.IGNORECASE)


def project_client() -> AIProjectClient:
    return AIProjectClient(PROJECT_ENDPOINT, credential(), allow_preview=True)


def check_judge(project: AIProjectClient) -> dict:
    deployment = project.deployments.get(JUDGE_DEPLOYMENT).as_dict()
    actual = deployment.get("modelVersion")
    if actual != JUDGE_MODEL_VERSION and not os.environ.get("ALLOW_JUDGE_VERSION_CHANGE"):
        raise SystemExit(
            f"Judge のモデルバージョンが想定と異なります: {JUDGE_DEPLOYMENT} = {actual}"
            f"（想定 {JUDGE_MODEL_VERSION}）。過去の結果と比較できなくなるため停止しました。"
            " 意図した更新なら JUDGE_MODEL_VERSION を更新するか ALLOW_JUDGE_VERSION_CHANGE=1 を付けて実行してください。"
        )
    return {"deployment": JUDGE_DEPLOYMENT, "model": deployment.get("modelName"), "version": actual}


def evaluator_versions(project: AIProjectClient, names: list[str]) -> dict[str, str]:
    versions = {}
    for name in names:
        found = list(project.evaluators.list_versions(name, type="custom", headers=FOUNDRY_PREVIEW_HEADERS))
        versions[name] = max((v.version for v in found), key=int) if found else "missing"
    return versions


def current_condition(evaluator_names: list[str]) -> dict:
    """今この環境で採点したら記録される評価条件（Judge・評価器のバージョン・ルーブリック）。採点はしない。"""
    project = project_client()
    judge = check_judge(project)
    return {
        "judge": judge,
        "evaluator_versions": evaluator_versions(project, evaluator_names),
        "rubric": rubric_fingerprint(),
    }


def transient_error(row: dict) -> bool:
    """出力の 1 行に、一時的なエラーで失敗した評価器の結果が含まれるか。"""
    return any(r.get("status") != "completed" and TRANSIENT_RE.search(json.dumps(r.get("sample") or {}))
               for r in row.get("results") or [])


def retry_transient(rows: list[dict], key: str, rejudge, *, retries: int, wait_sec: float) -> tuple[list[dict], int]:
    """一時的なエラーで失敗した行を rejudge(失敗した行) の結果で置き換える。(置き換え後の行, 採点し直した行数) を返す。"""
    retried = 0
    for attempt in range(1, retries + 1):
        failed = [r for r in rows if transient_error(r)]
        if not failed:
            break
        print(f"  一時的なエラーで失敗した {len(failed)} 件を {wait_sec * attempt:g} 秒後に採点し直します（{attempt}/{retries}）")
        time.sleep(wait_sec * attempt)
        retried += len(failed)
        fresh = {r["datasource_item"][key]: r for r in rejudge(failed)}
        rows = [fresh.get(r["datasource_item"][key], r) if transient_error(r) else r for r in rows]
    return rows, retried


def run_evaluation(
    *,
    name: str,
    testing_criteria: list[dict],
    item_fields: list[str],
    items: list[dict],
    repeats: int,
    evaluator_names: list[str],
) -> Path:
    """items を repeats 回評価し、results/<eval_id>/trial*.jsonl と run_meta.json を保存する。"""
    project = project_client()
    judge = check_judge(project)
    versions = evaluator_versions(project, evaluator_names)
    if "missing" in versions.values():
        raise SystemExit(f"未登録の評価器があります: {versions}。register_evaluators.py を先に実行してください。")
    client = project.get_openai_client()

    eval_obj = client.evals.create(
        name=name,
        data_source_config={
            "type": "custom",
            "item_schema": {
                "type": "object",
                "properties": {f: {"type": "string"} for f in item_fields},
                "required": item_fields,
            },
        },
        testing_criteria=testing_criteria,
        metadata={"judge": f"{judge['deployment']}@{judge['version']}"},
    )
    print(f"eval: {eval_obj.id}")

    runs = []
    for i in range(1, repeats + 1):
        run = create_run(client, eval_obj.id, f"{name}-trial{i}", i, items)
        print(f"run {i}: {run.id}")
        runs.append(run)
    wait_runs(client, eval_obj.id, runs)

    out_dir = RESULTS_DIR / eval_obj.id
    out_dir.mkdir(parents=True, exist_ok=True)
    key = item_fields[0]
    by_key = {it[key]: it for it in items}
    retry_run_ids, retried, still_failed = [], 0, 0
    for i, run in enumerate(runs, start=1):
        def rejudge(failed: list[dict], trial: int = i) -> list[dict]:
            fresh, size = [], RETRY_BATCH or len(failed)
            for start in range(0, len(failed), size):
                retry = create_run(client, eval_obj.id, f"{name}-trial{trial}-retry", trial,
                                   [by_key[r["datasource_item"][key]] for r in failed[start:start + size]])
                retry_run_ids.append(retry.id)
                wait_runs(client, eval_obj.id, [retry])
                fresh += output_rows(client, eval_obj.id, retry.id)
            return fresh

        rows, n = retry_transient(output_rows(client, eval_obj.id, run.id), key, rejudge,
                                     retries=RETRIES, wait_sec=RETRY_WAIT_SEC)
        retried += n
        still_failed += sum(1 for r in rows if transient_error(r))
        (out_dir / f"trial{i}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
        )
    if still_failed:
        print(f"注意: 一時的なエラーで採点できなかった項目が {still_failed} 件残りました（評価エラーとして人間レビューに回ります）")

    report_url = client.evals.runs.retrieve(runs[0].id, eval_id=eval_obj.id).report_url
    meta = {
        "eval_id": eval_obj.id,
        "name": name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project_endpoint": PROJECT_ENDPOINT,
        "judge": judge,
        "evaluator_versions": versions,
        "rubric": rubric_fingerprint(),
        "repeats": repeats,
        "items": len(items),
        "run_ids": [r.id for r in runs],
        "transient_retries": {"rejudged_items": retried, "still_failed": still_failed,
                               "retry_run_ids": retry_run_ids},
        "report_url": report_url,
    }
    (out_dir / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved: {out_dir}")
    print(f"portal: {report_url}")
    return out_dir


def create_run(client, eval_id: str, name: str, trial: int, items: list[dict]):
    return client.evals.runs.create(
        eval_id=eval_id,
        name=name,
        metadata={"trial": str(trial)},
        data_source={
            "type": "jsonl",
            "source": {"type": "file_content", "content": [{"item": it} for it in items]},
        },
    )


def wait_runs(client, eval_id: str, runs: list) -> None:
    pending = {r.id for r in runs}
    while pending:
        time.sleep(15)
        for run_id in list(pending):
            run = client.evals.runs.retrieve(run_id, eval_id=eval_id)
            if run.status in TERMINAL:
                pending.discard(run_id)
                print(f"{run_id}: {run.status} {run.result_counts}")


def output_rows(client, eval_id: str, run_id: str) -> list[dict]:
    return [o.model_dump(mode="json") for o in client.evals.runs.output_items.list(run_id, eval_id=eval_id)]
