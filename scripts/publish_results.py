"""評価結果を可視化基盤（infra/main.bicep で作るリソース）に送る。

- Cosmos DB: 評価データの保存（パーティションキー /project、type で種類を区別）
- Application Insights: カスタムイベントとして送り、Workbook / Log Analytics で可視化する
  （APIM が計測したトークン利用量は APIM 側から同じ Application Insights に入る）

使い方:
  # Hard Gate の結果・コスト・指標だけ（Judge 前、または不合格の試行）
  python scripts/publish_results.py --project shop --manifest datasets/manifest.jsonl
  # Judge の結果も送る（aggregate.py を先に実行して summary.jsonl / variants.json を作っておく）
  python scripts/publish_results.py --project shop --manifest datasets/manifest.jsonl --results-dir results/<eval_id>
  # ペア比較だけ、または絶対評価と一緒に送る（それぞれの評価 ID を保持）
  python scripts/publish_results.py --project shop --manifest datasets/manifest.jsonl --pairwise-dir results/<pairwise_eval_id>
  # 人間レビューの最終判断を送る（calibration.py export / export-pairs のシートを人間が埋めたもの）
  python scripts/publish_results.py --project shop --manifest datasets/manifest.jsonl --human-reviews review.csv
  python scripts/publish_results.py --project shop --manifest datasets/manifest.jsonl \
    --pairwise-dir results/<pairwise_eval_id> --human-pair-reviews pairs_review.csv --pair-key review_pairs/_key.json
  # 送らずに中身を確認
  python scripts/publish_results.py ... --dry-run
  # 種類を絞って送る（CI の llm-judge-compare は Run ごとの送信と重複しないよう比較の結果だけを送る）
  python scripts/publish_results.py ... --types comparison,pairwise,pair

送り先は環境変数 LLM_JUDGE_COSMOS_ENDPOINT と APPLICATIONINSIGHTS_CONNECTION_STRING（または引数）で指定する。
片方だけでもよい。Cosmos DB は Entra ID 認証（az login 済みのユーザーか CI の OIDC）で書き込む。
"""

import argparse
import csv
import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

COSMOS_DATABASE = "llmjudge"
COSMOS_CONTAINER = "results"
# calibration.py の採点シートの観点（publish_results.py は単体でも動くよう config に依存しない）
HUMAN_SCORE_KEYS = ("requirements", "test_quality", "design", "maintainability", "change_scope",
                    "robustness", "explanation")


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def load_json(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def safe_id(*parts: str) -> str:
    # Cosmos DB の id に使えない文字を置き換える
    return ":".join(str(x) for x in parts).translate(str.maketrans({c: "_" for c in "/\\?#"}))


def evaluation_documents(project: str, results_dir: Path, now: str) -> list[dict]:
    summary_path = results_dir / "summary.jsonl"
    pairwise = load_json(results_dir / "pairwise_summary.json")
    if not summary_path.exists() and pairwise is None:
        raise SystemExit(f"{results_dir}: summary.jsonl または pairwise_summary.json が必要です。対応する集計を実行してください")
    meta = load_json(results_dir / "run_meta.json") or {}
    eval_id = meta.get("eval_id", results_dir.name)
    judge = meta.get("judge", {})
    common = {"project": project, "eval_id": eval_id, "published_at": now,
              "judge": f"{judge.get('deployment', '')}@{judge.get('version', '')}",
              "evaluator_versions": meta.get("evaluator_versions", {})}
    docs = []
    if summary_path.exists():
        docs += [{"id": safe_id("run", eval_id, s["run_id"]), "type": "run", **common, **s}
                 for s in load_jsonl(summary_path)]
        variants = load_json(results_dir / "variants.json")
        if variants:
            docs += [{"id": safe_id("variant", eval_id, v), "type": "variant", "variant": v, **common, **st}
                     for v, st in variants["variants"].items()]
            docs += [{"id": safe_id("comparison", eval_id, c["a"], c["b"]), "type": "comparison", **common, **c}
                     for c in variants["comparisons"]]
    if pairwise is not None:
        docs += [{"id": safe_id("pairwise", eval_id, c["a"], c["b"]), "type": "pairwise", **common, **c,
                  "position_bias": pairwise.get("position_bias")} for c in pairwise["comparisons"]]
        for pair in pairwise["pairs"]:
            reasons = list(pair.get("human_review", []))
            reason = "ペア比較: A/B の順序で判定が逆転"
            if pair.get("position_inconsistent") and reason not in reasons:
                reasons.append(reason)
            docs.append({"id": safe_id("pair", eval_id, pair["pair_id"]), "type": "pair", **common,
                         **pair, "human_review": reasons})
    return docs


def build_documents(project: str, manifest: list[dict], results_dir: Path | None,
                    pairwise_dir: Path | None = None) -> list[dict]:
    if pairwise_dir is not None and not (pairwise_dir / "pairwise_summary.json").is_file():
        raise SystemExit(f"{pairwise_dir}: pairwise_summary.json が必要です。run_pairwise.py で集計してください")
    now = datetime.now(timezone.utc).isoformat()
    docs = [{"id": safe_id("attempt", m["run_id"]), "type": "attempt", "project": project, "published_at": now, **m}
            for m in manifest]
    directories = dict.fromkeys(p.resolve() for p in (results_dir, pairwise_dir) if p is not None)
    for directory in directories:
        docs.extend(evaluation_documents(project, directory, now))
    return docs


# ---------- 人間レビューの最終判断 ----------

def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def human_run_documents(project: str, manifest: list[dict], review_csv: Path, now: str) -> list[dict]:
    """calibration.py export の採点シート（人間が埋めたもの）を、Run ごとの最終判断として送る。"""
    meta = {m["run_id"]: m for m in manifest}
    docs = []
    for row in read_csv(review_csv):
        scores = {k: float(v) for k, v in row.items() if k in HUMAN_SCORE_KEYS and (v or "").strip()}
        critical = (row.get("uncaught_critical") or "").strip()
        if not scores and not critical:
            continue
        m = meta.get(row["run_id"], {})
        decision = ", ".join(f"{k}={v:g}" for k, v in scores.items())
        if critical:
            decision += ("; " if decision else "") + f"uncaught_critical={'問題なし' if critical == '1' else '問題あり'}"
        docs.append({"id": safe_id("human", "run", row["run_id"]), "type": "human_review", "project": project,
                     "published_at": now, "review_kind": "run", "review_key": row["run_id"],
                     "run_id": row["run_id"], "task_id": m.get("task_id"), "variant": m.get("variant"),
                     "scores": scores, "uncaught_critical": None if not critical else critical == "1",
                     "decision": decision, "comment": (row.get("comment") or "").strip()})
    return docs


def human_pair_documents(project: str, review_csv: Path, key_path: Path, pairwise_dir: Path,
                         now: str) -> list[dict]:
    """calibration.py export-pairs の比較シートを、組ごとの最終判断（どちらをマージするか）として送る。"""
    key = json.loads(key_path.read_text(encoding="utf-8"))
    eval_id = (load_json(pairwise_dir / "run_meta.json") or {}).get("eval_id", pairwise_dir.name)
    pairs = {p["pair_id"]: p for p in (load_json(pairwise_dir / "pairwise_summary.json") or {"pairs": []})["pairs"]}
    docs = []
    for row in read_csv(review_csv):
        choice = (row.get("better") or "").strip().upper()
        if choice not in ("A", "B", "SAME"):
            continue
        k = key[row["review_id"]]
        pair = pairs.get(k["pair_id"])
        if pair is None:
            raise SystemExit(f"{k['pair_id']} が {pairwise_dir} の pairwise_summary.json にありません")
        better_run = None if choice == "SAME" else k[choice]
        better = "same" if better_run is None else pair["variant_a"] if better_run == pair["run_a"] else pair["variant_b"]
        docs.append({"id": safe_id("human", "pair", eval_id, k["pair_id"]), "type": "human_review",
                     "project": project, "published_at": now, "review_kind": "pair",
                     "review_key": f"{eval_id}:{k['pair_id']}", "eval_id": eval_id, "pair_id": k["pair_id"],
                     "task_id": pair["task_id"], "run_a": pair["run_a"], "run_b": pair["run_b"],
                     "variant_a": pair["variant_a"], "variant_b": pair["variant_b"],
                     "better": better, "better_run": better_run, "decision": f"マージすべき: {better}",
                     "comment": (row.get("comment") or "").strip()})
    return docs


def scope_documents(docs: list[dict], experiment_id: str | None) -> list[dict]:
    """実験 ID を付け、文書 ID と Run 単位の人間レビューの結合キーを実験ごとに分ける。

    評価サービスは同じ Run を別の実験（評価条件）で採点し直すため、実験を含めないと
    attempt:<run_id> や human:run:<run_id> が実験をまたいで同じになり、前の実験の文書を上書きする。
    ペア比較の結合キー（<pairwise eval_id>:<pair_id>）は評価ごとに一意なのでそのまま使う。
    """
    if not experiment_id:
        return docs
    scoped = []
    for doc in docs:
        doc = {**doc, "experiment_id": experiment_id, "id": safe_id("experiment", experiment_id, doc["id"])}
        if doc["type"] == "human_review" and doc.get("review_kind") == "run":
            doc["review_key"] = f"{experiment_id}:{doc['run_id']}"
        scoped.append(doc)
    return scoped


# ---------- Application Insights ----------

EVENT_NAMES = {"attempt": "LlmJudgeAttempt", "run": "LlmJudgeRun", "variant": "LlmJudgeVariant",
               "comparison": "LlmJudgeComparison", "pairwise": "LlmJudgePairwise", "pair": "LlmJudgePair",
               "human_review": "LlmJudgeHumanReview"}


def flatten(doc: dict) -> tuple[dict[str, str], dict[str, float]]:
    """数値は measurements、それ以外は properties に分ける。入れ子は prefix 付きで平らにする。"""
    props, measures = {}, {}

    def walk(prefix: str, value):
        if isinstance(value, bool):
            props[prefix] = str(value).lower()
        elif isinstance(value, (int, float)):
            measures[prefix] = float(value)
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(f"{prefix}.{k}" if prefix else k, v)
        elif isinstance(value, list):
            props[prefix] = json.dumps(value, ensure_ascii=False)[:8000]
            measures[f"{prefix}.count"] = float(len(value))
        elif value is not None:
            props[prefix] = str(value)[:8000]

    walk("", {k: v for k, v in doc.items() if k not in ("id", "tasks")})
    return props, measures


def parse_connection_string(cs: str) -> tuple[str, str]:
    parts = dict(p.split("=", 1) for p in cs.split(";") if "=" in p)
    endpoint = parts.get("IngestionEndpoint", "https://dc.services.visualstudio.com").rstrip("/")
    return parts["InstrumentationKey"], endpoint


def send_appinsights(cs: str, docs: list[dict]) -> None:
    ikey, endpoint = parse_connection_string(cs)
    time = datetime.now(timezone.utc).isoformat()
    envelopes = []
    for doc in docs:
        props, measures = flatten(doc)
        envelopes.append({
            "name": "Microsoft.ApplicationInsights.Event",
            "time": time,
            "iKey": ikey,
            "data": {"baseType": "EventData", "baseData": {
                "ver": 2, "name": EVENT_NAMES[doc["type"]], "properties": props, "measurements": measures,
            }},
        })
    req = urllib.request.Request(f"{endpoint}/v2.1/track", data=json.dumps(envelopes).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as res:
        body = json.loads(res.read() or b"{}")
    if body.get("itemsAccepted", len(envelopes)) != len(envelopes):
        raise SystemExit(f"Application Insights が一部のイベントを拒否しました: {body}")
    print(f"Application Insights: {len(envelopes)} 件のイベントを送信")


# ---------- Cosmos DB ----------

def send_cosmos(endpoint: str, docs: list[dict]) -> None:
    from azure.cosmos import CosmosClient

    from azure_auth import credential

    container = CosmosClient(endpoint, credential=credential()) \
        .get_database_client(COSMOS_DATABASE).get_container_client(COSMOS_CONTAINER)
    for doc in docs:
        container.upsert_item(doc)
    print(f"Cosmos DB: {len(docs)} 件を保存（{COSMOS_DATABASE}/{COSMOS_CONTAINER}）")


def parse_types(value: str | None) -> set[str] | None:
    if not value:
        return None
    types = {t.strip() for t in value.split(",") if t.strip()}
    unknown = types - EVENT_NAMES.keys()
    if unknown:
        raise SystemExit(f"--types に未知の種類があります: {sorted(unknown)}（指定できるもの: {sorted(EVENT_NAMES)}）")
    return types


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--project", required=True, help="プロジェクト名（ダッシュボードでの絞り込みに使う）")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--results-dir", type=Path, help="集計済みの絶対評価またはペア比較の results/<eval_id>")
    p.add_argument("--pairwise-dir", type=Path, help="run_pairwise.py の results/<pairwise_eval_id>（単独でも指定可）")
    p.add_argument("--human-reviews", type=Path, help="人間が埋めた calibration.py export の採点シート（review.csv）")
    p.add_argument("--human-pair-reviews", type=Path, help="人間が埋めた calibration.py export-pairs の比較シート")
    p.add_argument("--pair-key", type=Path, help="export-pairs の _key.json（--human-pair-reviews と一緒に指定）")
    p.add_argument("--cosmos-endpoint", default=os.environ.get("LLM_JUDGE_COSMOS_ENDPOINT"))
    p.add_argument("--appinsights-connection-string", default=os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"))
    p.add_argument("--experiment-id", help="評価サービスの実験 ID。文書 ID と Run の人間レビューの結合キーを実験ごとに分ける")
    p.add_argument("--types", help="送る種類をカンマ区切りで絞る（例: comparison,pairwise,pair）。既定はすべて")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    types = parse_types(args.types)
    if args.human_pair_reviews and not (args.pair_key and args.pairwise_dir):
        raise SystemExit("--human-pair-reviews には --pair-key と --pairwise-dir が必要です")

    manifest = load_jsonl(args.manifest)
    docs = build_documents(args.project, manifest, args.results_dir, args.pairwise_dir)
    now = docs[0]["published_at"] if docs else datetime.now(timezone.utc).isoformat()
    if args.human_reviews:
        docs += human_run_documents(args.project, manifest, args.human_reviews, now)
    if args.human_pair_reviews:
        docs += human_pair_documents(args.project, args.human_pair_reviews, args.pair_key, args.pairwise_dir, now)
    docs = scope_documents(docs, args.experiment_id)
    if types is not None:
        docs = [d for d in docs if d["type"] in types]
    if args.dry_run:
        for doc in docs:
            print(json.dumps(doc, ensure_ascii=False)[:400])
        print(f"（dry-run）{len(docs)} 件")
        return
    if not docs:
        print("送る内容がありません")
        return
    if not args.cosmos_endpoint and not args.appinsights_connection_string:
        raise SystemExit("送信先がありません。LLM_JUDGE_COSMOS_ENDPOINT か APPLICATIONINSIGHTS_CONNECTION_STRING を設定してください")
    if args.cosmos_endpoint:
        send_cosmos(args.cosmos_endpoint, docs)
    if args.appinsights_connection_string:
        send_appinsights(args.appinsights_connection_string, docs)


if __name__ == "__main__":
    main()
