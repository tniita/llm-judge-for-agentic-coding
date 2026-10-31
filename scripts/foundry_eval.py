"""Foundry の評価 API を呼ぶ共通処理。Judge のバージョン確認と実行メタデータの記録も行う。"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential

from config import (
    FOUNDRY_PREVIEW_HEADERS,
    JUDGE_DEPLOYMENT,
    JUDGE_MODEL_VERSION,
    PROJECT_ENDPOINT,
    ROOT,
)

TERMINAL = {"completed", "failed", "canceled"}


def project_client() -> AIProjectClient:
    return AIProjectClient(PROJECT_ENDPOINT, AzureCliCredential(), allow_preview=True)


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
        run = client.evals.runs.create(
            eval_id=eval_obj.id,
            name=f"{name}-trial{i}",
            metadata={"trial": str(i)},
            data_source={
                "type": "jsonl",
                "source": {"type": "file_content", "content": [{"item": it} for it in items]},
            },
        )
        print(f"run {i}: {run.id}")
        runs.append(run)

    pending = {r.id for r in runs}
    while pending:
        time.sleep(15)
        for run_id in list(pending):
            run = client.evals.runs.retrieve(run_id, eval_id=eval_obj.id)
            if run.status in TERMINAL:
                pending.discard(run_id)
                print(f"{run_id}: {run.status} {run.result_counts}")

    out_dir = ROOT / "results" / eval_obj.id
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, run in enumerate(runs, start=1):
        rows = [o.model_dump(mode="json") for o in client.evals.runs.output_items.list(run.id, eval_id=eval_obj.id)]
        (out_dir / f"trial{i}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
        )

    report_url = client.evals.runs.retrieve(runs[0].id, eval_id=eval_obj.id).report_url
    meta = {
        "eval_id": eval_obj.id,
        "name": name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project_endpoint": PROJECT_ENDPOINT,
        "judge": judge,
        "evaluator_versions": versions,
        "repeats": repeats,
        "items": len(items),
        "run_ids": [r.id for r in runs],
        "report_url": report_url,
    }
    (out_dir / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved: {out_dir}")
    print(f"portal: {report_url}")
    return out_dir
