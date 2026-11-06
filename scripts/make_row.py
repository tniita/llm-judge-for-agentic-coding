"""1 Run 分の評価データ（datasets/runs.jsonl）と対応表（datasets/manifest.jsonl）を作る。

- Hard Gate に落ちた Run は対応表にだけ記録する（合格率の集計に使う。Judge には回さない）
- モデル名などの手がかりになる文字列を伏せ、Co-authored-by などの行を取り除く
- ロックファイルなどの自動生成物は差分から外し、長すぎる差分はファイル単位で省略する

使い方（差分ファイルから）:
  python scripts/make_row.py --run-id run-003 --task-id T002 --variant fixed --attempt 1 \
    --hard-gate pass --task task.md --diff pr.diff --ci ci.txt --pr pr.md \
    --tokens-in 120000 --tokens-out 8000 --cost-usd 1.2 --duration-sec 340

使い方（リポジトリから。周辺コードも見せるため -U30 で差分を取る）:
  python scripts/make_row.py ... --repo path/to/repo --base main --head agent-branch

機械的な指標とプロジェクトの Skill も渡す場合:
  python scripts/metrics.py --repo path/to/repo --base main --head agent-branch --out metrics.json
  python scripts/make_row.py ... --metrics metrics.json --skills project_skills/

実際に使ったモデルを記録する場合（対応表にだけ残し、Judge と同系統かの確認に使う。評価データでは伏せる）:
  python scripts/make_row.py ... --agent-models claude-opus-5.5,gpt-6-luna
"""

import argparse
import fnmatch
import json
import re
import subprocess
from pathlib import Path

from config import METRIC_THRESHOLDS, ROOT
from metrics import DEFAULT_EXCLUDES, comparison_refs, format_delta

DEFAULT_TERMS = [
    "claude", "anthropic", "openai", "chatgpt", r"gpt-?\d[\w.\-]*", "copilot", "gemini",
    "sonnet", "opus", "haiku", "codex", "llama", "mistral", "hydra ?fusion",
]
TRAILER_RE = re.compile(
    r"^[+\- ]?\s*(?:#|//|\*|--)?\s*(co-authored-by|generated[- ]by|generated with|signed-off-by)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
EXCLUDE_RE = re.compile(
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|uv\.lock|Cargo\.lock|go\.sum"
    r"|composer\.lock|Gemfile\.lock)$|\.min\.(js|css)$|\.map$|(^|/)(dist|build|vendor|node_modules)/"
)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->\s*", re.DOTALL)
FILE_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)


def read(path: Path | None) -> str:
    return path.read_text(encoding="utf-8") if path else ""


def git_diff(repo: Path, base: str, head: str, context_lines: int) -> str:
    base_sha, head_sha = comparison_refs(repo, base, head)
    return subprocess.run(
        ["git", "-C", str(repo), "diff", f"-U{context_lines}", base_sha, head_sha],
        check=True, capture_output=True, text=True,
    ).stdout


def diff_python_files(diff: str, excludes: list[str]) -> set[str]:
    """差分ファイルに現れる .py のうち、metrics.py の計測対象になるもの（rename は旧・新の両方）。"""
    paths = {p for m in FILE_HEADER_RE.finditer(diff) for p in m.groups()}
    return {p for p in paths if p.endswith(".py") and not any(fnmatch.fnmatch(p, x) for x in excludes)}


def split_files(diff: str) -> list[tuple[str, str]]:
    starts = [m.start() for m in FILE_HEADER_RE.finditer(diff)]
    if not starts:
        return [("(unknown)", diff)]
    blocks = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(diff)
        block = diff[start:end]
        blocks.append((FILE_HEADER_RE.match(block).group(2), block))
    return blocks


def shape_diff(diff: str, max_chars: int) -> tuple[str, list[str], list[str]]:
    kept, excluded, omitted, size = [], [], [], 0
    for path, block in split_files(diff):
        if EXCLUDE_RE.search(path):
            excluded.append(path)
        elif size + len(block) > max_chars:
            omitted.append(path)
        else:
            kept.append(block)
            size += len(block)
    text = "".join(kept)
    if excluded:
        text += f"\n（自動生成物のため差分を省略したファイル: {', '.join(excluded)}）\n"
    if omitted:
        text += f"\n（差分が長いため省略したファイル: {', '.join(omitted)}。これらのファイルについては判断を保留すること）\n"
    return text, excluded, omitted


def read_skills(path: Path | None, max_chars: int) -> str:
    """ディレクトリなら中の Markdown をファイル名順に連結する。"""
    if not path:
        return ""
    files = sorted(path.rglob("*.md")) if path.is_dir() else [path]
    text = "\n\n".join(f"## {f.relative_to(path) if path.is_dir() else f.name}\n\n{HTML_COMMENT_RE.sub('', f.read_text(encoding='utf-8')).strip()}"
                       for f in files if f.name.lower() != "readme.md")
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n（Skill が長いため {max_chars:,} 文字以降を省略。省略部分のルールについては判断を保留すること）"
    return text


def redact(text: str, pattern: re.Pattern) -> tuple[str, int]:
    text, trailers = TRAILER_RE.subn("", text)
    text, terms = pattern.subn("[REDACTED]", text)
    return text, trailers + terms


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", required=True)
    p.add_argument("--task-id", required=True)
    p.add_argument("--variant", required=True)
    p.add_argument("--attempt", type=int, default=1)
    p.add_argument("--hard-gate", choices=["pass", "fail"], default="pass")
    p.add_argument("--task", type=Path, help="タスクの要件と完了条件（R1, R2 ... の ID 付き推奨）")
    p.add_argument("--diff", type=Path)
    p.add_argument("--repo", type=Path)
    p.add_argument("--base")
    p.add_argument("--head")
    p.add_argument("--context-lines", type=int, default=30)
    p.add_argument("--context", type=Path, help="差分に含まれない関連コード（呼び出し元など）")
    p.add_argument("--ci", type=Path)
    p.add_argument("--pr", type=Path)
    p.add_argument("--tokens-in", type=float)
    p.add_argument("--tokens-out", type=float)
    p.add_argument("--cost-usd", type=float)
    p.add_argument("--duration-sec", type=float)
    p.add_argument("--agent-models", default="",
                   help="エージェントが実際に使ったモデル（カンマ区切り）。対応表にだけ記録し、Judge と同系統かの確認に使う")
    p.add_argument("--metrics", type=Path, help="metrics.py の出力（metrics.json）。Judge には差分（Δ）だけを渡す")
    p.add_argument("--metrics-error", help="metrics.py が失敗したときの理由。指標は計測不能として扱い、人間レビューに回す")
    p.add_argument("--skills", type=Path, help="プロジェクトの Skill（Markdown のディレクトリまたはファイル）")
    p.add_argument("--max-skills-chars", type=int, default=40_000)
    p.add_argument("--max-diff-chars", type=int, default=150_000)
    p.add_argument("--redact-terms", default="", help="追加で伏せる語（カンマ区切り、正規表現可）")
    p.add_argument("--no-redact-diff", action="store_true", help="差分のコードは伏せない（SDK 名などを含むコード向け）")
    p.add_argument("--dataset", type=Path, default=ROOT / "datasets" / "runs.jsonl")
    p.add_argument("--manifest", type=Path, default=ROOT / "datasets" / "manifest.jsonl")
    args = p.parse_args()

    manifest_row = {
        "run_id": args.run_id,
        "task_id": args.task_id,
        "variant": args.variant,
        "attempt": args.attempt,
        "hard_gate": args.hard_gate,
    }
    for key in ("tokens_in", "tokens_out", "cost_usd", "duration_sec"):
        if getattr(args, key) is not None:
            manifest_row[key] = getattr(args, key)
    agent_models = [m.strip() for m in args.agent_models.split(",") if m.strip()]
    if agent_models:
        manifest_row["agent_models"] = agent_models
    if args.metrics and args.metrics_error:
        raise SystemExit("--metrics と --metrics-error は同時に指定できません")
    metrics = json.loads(args.metrics.read_text(encoding="utf-8")) if args.metrics else None
    if args.metrics_error:
        metrics = {"status": "unavailable", "parse_errors": {"compute": args.metrics_error},
                   "delta": {k: None for k in METRIC_THRESHOLDS}}
    if metrics:
        if args.metrics and args.repo and args.base and args.head:
            base_sha, head_sha = comparison_refs(args.repo, args.base, args.head)
            if (metrics.get("base"), metrics.get("head")) != (base_sha, head_sha):
                raise SystemExit("指標と PR 差分の比較 SHA が一致しません。metrics.py を同じ base/head で再実行してください")
            args.base, args.head = base_sha, head_sha
        unavailable = metrics.get("status") == "unavailable" or bool(metrics.get("parse_errors"))
        manifest_row["metrics_status"] = "unavailable" if unavailable else "completed"
        manifest_row["metrics_errors"] = metrics.get("parse_errors", {})
        # 人間レビューの振り分けと方式ごとの集計に使う
        manifest_row["metrics_delta"] = {k: None if unavailable else v for k, v in metrics["delta"].items()
                                         if k in METRIC_THRESHOLDS or k in ("duplication_rate", "mean_instability")}

    if args.hard_gate == "fail":
        append_jsonl(args.manifest, manifest_row)
        print(f"{args.run_id}: Hard Gate 不合格のため対応表にのみ記録しました")
        return

    if not args.task or not args.ci or not (args.diff or (args.repo and args.base and args.head)):
        raise SystemExit("合格 Run には --task、--ci、および --diff か --repo/--base/--head が必要です")

    diff = read(args.diff) if args.diff else git_diff(args.repo, args.base, args.head, args.context_lines)
    if args.diff and args.metrics and "changed_files" in metrics:
        # 差分ファイルだけでは SHA を確かめられないため、変更された Python ファイルの一致で代わりに確かめる
        expected = set(metrics["changed_files"])
        actual = diff_python_files(diff, metrics.get("excludes", DEFAULT_EXCLUDES))
        if expected != actual:
            raise SystemExit("指標と差分ファイルの変更対象が一致しません（"
                             f"指標のみ: {sorted(expected - actual)}、差分のみ: {sorted(actual - expected)}）。"
                             "同じ base/head で metrics.py と差分を作り直してください")
    diff, excluded, omitted = shape_diff(diff, args.max_diff_chars)

    terms = (DEFAULT_TERMS + [re.escape(args.variant)] + [re.escape(m) for m in agent_models]
             + [t for t in args.redact_terms.split(",") if t])
    pattern = re.compile(r"\b(" + "|".join(terms) + r")\b", re.IGNORECASE)

    fields = {
        "query": read(args.task),
        "response": diff,
        "context": read(args.context) or "なし",
        "ci_summary": read(args.ci),
        "pr_description": read(args.pr) or "（PR 説明なし）",
        "metrics_delta": format_delta(metrics) if metrics else "なし",
        "project_skills": read_skills(args.skills, args.max_skills_chars) or "なし",
    }
    redactions = {}
    for key, value in fields.items():
        if key in ("query", "metrics_delta", "project_skills") or (key in ("response", "context") and args.no_redact_diff):
            continue
        fields[key], redactions[key] = redact(value, pattern)

    append_jsonl(args.dataset, {"run_id": args.run_id, **fields})
    append_jsonl(args.manifest, manifest_row)
    print(f"{args.run_id}: 追加しました（差分 {len(fields['response']):,} 文字）")
    if any(redactions.values()):
        print(f"  伏せた箇所: {redactions}。コードの意味が変わっていないか確認してください")
    if excluded:
        print(f"  自動生成物として除外: {excluded}")
    if omitted:
        print(f"  長さの上限で省略: {omitted}（--max-diff-chars で調整）")


if __name__ == "__main__":
    main()
