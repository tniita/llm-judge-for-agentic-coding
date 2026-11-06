"""検証用リポジトリ（shoplib）の 10 タスクを、2 つの方式のコーディングエージェントに解かせて評価データを作る。

方式（--agent で選ぶ）:
  claude（既定。HydraFusion の実行環境が無いため、Foundry 上の Claude で代用）
    sonnet-single  : Claude Code + claude-sonnet-5-5
    opus-single    : Claude Code + claude-opus-5-5
  copilot（GitHub Copilot CLI）
    opus-5.5       : Copilot CLI + claude-opus-5.5
    copilot-fusion : Copilot CLI + Copilot Fusion（--model fusion。実際に使われたモデルは agent.json に記録）

Run ごとに次を行う:
  1. shoplib_base を作業ディレクトリに複製して git 管理する
  2. エージェント（非対話モード）にタスクを渡して実装させる。使えるのはファイル操作と pytest / ruff の実行だけ
  3. 既存テスト・Hidden test・ruff を実行して Hard Gate を判定し、CI 結果の要約を作る
  4. --metrics なら scripts/metrics.py で共通祖先との指標の差分を計算する
  5. scripts/make_row.py で評価データと対応表に追記する（--skills の Skill はエージェントと Judge の両方に渡す）

使い方:
  python experiments/run_agents.py --attempts 1 --workers 4
  python experiments/run_agents.py --agent copilot --out-dir experiments/data_copilot --metrics --skills project_skills
"""

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

EXP = Path(__file__).resolve().parent
ROOT = EXP.parent
sys.path.insert(0, str(ROOT / "scripts"))
from make_row import read_skills

VENV_BIN = ROOT / ".venv" / "bin"
PY = str(VENV_BIN / "python")
RUFF = str(VENV_BIN / "ruff")
WORK = EXP / "work"
AGENTS = {
    "claude": {"sonnet-single": "sonnet", "opus-single": "opus"},
    "copilot": {"opus-5.5": "claude-opus-5.5", "copilot-fusion": "fusion"},
}
AGENT_TIMEOUT_SEC = 1500
USD_PER_AI_CREDIT = 0.01  # GitHub Copilot の AI クレジット単価

PROMPT = """あなたはこのリポジトリで作業するソフトウェアエンジニアです。次のタスクを実装してください。

{task}

# 作業ルール
- テストの実行: `{pytest}`
- Lint の実行: `{ruff}`
- 要件を検証するテストを tests/ に追加すること
- 作業の最後に、PR 説明（何をどう変えたか、確認方法）を PR_DESCRIPTION.md に書くこと
{skills}"""

write_lock = threading.Lock()


def sh(args: list[str], cwd: Path, timeout: int = 300, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)


def build_prompt(task_text: str, agent: str, skills: str) -> str:
    # Copilot CLI の許可パターンはコマンド名の前方一致なので、venv を PATH に入れて短い名前で実行させる
    pytest_cmd, ruff_cmd = ("pytest -q", "ruff check .") if agent == "copilot" else (f"{PY} -m pytest -q", f"{RUFF} check .")
    skills_text = f"\n# プロジェクトの Skill（守るべきルール）\n\n{skills}\n" if skills else ""
    return PROMPT.format(task=task_text, pytest=pytest_cmd, ruff=ruff_cmd, skills=skills_text)


def run_claude(prompt: str, model: str, repo: Path) -> dict:
    cmd = [
        "claude", "-p", prompt,
        "--model", model,
        "--bare", "--settings", str(Path.home() / ".claude" / "settings.json"), "--strict-mcp-config",
        "--permission-mode", "acceptEdits",
        "--tools", "Read", "Edit", "Write", "Glob", "Grep", "Bash",
        "--allowedTools", f"Bash({PY} -m pytest:*)", f"Bash({RUFF} check:*)",
        "--output-format", "json", "--no-session-persistence",
    ]
    try:
        agent = sh(cmd, repo, timeout=AGENT_TIMEOUT_SEC)
        out = json.loads(agent.stdout) if agent.stdout.strip().startswith("{") else {"raw": agent.stdout[-2000:]}
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    usage = out.get("usage", {})
    if "total_cost_usd" in out:
        out["_usage"] = {
            "cost_usd": out["total_cost_usd"], "duration_sec": out["duration_ms"] / 1000,
            "tokens_in": sum(usage.get(k, 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")),
            "tokens_out": usage.get("output_tokens", 0),
        }
    out["_denials"] = len(out.get("permission_denials", []) or [])
    return out


def run_copilot(prompt: str, model: str, repo: Path, workdir: Path) -> dict:
    usage_file = workdir / "usage.json"
    cmd = [
        "copilot", "-p", prompt,
        "--model", model,
        "--no-ask-user", "--no-custom-instructions", "--disable-builtin-mcps", "--no-auto-update",
        "--available-tools", "view", "edit", "create", "apply_patch", "glob", "grep", "bash",
        "--allow-tool", "write", "--allow-tool", "shell(pytest)", "--allow-tool", "shell(ruff)",
        "--output-format", "json", "--usage-output-file", str(usage_file),
    ]
    env = {**os.environ, "PATH": f"{VENV_BIN}{os.pathsep}{os.environ.get('PATH', '')}", "VIRTUAL_ENV": str(ROOT / ".venv")}
    started = time.monotonic()
    try:
        agent = sh(cmd, repo, timeout=AGENT_TIMEOUT_SEC, env=env)
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    elapsed = time.monotonic() - started
    (workdir / "agent_events.jsonl").write_text(agent.stdout, encoding="utf-8")
    denials, turns = 0, 0
    for line in agent.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        data = event.get("data") or {}
        if event.get("type") == "assistant.turn_start":
            turns += 1
        if event.get("type") == "tool.execution_complete" and "Permission denied" in str(data.get("error", "")):
            denials += 1
    out = {"exit_code": agent.returncode, "num_turns": turns, "_denials": denials, "stderr": agent.stderr[-2000:]}
    if not usage_file.exists():
        out["error"] = f"usage なし（exit {agent.returncode}）"
        return out
    usage = json.loads(usage_file.read_text(encoding="utf-8"))
    out["models_used"] = sorted(usage.get("modelMetrics", {}))
    out["premium_requests"] = usage.get("totalPremiumRequestCost")
    out["ai_credits"] = usage.get("totalNanoAiu", 0) / 1e9
    metrics = usage.get("modelMetrics", {}).values()
    out["_usage"] = {
        "cost_usd": out["ai_credits"] * USD_PER_AI_CREDIT, "duration_sec": elapsed,
        "tokens_in": sum(m["usage"].get("inputTokens", 0) for m in metrics),
        "tokens_out": sum(m["usage"].get("outputTokens", 0) for m in metrics),
    }
    return out


def pytest_summary(result: subprocess.CompletedProcess) -> str:
    lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
    return lines[-1] if lines else "(出力なし)"


def run_one(task_id: str, variant: str, attempt: int, opts: argparse.Namespace) -> dict:
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
    prompt = build_prompt(task_text, opts.agent, opts.skills_text)
    model = AGENTS[opts.agent][variant]
    if opts.agent == "copilot":
        agent_out = run_copilot(prompt, model, repo, workdir)
    else:
        agent_out = run_claude(prompt, model, repo)
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

    args = [
        PY, str(ROOT / "scripts" / "make_row.py"),
        "--run-id", run_id, "--task-id", task_id, "--variant", variant, "--attempt", str(attempt),
        "--hard-gate", hard_gate, "--task", str(EXP / "tasks" / f"{task_id}.md"),
        "--repo", str(repo), "--base", base_sha, "--head", "HEAD",
        "--ci", str(workdir / "ci.txt"), "--pr", str(workdir / "pr.md"),
        "--dataset", str(opts.out_dir / "runs.jsonl"), "--manifest", str(opts.out_dir / "manifest.jsonl"),
    ]
    if opts.metrics:
        metrics = sh([PY, str(ROOT / "scripts" / "metrics.py"), "--repo", str(repo), "--base", base_sha, "--head", "HEAD",
                      "--cache-dir", str(WORK / ".metrics-cache"), "--out", str(workdir / "metrics.json")], ROOT)
        if metrics.returncode == 0:
            args += ["--metrics", str(workdir / "metrics.json")]
        else:
            args += ["--metrics-error", (metrics.stderr.strip().splitlines() or ["metrics.py が失敗"])[-1]]
    if opts.skills:
        args += ["--skills", str(opts.skills)]
    usage = agent_out.get("_usage")
    if usage:
        args += ["--cost-usd", str(usage["cost_usd"]), "--duration-sec", str(usage["duration_sec"]),
                 "--tokens-in", str(usage["tokens_in"]), "--tokens-out", str(usage["tokens_out"])]
    models_used = agent_out.get("models_used") or sorted(agent_out.get("modelUsage") or {})
    if models_used:
        args += ["--agent-models", ",".join(models_used)]
    with write_lock:
        made = sh(args, ROOT)
    summary = {
        "run_id": run_id, "task_id": task_id, "variant": variant, "attempt": attempt, "hard_gate": hard_gate,
        "visible": pytest_summary(visible), "hidden": pytest_summary(hidden), "lint": lint_issues,
        "cost_usd": (usage or {}).get("cost_usd"), "turns": agent_out.get("num_turns"),
        "denials": agent_out.get("_denials", 0), "agent_error": agent_out.get("error"),
        "models_used": models_used,
        "make_row": (made.stdout + made.stderr).strip().splitlines()[:1],
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--tasks", nargs="*", help="例: T01 T02（省略時は全タスク）")
    parser.add_argument("--agent", choices=sorted(AGENTS), default="claude", help="エージェントの実行基盤")
    parser.add_argument("--out-dir", type=Path, default=EXP / "data", help="runs.jsonl / manifest.jsonl の出力先")
    parser.add_argument("--metrics", action="store_true", help="metrics.py で指標の差分を計算して評価データに含める")
    parser.add_argument("--skills", type=Path, help="プロジェクトの Skill。エージェントと Judge の両方に渡す")
    args = parser.parse_args()
    args.skills_text = read_skills(args.skills, 40_000) if args.skills else ""

    args.out_dir.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(exist_ok=True)
    manifest = args.out_dir / "manifest.jsonl"
    done = set()
    if manifest.exists():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            m = json.loads(line)
            done.add((m["task_id"], m["variant"], m["attempt"]))

    tasks = args.tasks or sorted(p.stem for p in (EXP / "tasks").glob("T*.md"))
    variants = AGENTS[args.agent]
    jobs = [(t, v, a) for t in tasks for v in variants for a in range(1, args.attempts + 1) if (t, v, a) not in done]
    print(f"{len(jobs)} 件を実行します（実行済み {len(done)} 件はスキップ）", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda j: run_one(*j, args), jobs))
    (args.out_dir / f"agent_runs_{secrets.token_hex(3)}.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    failed = [r for r in results if r["hard_gate"] == "fail"]
    print(f"完了: {len(results)} 件 / Hard Gate 不合格 {len(failed)} 件", file=sys.stderr)


if __name__ == "__main__":
    main()
