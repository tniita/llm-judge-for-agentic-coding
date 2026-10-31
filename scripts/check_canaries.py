"""欠陥をわざと仕込んだ差分（カナリア）で、Judge が欠陥をどれだけ検出できるかを測る。

使い方:
  python scripts/check_canaries.py build                      # datasets/canaries/*/ から評価データを作る
  python scripts/run_eval.py datasets/canaries/canary_runs.jsonl --repeats 3 --name canary
  python scripts/check_canaries.py check results/<eval_id>    # 期待どおりに検出できたかを判定

ルーブリックや Judge モデルを変えたら、実データの前に必ずこれを流して検出力が落ちていないか確かめる。
"""

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from aggregate import load_jsonl, summarize_run
from config import ROOT

CANARY_DIR = ROOT / "datasets" / "canaries"
DATASET = CANARY_DIR / "canary_runs.jsonl"
MANIFEST = CANARY_DIR / "canary_manifest.jsonl"


def load_expected() -> dict:
    return json.loads((CANARY_DIR / "expected.json").read_text(encoding="utf-8"))


def build(_args) -> None:
    for path in (DATASET, MANIFEST):
        path.unlink(missing_ok=True)
    for cid, spec in load_expected().items():
        subprocess.run([
            sys.executable, str(ROOT / "scripts" / "make_row.py"),
            "--run-id", cid, "--task-id", cid, "--variant", "canary-set",
            "--task", str(CANARY_DIR / spec.get("task", "task_duration.md")),
            "--diff", str(CANARY_DIR / cid / "pr.diff"),
            "--ci", str(CANARY_DIR / spec.get("ci", "ci_green.txt")),
            "--pr", str(CANARY_DIR / cid / "pr.md"),
            "--dataset", str(DATASET), "--manifest", str(MANIFEST),
        ], check=True)


def satisfied(actual, rule) -> bool:
    if isinstance(rule, bool):
        return actual is rule
    op, value = rule[:2], float(rule[2:])
    return actual >= value if op == ">=" else actual <= value


def check(args) -> None:
    trials = defaultdict(lambda: defaultdict(list))
    for path in sorted(args.results_dir.glob("trial*.jsonl")):
        for row in load_jsonl(path):
            for r in row["results"]:
                trials[row["datasource_item"]["run_id"]][r["name"]].append(r)

    total = ok = 0
    print("canary\t観点\t期待\t実際\t判定")
    for cid, spec in load_expected().items():
        s = summarize_run(cid, trials[cid], spread=99)
        for key, rule in spec["expect"].items():
            actual = (not s["critical_issues"]) if key == "uncaught_critical" else s["scores"].get(key)
            passed = actual is not None and satisfied(actual, rule)
            total += 1
            ok += passed
            print(f"{cid}\t{key}\t{rule}\t{actual}\t{'OK' if passed else 'NG'}")
    print(f"\n期待どおり: {ok}/{total}（{ok / total:.0%}）")
    print("NG の観点は、ルーブリックの記述か Judge モデルの見直しが必要です")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build")
    c = sub.add_parser("check")
    c.add_argument("results_dir", type=Path)
    args = parser.parse_args()
    build(args) if args.command == "build" else check(args)


if __name__ == "__main__":
    main()
