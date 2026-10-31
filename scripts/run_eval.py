"""データセットに対して全評価器を N 回（既定 3 回）実行し、結果を results/ に保存する。

使い方:
  python scripts/run_eval.py datasets/runs.jsonl --repeats 3 --name my-eval
"""

import argparse
import json
from pathlib import Path

from config import DATASET_FIELDS, EVALUATORS, JUDGE_DEPLOYMENT, ORDINAL_THRESHOLD
from foundry_eval import run_evaluation


def load_items(path: Path) -> list[dict]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            items.append({"run_id": row["run_id"], **{f: row.get(f) or "なし" for f in DATASET_FIELDS}})
    return items


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--name", default="code-pr-judge")
    args = parser.parse_args()

    testing_criteria = [
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
    run_evaluation(
        name=args.name,
        testing_criteria=testing_criteria,
        item_fields=["run_id", *DATASET_FIELDS],
        items=load_items(args.dataset),
        repeats=args.repeats,
        evaluator_names=[s["name"] for s in EVALUATORS.values()],
    )


if __name__ == "__main__":
    main()
