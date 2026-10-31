"""人間と Judge の答え合わせ。

絶対評価（観点ごとの点数）:
  1) python scripts/calibration.py export datasets/runs.jsonl --sample 10 --out review.csv --packet-dir review/
  2) 人間が review/ の資料を読み、review.csv の空欄（各観点 0〜5、uncaught_critical は 1=問題なし / 0=問題あり）を埋める
  3) python scripts/calibration.py compare review.csv results/<eval_id>/summary.jsonl \
       --report calibration_report.md --history results/calibration_history.jsonl
     ずれた観点と Judge の理由をまとめた改善レポートを書き出し、一致率の推移を記録する

ペア比較（どちらをマージすべきか）:
  1) python scripts/calibration.py export-pairs datasets/runs.jsonl results/<pairwise_eval_id> --out pairs_review.csv --packet-dir review_pairs/
  2) 人間が pairs_review.csv の better 列に A / B / same を入れる
  3) python scripts/calibration.py compare-pairs pairs_review.csv review_pairs/_key.json results/<pairwise_eval_id>

人間レビューに回った Run / 組だけを書き出す（最終判断用）:
  python scripts/calibration.py export datasets/runs.jsonl --flagged results/<eval_id>/summary.jsonl --packet-dir review/
  python scripts/calibration.py export-pairs datasets/runs.jsonl results/<pairwise_eval_id> --flagged
  埋めたシートは publish_results.py --human-reviews / --human-pair-reviews で可視化基盤に送る

資料には方式名も Judge の点数も載せない（ブラインド）。_key.json は採点が終わるまで開かないこと。
"""

import argparse
import csv
import json
import random
from datetime import datetime, timezone
from pathlib import Path

from config import DATASET_FIELDS, EVALUATORS
from stats_utils import fmt, mean

ORDINAL_KEYS = [k for k, s in EVALUATORS.items() if s["scoring"] == "ordinal"]
SCORE_COLUMNS = [*ORDINAL_KEYS, "uncaught_critical", "comment"]


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def run_section(title: str, row: dict, *, include_context: bool = False, include_metrics: bool = False) -> str:
    metrics = (row.get("metrics_delta") or "なし").strip()
    return (
        f"## {title}\n\n### CI 結果\n\n```\n{row['ci_summary'].strip()}\n```\n\n"
        + (f"### 関連する既存コード\n\n{row.get('context') or 'なし'}\n\n"
           f"### プロジェクトの Skill（評価時の入力）\n\n{row.get('project_skills') or 'なし'}\n\n"
           if include_context else "")
        + (f"### 機械的な指標の差分\n\n```\n{metrics}\n```\n\n" if include_metrics and metrics != "なし" else "")
        + f"### 差分\n\n```diff\n{row['response'].strip()}\n```\n"
    )


def flagged_ids(rows: list[dict], key: str) -> set[str]:
    return {r[key] for r in rows if r.get("human_review")}


def export(args) -> None:
    rows = load_jsonl(args.dataset)
    if args.flagged:
        flagged = flagged_ids(load_jsonl(args.flagged), "run_id")
        rows = [r for r in rows if r["run_id"] in flagged]
        missing = flagged - {r["run_id"] for r in rows}
        if missing:
            print(f"注意: 評価データに無い要レビュー Run を飛ばしました: {sorted(missing)}")
    random.Random(args.seed).shuffle(rows)
    # 要レビューの Run は全件、無作為抽出（答え合わせ）は既定 10 件
    sample = args.sample if args.sample is not None else (len(rows) if args.flagged else 10)
    rows = rows[:sample]
    columns = ["run_id", *(["packet_file"] if args.packet_dir else DATASET_FIELDS), *SCORE_COLUMNS]
    if args.packet_dir:
        args.packet_dir.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for r in rows:
            if args.packet_dir:
                path = args.packet_dir / f"{r['run_id']}.md"
                path.write_text(
                    f"# {r['run_id']}\n\n## タスク\n\n{r['query'].strip()}\n\n"
                    f"## PR 説明\n\n{r['pr_description'].strip()}\n\n"
                    f"{run_section('実装', r, include_context=True, include_metrics=True)}",
                    encoding="utf-8",
                )
                writer.writerow({"run_id": r["run_id"], "packet_file": path.name})
            else:
                writer.writerow({"run_id": r["run_id"], **{k: r.get(k, "") for k in DATASET_FIELDS}})
    print(f"{len(rows)} 件の採点シートを書き出しました → {args.out}")
    print("採点基準は evaluators/*.md と同じものを人間にも渡してください")


def export_pairs(args) -> None:
    dataset = {r["run_id"]: r for r in load_jsonl(args.dataset)}
    pairs = json.loads((args.pairwise_dir / "pairs.json").read_text(encoding="utf-8"))
    if args.flagged:
        summary = json.loads((args.pairwise_dir / "pairwise_summary.json").read_text(encoding="utf-8"))
        flagged = flagged_ids(summary["pairs"], "pair_id")
        pairs = [p for p in pairs if p["pair_id"] in flagged]
    rng = random.Random(args.seed)
    args.packet_dir.mkdir(parents=True, exist_ok=True)
    key = {}
    with args.out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["review_id", "packet_file", "better", "comment"])
        writer.writeheader()
        for i, p in enumerate(rng.sample(pairs, len(pairs)), start=1):
            review_id = f"Q{i:02d}"
            a, b = (p["run_a"], p["run_b"]) if rng.random() < 0.5 else (p["run_b"], p["run_a"])
            key[review_id] = {"pair_id": p["pair_id"], "A": a, "B": b}
            path = args.packet_dir / f"{review_id}.md"
            # ペア比較の Judge と同じ入力（要件・Skill・各実装の CI・指標の差分・差分）を載せる
            path.write_text(
                f"# {review_id}: どちらをマージすべきか\n\n## タスク\n\n{dataset[a]['query'].strip()}\n\n"
                f"## プロジェクトの Skill（評価時の入力）\n\n{dataset[a].get('project_skills') or 'なし'}\n\n"
                f"{run_section('実装 A', dataset[a], include_metrics=True)}\n"
                f"{run_section('実装 B', dataset[b], include_metrics=True)}",
                encoding="utf-8",
            )
            writer.writerow({"review_id": review_id, "packet_file": path.name})
    (args.packet_dir / "_key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")
    print(f"{len(key)} 組の比較シートを書き出しました → {args.out}（better 列に A / B / same）")
    print(f"対応表 {args.packet_dir / '_key.json'} は採点が終わるまで開かないでください")


def compare_pairs(args) -> None:
    key = json.loads(args.key.read_text(encoding="utf-8"))
    summary = json.loads((args.pairwise_dir / "pairwise_summary.json").read_text(encoding="utf-8"))
    judge = {p["pair_id"]: p for p in summary["pairs"]}
    with args.human.open(encoding="utf-8-sig") as f:
        human = [r for r in csv.DictReader(f) if r.get("better", "").strip().upper() in ("A", "B", "SAME")]

    def sign(x: float) -> int:
        return (x > 0) - (x < 0)

    agree = []
    print("review\ttask\t人間\tJudge\t一致")
    for r in human:
        k, p = key[r["review_id"]], judge[key[r["review_id"]]["pair_id"]]
        choice = r["better"].strip().upper()
        # どちらも run_a（variant_a）から見た向きにそろえる
        human_dir = 0 if choice == "SAME" else (1 if k[choice] == p["run_a"] else -1)
        judge_dir = sign(p["pref"] or 0)
        agree.append(human_dir == judge_dir)
        label = {1: p["variant_a"], -1: p["variant_b"], 0: "同等"}
        print(f"{r['review_id']}\t{p['task_id']}\t{label[human_dir]}\t{label[judge_dir]}\t{'○' if agree[-1] else '×'}")
    if agree:
        print(f"\n方向の一致率: {sum(agree)}/{len(agree)}（{sum(agree) / len(agree):.0%}）")


def spearman(xs: list[float], ys: list[float]) -> float | None:
    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        result = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            for k in range(i, j + 1):
                result[order[k]] = (i + j) / 2
            i = j + 1
        return result

    if len(xs) < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = mean(rx), mean(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    var = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return cov / var if var else None


AGREE_MIN, BIAS_MAX = 0.8, 0.5


def judge_reasons(results_dir: Path | None) -> dict[tuple[str, str], list[str]]:
    """results/<eval_id>/trial*.jsonl から (run_id, 観点) → 各試行の reason を集める。"""
    reasons = {}
    if not results_dir:
        return reasons
    for path in sorted(results_dir.glob("trial*.jsonl")):
        for row in load_jsonl(path):
            for r in row["results"]:
                reasons.setdefault((row["datasource_item"]["run_id"], r["name"]), []).append(r.get("reason") or "")
    return reasons


def advice(stat: dict) -> list[str]:
    tips = []
    if stat["within1"] is not None and stat["within1"] < AGREE_MIN:
        tips.append(f"±1 以内の一致率が {AGREE_MIN} 未満。採点基準の各段階の境界を具体例付きで書き分ける")
    if stat["bias"] is not None and stat["bias"] > BIAS_MAX:
        tips.append("Judge が人間より甘い。減点すべき典型例を採点基準に追加する")
    if stat["bias"] is not None and stat["bias"] < -BIAS_MAX:
        tips.append("Judge が人間より厳しい。減点しない例（許容される変更・好みの問題）を明記する")
    if stat["spearman"] is not None and stat["spearman"] < 0.5:
        tips.append("順位相関が低い。観点の定義があいまいか、他の観点と混ざっている。見る点を絞る")
    return tips


def compare(args) -> None:
    judge = {s["run_id"]: s for s in load_jsonl(args.summary)}
    with args.human.open(encoding="utf-8-sig") as f:
        human = [r for r in csv.DictReader(f) if r["run_id"] in judge]

    stats, disagreements = {}, []
    for key in ORDINAL_KEYS:
        rows = [r for r in human if r.get(key, "").strip() and key in judge[r["run_id"]]["scores"]]
        if not rows:
            continue
        pairs = [(float(r[key]), judge[r["run_id"]]["scores"][key]) for r in rows]
        hs, js = [h for h, _ in pairs], [j for _, j in pairs]
        stats[key] = {
            "n": len(pairs),
            "exact": mean([1.0 if h == j else 0.0 for h, j in pairs]),
            "within1": mean([1.0 if abs(h - j) <= 1 else 0.0 for h, j in pairs]),
            "bias": mean([j - h for h, j in pairs]),
            "spearman": spearman(hs, js),
        }
        disagreements += [{"run_id": r["run_id"], "key": key, "human": h, "judge": j, "comment": r.get("comment", "")}
                          for r, (h, j) in zip(rows, pairs) if abs(h - j) >= 2]

    crit = [(r, r["uncaught_critical"].strip() == "1", not judge[r["run_id"]]["critical_issues"])
            for r in human if r.get("uncaught_critical", "").strip() in ("0", "1")]
    if crit:
        stats["uncaught_critical"] = {"n": len(crit), "exact": mean([1.0 if h == j else 0.0 for _, h, j in crit]),
                                      "within1": None, "bias": None, "spearman": None}
        disagreements += [{"run_id": r["run_id"], "key": "uncaught_critical", "human": h, "judge": j,
                           "comment": r.get("comment", "")} for r, h, j in crit if h != j]

    print("観点\t件数\t完全一致\t±1以内\t平均誤差(Judge-人間)\t順位相関")
    for key, st in stats.items():
        print("\t".join([key, str(st["n"]), fmt(st["exact"], 2), fmt(st["within1"], 2), fmt(st["bias"], 2), fmt(st["spearman"], 2)]))

    print("\n## 2 点以上ずれたケース（ルーブリック改善の材料）")
    for d in disagreements:
        print(f"{d['run_id']}\t{d['key']}\t人間={d['human']}\tJudge={d['judge']}")
    print(f"\n目安: ±1以内の一致率が {AGREE_MIN} 未満、または平均誤差が ±{BIAS_MAX} を超える観点は、ルーブリックの見直しを検討してください")

    meta_path = args.summary.parent / "run_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}

    if args.history:
        history = load_jsonl(args.history) if args.history.exists() else []
        previous = history[-1] if history else None
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "summary": str(args.summary),
            "judge": meta.get("judge"),
            "evaluator_versions": meta.get("evaluator_versions"),
            "stats": stats,
        }
        args.history.parent.mkdir(parents=True, exist_ok=True)
        with args.history.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"\n推移を記録しました → {args.history}")
        if previous:
            print("前回からの変化（±1以内の一致率）:")
            for key, st in stats.items():
                before = previous["stats"].get(key, {}).get("within1")
                if st["within1"] is not None and before is not None:
                    print(f"  {key}: {before:.2f} → {st['within1']:.2f}（{st['within1'] - before:+.2f}）")

    if args.report:
        reasons = judge_reasons(args.results_dir or args.summary.parent)
        names = {k: s["name"] for k, s in EVALUATORS.items()}
        lines = [
            "# Judge と人間の答え合わせ: 改善レポート", "",
            f"- 対象: `{args.summary}`（{len(human)} 件）",
            f"- Judge: {(meta.get('judge') or {}).get('deployment', '-')}@{(meta.get('judge') or {}).get('version', '-')}",
            f"- 評価器のバージョン: {json.dumps(meta.get('evaluator_versions', {}), ensure_ascii=False)}", "",
            "## 観点ごとの一致度", "",
            "| 観点 | 件数 | 完全一致 | ±1 以内 | 平均誤差（Judge−人間） | 順位相関 | 見直し |",
            "|---|---|---|---|---|---|---|",
        ]
        to_fix = {}
        for key, st in stats.items():
            tips = advice(st)
            if key == "uncaught_critical" and st["exact"] is not None and st["exact"] < AGREE_MIN:
                tips = ["重大問題の判定が人間と食い違う。重大とみなす基準（影響範囲・悪用可能性）を具体化する"]
            if tips:
                to_fix[key] = tips
            lines.append(f"| {key} | {st['n']} | {fmt(st['exact'], 2)} | {fmt(st['within1'], 2)} | "
                         f"{fmt(st['bias'], 2)} | {fmt(st['spearman'], 2)} | {'要' if tips else '-'} |")
        lines += ["", "## 見直す候補", ""]
        if not to_fix:
            lines.append("目安を下回る観点はありません。")
        for key, tips in to_fix.items():
            lines.append(f"### {key}（`evaluators/{key}.md`）")
            lines += [f"- {t}" for t in tips]
            lines.append("- 人間のコメントがプロジェクト固有のルール（命名・レイヤー・使用ライブラリなど）に基づく場合は、"
                         "ルーブリックではなく `project_skills/` に追記し、エージェントと Judge の両方に渡す")
            lines.append("")
        lines += ["## ずれたケース（2 点以上、または重大問題の判定が不一致）", ""]
        for d in disagreements:
            lines.append(f"### {d['run_id']} / {d['key']}: 人間 {d['human']} / Judge {d['judge']}")
            if d["comment"].strip():
                lines.append(f"- 人間のコメント: {d['comment'].strip()}")
            for i, reason in enumerate(reasons.get((d["run_id"], d["key"])) or reasons.get((d["run_id"], names.get(d["key"], ""))) or [], 1):
                lines.append(f"- Judge の理由（試行 {i}）: {reason.strip()}")
            lines.append("")
        lines += ["## 次の手順", "",
                  "1. 上の候補に沿って `evaluators/*.md` または `project_skills/` を直す",
                  "2. `python scripts/register_evaluators.py` で新しいバージョンを登録する",
                  "3. カナリア（`check_canaries.py`）で検出力が落ちていないことを確かめる",
                  "4. 同じ採点シートで再評価し、`--history` で一致率の推移を確認する"]
        args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"改善レポートを書き出しました → {args.report}")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    e = sub.add_parser("export")
    e.add_argument("dataset", type=Path)
    e.add_argument("--sample", type=int, help="書き出す件数（既定: 無作為抽出は 10 件、--flagged は全件）")
    e.add_argument("--flagged", type=Path, help="aggregate.py の summary.jsonl。人間レビュー対象の Run だけを書き出す")
    e.add_argument("--seed", type=int, default=0)
    e.add_argument("--out", type=Path, default=Path("review.csv"))
    e.add_argument("--packet-dir", type=Path, help="Run ごとの読みやすい資料（Markdown）を書き出す先")
    c = sub.add_parser("compare")
    c.add_argument("human", type=Path)
    c.add_argument("summary", type=Path)
    c.add_argument("--report", type=Path, help="改善レポート（Markdown）の出力先")
    c.add_argument("--history", type=Path, help="一致率の推移を追記する JSONL（例: results/calibration_history.jsonl）")
    c.add_argument("--results-dir", type=Path, help="Judge の理由を読む results/<eval_id>（既定: summary と同じディレクトリ）")
    ep = sub.add_parser("export-pairs")
    ep.add_argument("dataset", type=Path)
    ep.add_argument("pairwise_dir", type=Path)
    ep.add_argument("--seed", type=int, default=0)
    ep.add_argument("--out", type=Path, default=Path("pairs_review.csv"))
    ep.add_argument("--packet-dir", type=Path, default=Path("review_pairs"))
    ep.add_argument("--flagged", action="store_true", help="pairwise_summary.json で人間レビュー対象の組だけを書き出す")
    cp = sub.add_parser("compare-pairs")
    cp.add_argument("human", type=Path)
    cp.add_argument("key", type=Path)
    cp.add_argument("pairwise_dir", type=Path)
    args = parser.parse_args()
    {"export": export, "compare": compare, "export-pairs": export_pairs, "compare-pairs": compare_pairs}[args.command](args)


if __name__ == "__main__":
    main()
