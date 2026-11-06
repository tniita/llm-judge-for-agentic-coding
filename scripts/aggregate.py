"""run_eval.py の結果を集計し、方式（variant）ごとの比較と人間レビューへの振り分けを出す。

使い方:
  python scripts/aggregate.py results/<eval_id> datasets/manifest.jsonl --gap 5
  # Run ごとに別々に評価した結果をまとめて集計する（CI の llm-judge-compare など）
  python scripts/aggregate.py --manifest merged_manifest.jsonl --out-dir compare/absolute results/<eval_1> results/<eval_2>

manifest は run_id → task_id / variant / attempt / hard_gate / コストの対応表。Judge には渡さず、ここでだけ使う。
Hard Gate 不合格の Run も manifest に入れておくと、合格率とコストを集計できる。
manifest に機械的な指標の差分（make_row.py --metrics）があれば、config.py の METRIC_THRESHOLDS を超えて悪化した Run を人間レビューに回す。
manifest にエージェントが使ったモデル（make_row.py --agent-models）があれば、Judge と同系統の側を優位とした判定を人間レビューに回す。

出力（--out-dir を指定した場合はそのディレクトリ）:
  results/<eval_id>/summary.jsonl   Run ごとの結果
  results/<eval_id>/variants.json   方式ごとの集計と方式どうしの比較
"""

import argparse
import json
import re
import statistics
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

from config import (
    EVALUATORS,
    METRIC_LABELS,
    METRIC_THRESHOLDS,
    ORDINAL_MAX,
    WEIGHTS,
    same_family_models,
)
from stats_utils import fmt, mean, sign_test

ORDINAL_KEYS = [k for k, s in EVALUATORS.items() if s["scoring"] == "ordinal"]
CONFIDENCE_RE = re.compile(r"\[confidence:\s*(high|medium|low)\]", re.IGNORECASE)
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
REQ_RE = re.compile(r"\b(R\d+)[^:：/]{0,20}[:：]\s*(met|partial|unmet)\b", re.IGNORECASE)
REQ_VALUE = {"met": 1.0, "partial": 0.5, "unmet": 0.0}
COST_KEYS = ["tokens_in", "tokens_out", "cost_usd", "duration_sec"]


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def weighted_total(scores: dict[str, float]) -> float | None:
    used = {k: w for k, w in WEIGHTS.items() if k in scores}
    if not used:
        return None
    return sum(scores[k] * w for k, w in used.items()) / (ORDINAL_MAX * sum(used.values())) * 100


def requirement_statuses(results: list[dict]) -> dict[str, str]:
    """各試行の reason から要件ごとの判定を取り出し、多数決を取る（同数なら厳しい方）。"""
    votes = defaultdict(list)
    for r in results:
        for rid, status in dict(REQ_RE.findall(r.get("reason") or "")).items():
            votes[rid.upper()].append(status.lower())
    decided = {}
    for rid, vs in votes.items():
        counts = Counter(vs)
        top = max(counts.values())
        decided[rid] = min((s for s, c in counts.items() if c == top), key=REQ_VALUE.get)
    return dict(sorted(decided.items(), key=lambda kv: int(kv[0][1:])))


def metric_flags(delta: dict | None) -> list[str]:
    flags = []
    for key, (direction, threshold) in METRIC_THRESHOLDS.items():
        value = (delta or {}).get(key)
        if value is None:
            continue
        worse = value if direction == "up" else -value
        if worse > threshold:
            flags.append(f"指標の悪化: {METRIC_LABELS.get(key, key)} {value:+g}（閾値 {'+' if direction == 'up' else '-'}{threshold:g}）")
    return flags


def summarize_run(run_id: str, by_key: dict[str, list[dict]], spread: float) -> dict:
    flags, scores, confidences, critical = [], {}, [], []
    for key in EVALUATORS:
        results = by_key.get(key, [])
        if not results or any(r.get("status") != "completed" for r in results):
            flags.append(f"{key}: 評価エラーあり")
        for r in results:
            m = CONFIDENCE_RE.search(r.get("reason") or "")
            confidences.append(m.group(1).lower() if m else "low")
        if key in ORDINAL_KEYS:
            values = [r["score"] for r in results if r.get("score") is not None]
            if values:
                scores[key] = statistics.median(values)
                if max(values) - min(values) >= spread:
                    flags.append(f"{key}: 試行間の差 {max(values) - min(values):g} 点（{values}）")
        else:
            critical += [r.get("reason", "") for r in results if r.get("passed") is False]

    confidence = min(confidences, key=CONFIDENCE_RANK.get, default="low")
    if confidence == "low":
        flags.append("confidence が low の試行あり")
    if critical:
        flags.append(f"CI 未検出の重大問題（{len(critical)}/{len(by_key.get('uncaught_critical', []))} 試行）")

    requirements = requirement_statuses(by_key.get("requirements", []))
    return {
        "run_id": run_id,
        "scores": scores,
        "total": weighted_total(scores),
        "requirements": requirements,
        "req_met_rate": mean([REQ_VALUE[s] for s in requirements.values()]),
        "critical_issues": critical,
        "confidence": confidence,
        "human_review": flags,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", type=Path, nargs="*",
                        help="results/<eval_id> と manifest（従来の形式）。--manifest 指定時は results/<eval_id> を 0 個以上")
    parser.add_argument("--manifest", type=Path, help="対応表。指定すると位置引数はすべて結果ディレクトリとして扱う")
    parser.add_argument("--out-dir", type=Path, help="summary.jsonl / variants.json の出力先（結果ディレクトリが 1 つでない場合は必須）")
    parser.add_argument("--allow-mixed-conditions", action="store_true",
                        help="Judge・評価器のバージョン・採点回数が異なる結果ディレクトリもまとめて集計する（既定では停止）")
    parser.add_argument("--gap", type=float, default=5, help="同じタスクで方式間の総合点（0〜100）の差がこれ未満なら人間レビュー")
    parser.add_argument("--spread", type=float, default=2, help="試行間の点差がこれ以上なら人間レビュー")
    args = parser.parse_args(argv)
    if args.manifest is None:
        if len(args.paths) != 2:
            parser.error("results/<eval_id> と manifest を指定するか、--manifest を使ってください")
        *args.results_dirs, args.manifest = args.paths
    else:
        args.results_dirs = args.paths
    if args.out_dir is None:
        if len(args.results_dirs) != 1:
            parser.error("結果ディレクトリが 1 つでない場合は --out-dir が必要です")
        args.out_dir = args.results_dirs[0]
    return args


def evaluation_condition(results_dir: Path) -> dict | None:
    """run_meta.json から、点数を比べてよいかを決める評価条件（Judge・評価器のバージョン・ルーブリック・採点回数）を取り出す。"""
    path = results_dir / "run_meta.json"
    if not path.is_file():
        return None
    meta = json.loads(path.read_text(encoding="utf-8"))
    judge = meta.get("judge") or {}
    return {
        "judge": f"{judge.get('deployment', '')}@{judge.get('version', '')}",
        "evaluator_versions": dict(sorted((meta.get("evaluator_versions") or {}).items())),
        "rubric": meta.get("rubric"),
        "repeats": meta.get("repeats"),
    }


def judge_model(results_dir: Path) -> str | None:
    """run_meta.json に記録された Judge のモデル名（無ければデプロイ名）。"""
    path = results_dir / "run_meta.json"
    if not path.is_file():
        return None
    judge = json.loads(path.read_text(encoding="utf-8")).get("judge") or {}
    return judge.get("model") or judge.get("deployment")


def self_preference_reason(models: list[str]) -> str:
    return (f"Judge と同系統のモデル（{', '.join(sorted(set(models)))}）で生成した側を優位と判定"
            "（自己選好バイアスの可能性）")


def check_conditions(results_dirs: list[Path], allow_mixed: bool) -> list[dict]:
    """複数の結果ディレクトリを集計する前に、評価条件がそろっているかを確かめる。"""
    conditions = [{"results_dir": str(d), "condition": evaluation_condition(d)} for d in results_dirs]
    if len(results_dirs) > 1 and not allow_mixed:
        unknown = [c["results_dir"] for c in conditions if c["condition"] is None]
        if unknown:
            raise SystemExit(f"run_meta.json がなく評価条件を確認できません: {unknown}"
                             "（確認せずに集計するなら --allow-mixed-conditions）")
        distinct = {json.dumps(c["condition"], sort_keys=True) for c in conditions}
        if len(distinct) > 1:
            raise SystemExit("評価条件（Judge・評価器のバージョン・ルーブリック・採点回数）が異なる結果は、同じ物差しで比べられません:\n"
                             + "\n".join(f"  {c['results_dir']}: {json.dumps(c['condition'], ensure_ascii=False)}"
                                         for c in conditions)
                             + "\n同じ条件で採点し直すか、承知の上なら --allow-mixed-conditions を付けてください")
    return conditions


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    manifest = {r["run_id"]: r for r in load_jsonl(args.manifest)}
    for results_dir in args.results_dirs:
        if not results_dir.is_dir():
            raise SystemExit(f"{results_dir}: 結果ディレクトリがありません")
    conditions = check_conditions(args.results_dirs, args.allow_mixed_conditions)
    judges = {d: judge_model(d) for d in args.results_dirs}

    trials: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    judged_by: dict[str, set[str]] = defaultdict(set)
    for results_dir in args.results_dirs:
        for path in sorted(results_dir.glob("trial*.jsonl")):
            for row in load_jsonl(path):
                run_id = row["datasource_item"]["run_id"]
                if judges[results_dir]:
                    judged_by[run_id].add(judges[results_dir])
                for r in row["results"]:
                    trials[run_id][r["name"]].append(r)

    runs = {}
    for run_id, by_key in trials.items():
        s = summarize_run(run_id, by_key, args.spread)
        meta = manifest.get(run_id)
        if meta is None:
            s["human_review"].append("manifest に記載がない")
            meta = {}
        s.update({k: meta.get(k) for k in ("task_id", "variant", "attempt")})
        s["metrics_delta"] = meta.get("metrics_delta")
        s["metrics_status"] = meta.get("metrics_status")
        s["metrics_errors"] = meta.get("metrics_errors", {})
        s["agent_models"] = meta.get("agent_models")
        s["judge_same_family"] = sorted({m for j in judged_by[run_id] for m in same_family_models(s["agent_models"], j)})
        if s["metrics_status"] == "unavailable" or s["metrics_errors"]:
            s["metrics_delta"] = None
            s["human_review"].append("機械的な品質指標が計測不能: " + json.dumps(s["metrics_errors"], ensure_ascii=False))
        else:
            s["human_review"] += metric_flags(s["metrics_delta"])
        runs[run_id] = s

    # (task, variant) ごとに、全試行（不合格を含む）と Judge 済みの Run をまとめる
    cells = defaultdict(lambda: {"attempts": [], "judged": []})
    for m in manifest.values():
        cells[(m["task_id"], m["variant"])]["attempts"].append(m)
    for s in runs.values():
        if s["task_id"] is not None:
            cells[(s["task_id"], s["variant"])]["judged"].append(s)

    tasks = sorted({t for t, _ in cells})
    variants = sorted({v for _, v in cells})

    def cell_total(task, variant):
        return mean([s["total"] for s in cells[(task, variant)]["judged"] if s["total"] is not None])

    def cell_passed(task, variant):
        return sum(1 for a in cells[(task, variant)]["attempts"] if a.get("hard_gate", "pass") == "pass")

    def cell_same_family(task, variant):
        return sorted({m for s in cells[(task, variant)]["judged"] for m in s["judge_same_family"]})

    # タスクごとの方式間比較。片方しか合格しなかったタスクは合格した方の勝ちとする
    comparisons = []
    for a, b in combinations(variants, 2):
        wins = losses = ties = 0
        diffs, details = [], []
        for task in tasks:
            if (task, a) not in cells or (task, b) not in cells:
                continue
            ta, tb = cell_total(task, a), cell_total(task, b)
            pa, pb = cell_passed(task, a), cell_passed(task, b)
            if ta is not None and tb is not None:
                diff = ta - tb
                diffs.append(diff)
                outcome = "a" if diff > 0 else "b" if diff < 0 else "tie"
                wins += diff > 0
                losses += diff < 0
                ties += diff == 0
                if abs(diff) < args.gap:
                    for s in cells[(task, a)]["judged"] + cells[(task, b)]["judged"]:
                        s["human_review"].append(f"{task}: 方式間の総合点差 {abs(diff):.1f} < {args.gap:g}")
                # 同系統の側を優位とした判定だけを疑う（逆向きなら、バイアスがあっても結論は保守的になる）
                winner, loser = (a, b) if diff > 0 else (b, a)
                favored = cell_same_family(task, winner)
                if diff != 0 and favored and not cell_same_family(task, loser):
                    for s in cells[(task, a)]["judged"] + cells[(task, b)]["judged"]:
                        s["human_review"].append(f"{task}: {self_preference_reason(favored)}")
            elif ta is not None and pb == 0:
                outcome, wins = "a_only_passed", wins + 1
            elif tb is not None and pa == 0:
                outcome, losses = "b_only_passed", losses + 1
            else:
                outcome = "both_failed" if pa == 0 and pb == 0 else "not_judged"
            details.append({"task_id": task, "outcome": outcome, "total_a": ta, "total_b": tb})
        comparisons.append({
            "a": a, "b": b, "wins_a": wins, "wins_b": losses, "ties": ties,
            "p_value_sign_test": sign_test(wins, losses),
            "mean_total_diff": mean(diffs),
            "tasks": details,
        })

    variant_stats = {}
    for v in variants:
        attempts = [a for (t, vv), c in cells.items() if vv == v for a in c["attempts"]]
        judged = [s for (t, vv), c in cells.items() if vv == v for s in c["judged"]]
        passed = sum(1 for a in attempts if a.get("hard_gate", "pass") == "pass")
        known = [s for s in judged if s["agent_models"]]
        variant_stats[v] = {
            "attempts": len(attempts),
            "hard_gate_pass_rate": passed / len(attempts) if attempts else None,
            "judged_runs": len(judged),
            # 使ったモデルが記録された Run のうち、Judge と同系統のモデルを使った割合
            "judge_same_family_rate": mean([1.0 if s["judge_same_family"] else 0.0 for s in known]),
            "mean_total": mean([s["total"] for s in judged if s["total"] is not None]),
            "mean_req_met_rate": mean([s["req_met_rate"] for s in judged if s["req_met_rate"] is not None]),
            "mean_scores": {k: mean([s["scores"][k] for s in judged if k in s["scores"]]) for k in ORDINAL_KEYS},
            "critical_rate": mean([1.0 if s["critical_issues"] else 0.0 for s in judged]),
            # コストは不合格の試行も含めた 1 試行あたりの平均
            **{f"mean_{k}": mean([a[k] for a in attempts if a.get(k) is not None]) for k in COST_KEYS},
            "mean_metrics_delta": {
                k: mean([s["metrics_delta"][k] for s in judged
                         if s.get("metrics_delta") and s["metrics_delta"].get(k) is not None])
                for k in METRIC_LABELS
            },
        }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "summary.jsonl").write_text(
        "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in runs.values()), encoding="utf-8"
    )
    (args.out_dir / "variants.json").write_text(
        json.dumps({"variants": variant_stats, "comparisons": comparisons, "weights": WEIGHTS,
                    "conditions": conditions}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("## Run ごと")
    print("\t".join(["task", "variant", "try", "run_id", *ORDINAL_KEYS, "total", "req", "conf", "review"]))
    for s in sorted(runs.values(), key=lambda s: (str(s["task_id"]), str(s["variant"]), s["attempt"] or 0)):
        row = [s["task_id"], s["variant"], s["attempt"], s["run_id"]]
        row += [f"{s['scores'].get(k, '-')}" for k in ORDINAL_KEYS]
        row += [fmt(s["total"]), fmt(s["req_met_rate"], 2), s["confidence"], "要" if s["human_review"] else "-"]
        print("\t".join(str(c) for c in row))
        for f in s["human_review"]:
            print(f"    - {f}")

    print("\n## 方式ごと")
    print("variant\tattempts\tpass_rate\tjudged\ttotal\treq_met\tcritical\tcost_usd\ttokens_in\ttokens_out\tsec")
    for v, st in variant_stats.items():
        print("\t".join(str(c) for c in [
            v, st["attempts"], fmt(st["hard_gate_pass_rate"], 2), st["judged_runs"], fmt(st["mean_total"]),
            fmt(st["mean_req_met_rate"], 2), fmt(st["critical_rate"], 2), fmt(st["mean_cost_usd"], 3),
            fmt(st["mean_tokens_in"], 0), fmt(st["mean_tokens_out"], 0), fmt(st["mean_duration_sec"], 0),
        ]))

    if any(v is not None for st in variant_stats.values() for v in st["mean_metrics_delta"].values()):
        print("\n## 方式ごとの指標の差分（Judge 済みの Run の平均）")
        print("\t".join(["variant", *METRIC_LABELS]))
        for v, st in variant_stats.items():
            print("\t".join([v, *(fmt(st["mean_metrics_delta"][k], 3) for k in METRIC_LABELS)]))

    if any(st["judge_same_family_rate"] for st in variant_stats.values()):
        print(f"\n## Judge（{', '.join(sorted({j for j in judges.values() if j}))}）と同系統のモデルを使った Run の割合")
        for v, st in variant_stats.items():
            print(f"{v}\t{fmt(st['judge_same_family_rate'], 2)}")
        print("  ※ 同系統の側を優位とした判定は人間レビューに回します。結論に使う前に別系統の Judge でも採点してください")

    print("\n## 方式どうしの比較（タスク単位の勝敗）")
    for c in comparisons:
        print(f"{c['a']} vs {c['b']}: {c['wins_a']} 勝 {c['wins_b']} 敗 {c['ties']} 分 "
              f"/ 総合点差の平均 {fmt(c['mean_total_diff'])} / 符号検定 p={c['p_value_sign_test']:.3f}")
    if sum(c["wins_a"] + c["wins_b"] for c in comparisons) < 10:
        print("  ※ 勝敗のついたタスクが 10 件未満のため、p 値はほぼ有意になりません。タスク数を増やしてください")

    reviewed = sum(1 for s in runs.values() if s["human_review"])
    print(f"\n人間レビュー対象: {reviewed}/{len(runs)} 件  → {args.out_dir}")


if __name__ == "__main__":
    main()
