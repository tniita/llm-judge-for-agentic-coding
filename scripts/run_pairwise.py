"""同じタスクの 2 方式の PR を Judge に並べて比較させる（ペア比較）。位置バイアスを打ち消すため、A/B を入れ替えて両方の順序で評価する。

使い方:
  python scripts/run_pairwise.py datasets/runs.jsonl datasets/manifest.jsonl --name my-pairwise
  python scripts/run_pairwise.py datasets/runs.jsonl datasets/manifest.jsonl --results-dir results/<eval_id>  # 集計のみ

同じタスク × 方式に複数回の生成がある場合は、attempt の番号順に組にする。
"""

import argparse
import json
from collections import defaultdict
from itertools import combinations
from pathlib import Path

from aggregate import CONFIDENCE_RE
from config import JUDGE_DEPLOYMENT, PAIRWISE
from foundry_eval import run_evaluation
from stats_utils import fmt, mean, sign_test

REASON_INCONSISTENT = "ペア比較: A/B の順序で判定が逆転"


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def build_pairs(dataset: dict[str, dict], manifest: list[dict]) -> list[dict]:
    by_cell = defaultdict(list)
    for m in manifest:
        if m.get("hard_gate", "pass") == "pass" and m["run_id"] in dataset:
            by_cell[(m["task_id"], m["variant"])].append(m)
    tasks = sorted({t for t, _ in by_cell})
    variants = sorted({v for _, v in by_cell})
    pairs = []
    for task in tasks:
        for va, vb in combinations(variants, 2):
            runs_a = sorted(by_cell.get((task, va), []), key=lambda m: m.get("attempt", 1))
            runs_b = sorted(by_cell.get((task, vb), []), key=lambda m: m.get("attempt", 1))
            for ra, rb in zip(runs_a, runs_b):
                pairs.append({
                    "pair_id": f"P{len(pairs) + 1:03d}", "task_id": task,
                    "variant_a": va, "run_a": ra["run_id"], "variant_b": vb, "run_b": rb["run_id"],
                })
    return pairs


def to_items(pairs: list[dict], dataset: dict[str, dict]) -> list[dict]:
    items = []
    for p in pairs:
        first, second = dataset[p["run_a"]], dataset[p["run_b"]]
        skills = first.get("project_skills") or "なし"
        if skills != (second.get("project_skills") or "なし"):
            raise SystemExit(f"{p['pair_id']}: 2 つの Run で Skill が異なるため、同じ条件で比較できません"
                             f"（{p['run_a']} / {p['run_b']}）")
        for order, (x, y) in (("1", (first, second)), ("2", (second, first))):
            items.append({
                "pair_key": f"{p['pair_id']}-{order}",
                "query": x["query"],
                "project_skills": skills,
                "response_a": x["response"], "ci_summary_a": x["ci_summary"],
                "metrics_delta_a": x.get("metrics_delta") or "なし",
                "response_b": y["response"], "ci_summary_b": y["ci_summary"],
                "metrics_delta_b": y.get("metrics_delta") or "なし",
            })
    return items


def aggregate(results_dir: Path, pairs: list[dict]) -> None:
    # pref: variant_a から見た優位度（-1〜+1）。順序 1 は A=variant_a、順序 2 は A=variant_b
    prefs = defaultdict(lambda: {"1": [], "2": []})
    errors = defaultdict(set)
    low_confidence = defaultdict(set)
    raw_first = []
    for path in sorted(results_dir.glob("trial*.jsonl")):
        for row in load_jsonl(path):
            pair_id, order = row["datasource_item"]["pair_key"].rsplit("-", 1)
            for r in row["results"]:
                if r.get("score") is None or r.get("status", "completed") != "completed":
                    errors[pair_id].add(order)
                    continue
                m = CONFIDENCE_RE.search(r.get("reason") or "")
                if not m or m.group(1).lower() == "low":
                    low_confidence[pair_id].add(order)
                raw_first.append(3 - r["score"])
                pref = (3 - r["score"]) / 2 if order == "1" else (r["score"] - 3) / 2
                prefs[pair_id][order].append(pref)

    pair_rows = []
    for p in pairs:
        pid = p["pair_id"]
        o1, o2 = mean(prefs[pid]["1"]), mean(prefs[pid]["2"])
        inconsistent = o1 is not None and o2 is not None and o1 * o2 < 0
        reasons = []
        if errors[pid]:
            reasons.append(f"ペア比較: 評価エラー（順序 {', '.join(sorted(errors[pid]))}）")
        missing = [o for o, v in (("1", o1), ("2", o2)) if v is None]
        if missing:
            reasons.append(f"ペア比較: 順序 {', '.join(missing)} の判定がなく、位置バイアスを打ち消せない")
        if inconsistent:
            reasons.append(REASON_INCONSISTENT)
        if low_confidence[pid]:
            reasons.append(f"ペア比較: confidence が low の判定あり（順序 {', '.join(sorted(low_confidence[pid]))}）")
        # 両方の順序がそろったときだけ優劣を確定する（片方だけでは位置バイアスを含む）
        combined = None if missing else mean([o1, o2])
        pair_rows.append({**p, "pref_order1": o1, "pref_order2": o2, "pref": combined,
                          "position_inconsistent": inconsistent, "human_review": reasons})

    by_task = defaultdict(list)
    for r in pair_rows:
        if r["pref"] is not None:
            by_task[(r["variant_a"], r["variant_b"], r["task_id"])].append(r["pref"])
    comparisons = defaultdict(lambda: {"wins_a": 0, "wins_b": 0, "ties": 0, "prefs": []})
    for (va, vb, _), values in by_task.items():
        c, m = comparisons[(va, vb)], mean(values)
        c["prefs"].append(m)
        c["wins_a"] += m > 0
        c["wins_b"] += m < 0
        c["ties"] += m == 0

    summary = {
        "pairs": pair_rows,
        "comparisons": [
            {"a": va, "b": vb, **{k: v for k, v in c.items() if k != "prefs"},
             "mean_pref_a": mean(c["prefs"]), "p_value_sign_test": sign_test(c["wins_a"], c["wins_b"])}
            for (va, vb), c in comparisons.items()
        ],
        # 正なら先に提示された方を選びやすい（位置バイアス）
        "position_bias": mean([v / 2 for v in raw_first]),
    }
    (results_dir / "pairwise_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("pair\ttask\ta\tb\torder1\torder2\tpref(a)\treview")
    for r in pair_rows:
        print("\t".join(str(c) for c in [
            r["pair_id"], r["task_id"], f"{r['variant_a']}:{r['run_a']}", f"{r['variant_b']}:{r['run_b']}",
            fmt(r["pref_order1"], 2), fmt(r["pref_order2"], 2), fmt(r["pref"], 2),
            " / ".join(r["human_review"]) or "-",
        ]))
    print()
    for c in summary["comparisons"]:
        print(f"{c['a']} vs {c['b']}: {c['wins_a']} 勝 {c['wins_b']} 敗 {c['ties']} 分 "
              f"/ 優位度の平均 {fmt(c['mean_pref_a'], 2)}（+1 で {c['a']} が常に明確に優位）"
              f"/ 符号検定 p={c['p_value_sign_test']:.3f}")
    print(f"位置バイアス: {fmt(summary['position_bias'], 2)}（0 に近いほど良い。正なら先に提示された方を選びやすい）")
    print(f"→ {results_dir / 'pairwise_summary.json'}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--name", default="code-pr-pairwise")
    parser.add_argument("--repeats", type=int, default=1, help="各順序の試行回数")
    parser.add_argument("--results-dir", type=Path, help="既存の結果を集計だけする")
    args = parser.parse_args()

    dataset = {r["run_id"]: r for r in load_jsonl(args.dataset)}

    if args.results_dir:
        aggregate(args.results_dir, json.loads((args.results_dir / "pairs.json").read_text(encoding="utf-8")))
        return

    pairs = build_pairs(dataset, load_jsonl(args.manifest))
    if not pairs:
        raise SystemExit("比較できる組がありません（同じタスクに 2 方式以上の合格 Run が必要）")
    out_dir = judge_pairs(pairs, dataset, name=args.name, repeats=args.repeats)
    aggregate(out_dir, pairs)


def judge_pairs(pairs: list[dict], dataset: dict[str, dict], *, name: str, repeats: int) -> Path:
    """組を A/B 両方の順序で採点し、results/<eval_id>/（pairs.json を含む）を返す。集計は aggregate() で行う。"""
    out_dir = run_evaluation(
        name=name,
        testing_criteria=[{
            "type": "azure_ai_evaluator",
            "name": "pairwise",
            "evaluator_name": PAIRWISE["name"],
            "data_mapping": {f: f"{{{{item.{f}}}}}" for f in PAIRWISE["fields"]},
            "initialization_parameters": {"deployment_name": JUDGE_DEPLOYMENT, "threshold": 3},
        }],
        item_fields=["pair_key", *PAIRWISE["fields"]],
        items=to_items(pairs, dataset),
        repeats=repeats,
        evaluator_names=[PAIRWISE["name"]],
    )
    (out_dir / "pairs.json").write_text(json.dumps(pairs, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_dir


if __name__ == "__main__":
    main()
