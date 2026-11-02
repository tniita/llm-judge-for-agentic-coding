"""タスク単位の方式比較（ci/llm-judge-compare.yml）のために、Run ごとの成果物を集めて揃ったかを判定する。

llm-judge.yml は Run ごとに成果物 llm-judge-run-<task_id>-<run_id> を残す（中身は runs.jsonl・manifest.jsonl・results/<eval_id>/）。
このスクリプトはそれを集め、比較する方式 × attempt の Run がすべて揃ったか（または待ち時間を過ぎたか）を判定し、
run_pairwise.py / aggregate.py にそのまま渡せる形にまとめる。

使い方:
  # 最近の成果物があるタスクの一覧（定期実行での取りこぼし拾い）
  python scripts/collect_task.py tasks --lookback-days 14
  # タスクの成果物をダウンロード（gh CLI と GH_TOKEN が必要）
  python scripts/collect_task.py download --task-id T002 --dest artifacts/
  # 揃ったかを判定し、まとめる
  python scripts/collect_task.py merge --task-id T002 --variants fixed,hydrafusion --attempts 1 \
    --timeout-hours 24 --artifacts artifacts/ --out compare/

merge の出力（--out）:
  runs.jsonl / manifest.jsonl  比較に使う Run だけをまとめた評価データと対応表
  results_dirs.txt             Judge 済みの Run の results/<eval_id>（aggregate.py に渡す）
  collect.json                 判定結果。status は次のどれか
    complete      全方式 × 全 attempt が揃った
    timeout       待ち時間を過ぎたので、揃った分だけで比較する（missing に未提出を記録）
    waiting       まだ揃っていない
    insufficient  比較できる方式が 2 つ未満（揃った・待ち時間切れでも比較しない）
    no_runs       成果物がない
"""

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

RUN_PREFIX = "llm-judge-run-"
COMPARE_PREFIX = "llm-judge-compare-"
# 成果物名から task_id を一意に取り出せるよう、task_id にはハイフンを使わない
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.]+$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
READY_STATUSES = ("complete", "timeout")


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def artifact_task(name: str) -> str | None:
    """llm-judge-run-<task_id>-<run_id> から task_id を取り出す。形式が違えば None。"""
    if not name.startswith(RUN_PREFIX):
        return None
    task, sep, run_id = name[len(RUN_PREFIX):].partition("-")
    if not sep or not TASK_ID_RE.match(task) or not RUN_ID_RE.match(run_id):
        return None
    return task


# ---------- GitHub API（gh CLI 経由） ----------

def gh_api(path: str, *, paginate: bool = False, jq: str | None = None) -> bytes:
    cmd = ["gh", "api", path]
    if paginate:
        cmd.append("--paginate")
    if jq:
        cmd += ["--jq", jq]
    return subprocess.run(cmd, check=True, capture_output=True).stdout


def list_artifacts(repo: str) -> list[dict]:
    out = gh_api(f"repos/{repo}/actions/artifacts?per_page=100", paginate=True, jq=".artifacts[]")
    return [a for a in (json.loads(l) for l in out.decode().splitlines() if l.strip()) if not a.get("expired")]


def list_run_artifacts(repo: str) -> list[dict]:
    return [a for a in list_artifacts(repo) if artifact_task(a["name"]) is not None]


def compare_task(name: str) -> str | None:
    """llm-judge-compare-<task_id>-<fingerprint> から task_id を取り出す。"""
    if not name.startswith(COMPARE_PREFIX):
        return None
    task, sep, _ = name[len(COMPARE_PREFIX):].partition("-")
    return task if sep and TASK_ID_RE.match(task) else None


def pending_tasks(artifacts: list[dict], since: datetime) -> list[str]:
    """最近 Run の成果物があり、まだ比較していない（比較後に新しい Run が来た）タスク。"""
    latest_run: dict[str, datetime] = {}
    latest_compare: dict[str, datetime] = {}
    for a in artifacts:
        created = parse_time(a["created_at"])
        if (task := artifact_task(a["name"])) is not None:
            latest_run[task] = max(created, latest_run.get(task, created))
        elif (task := compare_task(a["name"])) is not None:
            latest_compare[task] = max(created, latest_compare.get(task, created))
    return sorted(t for t, created in latest_run.items()
                  if created >= since and (t not in latest_compare or created > latest_compare[t]))


def cmd_tasks(args) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=args.lookback_days)
    print(json.dumps(pending_tasks(list_artifacts(args.repo), since)))


def cmd_download(args) -> None:
    args.dest.mkdir(parents=True, exist_ok=True)
    index = []
    for a in list_run_artifacts(args.repo):
        if artifact_task(a["name"]) != args.task_id:
            continue
        target = args.dest / f"{a['id']}"
        with zipfile.ZipFile(io.BytesIO(gh_api(f"repos/{args.repo}/actions/artifacts/{a['id']}/zip"))) as z:
            z.extractall(target)
        index.append({"name": a["name"], "id": a["id"], "created_at": a["created_at"], "dir": target.name})
    (args.dest / "_index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{args.task_id}: {len(index)} 件の成果物をダウンロード → {args.dest}")


# ---------- 判定とまとめ ----------

def merge(task_id: str, variants: list[str], attempts: int, timeout_hours: float,
          artifacts_dir: Path, out_dir: Path, now: datetime) -> dict:
    index = json.loads((artifacts_dir / "_index.json").read_text(encoding="utf-8"))
    notes = []

    # run_id ごとに最新の成果物を採用する（同じ Run の再実行）
    latest: dict[str, dict] = {}
    for entry in index:
        directory = artifacts_dir / entry["dir"]
        manifest_path = directory / "manifest.jsonl"
        if not manifest_path.is_file():
            notes.append(f"{entry['name']}: manifest.jsonl がないため無視")
            continue
        dataset = {r["run_id"]: r for r in load_jsonl(directory / "runs.jsonl")} \
            if (directory / "runs.jsonl").is_file() else {}
        for m in load_jsonl(manifest_path):
            if m.get("task_id") != task_id:
                continue
            candidate = {"manifest": m, "dataset": dataset.get(m["run_id"]), "dir": directory,
                         "created_at": parse_time(entry["created_at"]), "artifact": entry["name"]}
            current = latest.get(m["run_id"])
            if current is None or candidate["created_at"] > current["created_at"]:
                latest[m["run_id"]] = candidate

    # 方式 × attempt ごとに 1 Run。同じ枠に複数あれば最新を採用する
    cells: dict[tuple[str, int], dict] = {}
    superseded, ignored = [], []
    for run in sorted(latest.values(), key=lambda r: r["created_at"]):
        m = run["manifest"]
        if m.get("variant") not in variants:
            ignored.append(m["run_id"])
            continue
        key = (m["variant"], int(m.get("attempt") or 1))
        if key[1] > attempts:
            ignored.append(m["run_id"])
            continue
        if key in cells:
            superseded.append(cells[key]["manifest"]["run_id"])
        cells[key] = run
    if ignored:
        notes.append(f"比較対象外の方式または attempt の Run を無視: {ignored}")

    expected = [(v, a) for v in variants for a in range(1, attempts + 1)]
    missing = [{"variant": v, "attempt": a} for v, a in expected if (v, a) not in cells]
    chosen = [cells[k] for k in expected if k in cells]
    first_seen = min((r["created_at"] for r in chosen), default=None)

    if not chosen:
        status = "no_runs"
    elif not missing:
        status = "complete"
    elif now - first_seen >= timedelta(hours=timeout_hours):
        status = "timeout"
    else:
        status = "waiting"
    if status in READY_STATUSES and len({r["manifest"]["variant"] for r in chosen}) < 2:
        status = "insufficient"

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_rows = [r["manifest"] for r in chosen]
    dataset_rows = [r["dataset"] for r in chosen if r["dataset"] is not None]
    write_jsonl(out_dir / "manifest.jsonl", manifest_rows)
    write_jsonl(out_dir / "runs.jsonl", dataset_rows)
    results_dirs = sorted({str(p.parent) for r in chosen for p in (r["dir"] / "results").glob("*/trial*.jsonl")})
    (out_dir / "results_dirs.txt").write_text("".join(f"{d}\n" for d in results_dirs), encoding="utf-8")

    run_ids = sorted(m["run_id"] for m in manifest_rows)
    fingerprint = hashlib.sha256(json.dumps([task_id, run_ids]).encode()).hexdigest()[:16]
    pairwise = False
    if status in READY_STATUSES:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from run_pairwise import build_pairs  # 組の作り方を run_pairwise.py と一致させる
        pairwise = bool(build_pairs({r["run_id"]: r for r in dataset_rows}, manifest_rows))

    collect = {
        "task_id": task_id,
        "status": status,
        "variants": variants,
        "attempts": attempts,
        "present": [{"variant": r["manifest"]["variant"], "attempt": int(r["manifest"].get("attempt") or 1),
                     "run_id": r["manifest"]["run_id"], "hard_gate": r["manifest"].get("hard_gate", "pass"),
                     "judged": r["dataset"] is not None, "artifact": r["artifact"]} for r in chosen],
        "missing": missing,
        "superseded": superseded,
        "first_seen": first_seen.isoformat() if first_seen else None,
        "fingerprint": fingerprint,
        "marker": f"{COMPARE_PREFIX}{task_id}-{fingerprint}",
        "pairwise": pairwise,
        "absolute_results": len(results_dirs),
        "notes": notes,
    }
    (out_dir / "collect.json").write_text(json.dumps(collect, ensure_ascii=False, indent=2), encoding="utf-8")
    return collect


def cmd_merge(args) -> None:
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    if len(variants) < 2:
        raise SystemExit("--variants には比較する方式を 2 つ以上指定してください")
    now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
    c = merge(args.task_id, variants, args.attempts, args.timeout_hours, args.artifacts, args.out, now)
    print(f"{c['task_id']}: {c['status']}（揃った Run {len(c['present'])} / 未提出 {len(c['missing'])}）")
    for note in c["notes"]:
        print(f"  - {note}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"status={c['status']}\nready={str(c['status'] in READY_STATUSES).lower()}\n"
                    f"marker={c['marker']}\npairwise={str(c['pairwise']).lower()}\n")


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    repo = os.environ.get("GITHUB_REPOSITORY")

    t = sub.add_parser("tasks")
    t.add_argument("--repo", default=repo, required=repo is None)
    t.add_argument("--lookback-days", type=float, default=14)
    t.set_defaults(func=cmd_tasks)

    d = sub.add_parser("download")
    d.add_argument("--repo", default=repo, required=repo is None)
    d.add_argument("--task-id", required=True)
    d.add_argument("--dest", type=Path, required=True)
    d.set_defaults(func=cmd_download)

    m = sub.add_parser("merge")
    m.add_argument("--task-id", required=True)
    m.add_argument("--variants", required=True, help="比較する方式（カンマ区切り）")
    m.add_argument("--attempts", type=int, default=1, help="方式ごとの生成回数")
    m.add_argument("--timeout-hours", type=float, default=24, help="最初の Run からこの時間を過ぎたら揃った分だけで比較する")
    m.add_argument("--artifacts", type=Path, required=True, help="download の出力先")
    m.add_argument("--out", type=Path, required=True)
    m.add_argument("--now", help="現在時刻（ISO 8601。テスト用）")
    m.set_defaults(func=cmd_merge)

    args = p.parse_args()
    if getattr(args, "task_id", None) is not None and not TASK_ID_RE.match(args.task_id):
        raise SystemExit(f"task_id には英数字・_・. だけを使ってください: {args.task_id!r}")
    args.func(args)


if __name__ == "__main__":
    main()
