"""検証用リポジトリ（shoplib）の 10 タスクを、2 つの方式のコーディングエージェントに解かせて評価データを作る。

方式（HydraFusion の実行環境が無いため、Foundry 上の Claude で代用）:
  sonnet-single : Claude Code + claude-sonnet-5-5
  opus-single   : Claude Code + claude-opus-5-5

Run ごとに次を行う:
  1. shoplib_base を作業ディレクトリに複製して git 管理する
  2. Claude Code（-p モード）にタスクを渡して実装させる。使えるのはファイル操作と pytest / ruff の実行だけ
  3. 既存テスト・Hidden test・ruff を実行して Hard Gate を判定し、CI 結果の要約を作る
  4. scripts/make_row.py で評価データと対応表に追記する

使い方:
  python experiments/run_agents.py --attempts 1 --workers 4
"""

import argparse
import json
import re
import secrets
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

EXP = Path(__file__).resolve().parent
ROOT = EXP.parent
PY = str(ROOT / ".venv" / "bin" / "python")
RUFF = str(ROOT / ".venv" / "bin" / "ruff")
WORK = EXP / "work"
DATA = EXP / "data"
DATASET = DATA / "runs.jsonl"
MANIFEST = DATA / "manifest.jsonl"
VARIANTS = {"sonnet-single": "sonnet", "opus-single": "opus"}
AGENT_TIMEOUT_SEC = 1500

PROMPT = """あなたはこのリポジトリで作業するソフトウェアエンジニアです。次のタスクを実装してください。

{task}

# 作業ルール
- テストの実行: `{py} -m pytest -q`
- Lint の実行: `{ruff} check .`
- 要件を検証するテストを tests/ に追加すること
- 作業の最後に、PR 説明（何をどう変えたか、確認方法）を PR_DESCRIPTION.md に書くこと
"""

write_lock = threading.Lock()


def sh(args: list[str], cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def pytest_summary(result: subprocess.CompletedProcess) -> str:
    lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
    return lines[-1] if lines else "(出力なし)"


def run_one(task_id: str, variant: str, attempt: int) -> dict:
    run_id = f"r-{secrets.token_hex(4)}"
    workdir = WORK / run_id
    repo = workdir / "repo"
    shutil.copytree(EXP / "shoplib_base", repo)
    git = ["git", "-c", "user.name=bench", "-c", "user.email=bench@example.invalid"]
    sh(["git", "init", "-q"], repo)
    sh([*git, "add", "-A"], repo)
    sh([*git, "commit", "-q", "-m", "base"], repo)
    base_sha = sh(["git", "rev-parse", "HEAD"], repo).stdout.strip()

    task_text = (EXP / "tasks" / f"{task_id}.md").read_text(encoding="utf-8")
    cmd = [
        "claude", "-p", PROMPT.format(task=task_text, py=PY, ruff=RUFF),
        "--model", VARIANTS[variant],
        "--bare", "--settings", str(Path.home() / ".claude" / "settings.json"), "--strict-mcp-config",
        "--permission-mode", "acceptEdits",
        "--tools", "Read", "Edit", "Write", "Glob", "Grep", "Bash",
        "--allowedTools", f"Bash({PY} -m pytest:*)", f"Bash({RUFF} check:*)",
        "--output-format", "json", "--no-session-persistence",
    ]
    try:
        agent = sh(cmd, repo, timeout=AGENT_TIMEOUT_SEC)
        agent_out = json.loads(agent.stdout) if agent.stdout.strip().startswith("{") else {"raw": agent.stdout[-2000:]}
    except subprocess.TimeoutExpired:
        agent_out = {"error": "timeout"}
    (workdir / "agent.json").write_text(json.dumps(agent_out, ensure_ascii=False, indent=2), encoding="utf-8")

    pr_file = repo / "PR_DESCRIPTION.md"
    pr_text = pr_file.read_text(encoding="utf-8") if pr_file.exists() else ""
    pr_file.unlink(missing_ok=True)
    (workdir / "pr.md").write_text(pr_text, encoding="utf-8")

    visible = sh([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"], repo)
    hidden_dir = repo / "_hidden"
    hidden_dir.mkdir(exist_ok=True)
    shutil.copy(EXP / "hidden" / f"test_{task_id}.py", hidden_dir)
    hidden = sh([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir=.", str(hidden_dir)], repo)
    shutil.rmtree(hidden_dir)
    lint = sh([RUFF, "check", "--output-format", "concise", "."], repo)
    lint_issues = len([l for l in lint.stdout.splitlines() if re.match(r"^\S+:\d+:\d+:", l)])

    hard_gate = "pass" if visible.returncode == 0 and hidden.returncode == 0 else "fail"
    ci_text = (
        f"Build: success\n"
        f"Tests (repository): {pytest_summary(visible)}\n"
        f"Hidden tests: {'passed' if hidden.returncode == 0 else 'failed'}\n"
        f"Lint (ruff): {lint_issues} issues\n"
        f"CodeQL: not run\n"
    )
    if lint_issues:
        ci_text += "Lint details:\n" + "\n".join(lint.stdout.splitlines()[:20]) + "\n"
    (workdir / "ci.txt").write_text(ci_text, encoding="utf-8")
    (workdir / "hidden_output.txt").write_text(hidden.stdout[-4000:], encoding="utf-8")

    sh([*git, "add", "-A"], repo)
    sh([*git, "commit", "-q", "--allow-empty", "-m", "agent"], repo)

    usage = agent_out.get("usage", {})
    tokens_in = sum(usage.get(k, 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    args = [
        PY, str(ROOT / "scripts" / "make_row.py"),
        "--run-id", run_id, "--task-id", task_id, "--variant", variant, "--attempt", str(attempt),
        "--hard-gate", hard_gate, "--task", str(EXP / "tasks" / f"{task_id}.md"),
        "--repo", str(repo), "--base", base_sha, "--head", "HEAD",
        "--ci", str(workdir / "ci.txt"), "--pr", str(workdir / "pr.md"),
        "--dataset", str(DATASET), "--manifest", str(MANIFEST),
    ]
    if "total_cost_usd" in agent_out:
        args += ["--cost-usd", str(agent_out["total_cost_usd"]), "--duration-sec", str(agent_out["duration_ms"] / 1000),
                 "--tokens-in", str(tokens_in), "--tokens-out", str(usage.get("output_tokens", 0))]
    with write_lock:
        made = sh(args, ROOT)
    summary = {
        "run_id": run_id, "task_id": task_id, "variant": variant, "attempt": attempt, "hard_gate": hard_gate,
        "visible": pytest_summary(visible), "hidden": pytest_summary(hidden), "lint": lint_issues,
        "cost_usd": agent_out.get("total_cost_usd"), "turns": agent_out.get("num_turns"),
        "denials": len(agent_out.get("permission_denials", []) or []), "agent_error": agent_out.get("error"),
        "make_row": (made.stdout + made.stderr).strip().splitlines()[:1],
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--tasks", nargs="*", help="例: T01 T02（省略時は全タスク）")
    args = parser.parse_args()

    DATA.mkdir(exist_ok=True)
    WORK.mkdir(exist_ok=True)
    done = set()
    if MANIFEST.exists():
        for line in MANIFEST.read_text(encoding="utf-8").splitlines():
            m = json.loads(line)
            done.add((m["task_id"], m["variant"], m["attempt"]))

    tasks = args.tasks or sorted(p.stem for p in (EXP / "tasks").glob("T*.md"))
    jobs = [(t, v, a) for t in tasks for v in VARIANTS for a in range(1, args.attempts + 1) if (t, v, a) not in done]
    print(f"{len(jobs)} 件を実行します（実行済み {len(done)} 件はスキップ）", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda j: run_one(*j), jobs))
    (DATA / f"agent_runs_{secrets.token_hex(3)}.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    failed = [r for r in results if r["hard_gate"] == "fail"]
    print(f"完了: {len(results)} 件 / Hard Gate 不合格 {len(failed)} 件", file=sys.stderr)


if __name__ == "__main__":
    main()
