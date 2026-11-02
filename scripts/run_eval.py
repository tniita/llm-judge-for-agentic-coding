"""データセットに対して全評価器を N 回（既定 3 回）実行し、結果を results/ に保存する。

使い方:
  python scripts/run_eval.py datasets/runs.jsonl --repeats 3 --name my-eval
"""

import argparse
import json
from pathlib import Path

from config import DATASET_FIELDS, EVALUATORS, JUDGE_DEPLOYMENT, ORDINAL_THRESHOLD
from foundry_eval import run_evaluation


def to_item(row: dict) -> dict:
    return {"run_id": row["run_id"], **{f: row.get(f) or "なし" for f in DATASET_FIELDS}}


def load_items(path: Path) -> list[dict]:
    return [to_item(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def testing_criteria() -> list[dict]:
    return [
        {
            "type": "azure_ai_evaluator",
            "name": key,
            "evaluator_name": spec["name"],
            "data_mapping": {f: f"{{{{item.{f}}}}}" for f in spec["fields"]},
            "initialization_parameters": {
                "deployment_name": JUDGE_DEPLOYMENT,
                "threshold": ORDINAL_THRESHOLD if spec["scoring"] == "ordinal" else 1,
            },
        }
        for key, spec in EVALUATORS.items()
    ]


def evaluator_names() -> list[str]:
    return [s["name"] for s in EVALUATORS.values()]


def judge_rows(rows: list[dict], *, name: str, repeats: int) -> Path:
    """評価データの行を全評価器で repeats 回採点し、results/<eval_id>/ を返す。"""
    return run_evaluation(
        name=name,
        testing_criteria=testing_criteria(),
        item_fields=["run_id", *DATASET_FIELDS],
        items=[to_item(r) for r in rows],
        repeats=repeats,
        evaluator_names=evaluator_names(),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--name", default="code-pr-judge")
    args = parser.parse_args()
    rows = [json.loads(l) for l in args.dataset.read_text(encoding="utf-8").splitlines() if l.strip()]
    judge_rows(rows, name=args.name, repeats=args.repeats)


if __name__ == "__main__":
    main()
