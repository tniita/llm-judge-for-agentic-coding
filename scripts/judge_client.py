"""LLM Judge の評価サービス（Foundry Hosted Agent）を呼ぶクライアント。CI や手元から使う。

接続先（どちらか）:
  --agent llm-judge-service [--project-endpoint https://<account>.services.ai.azure.com/api/projects/<project>]
      Foundry の Hosted Agent。Entra ID のトークン（スコープ https://ai.azure.com/.default）を
      az login / CI の azure/login（OIDC）の資格情報で取得する。プロジェクトは AZURE_AI_PROJECT_ENDPOINT でも指定できる
  --base-url http://localhost:8088
      ローカルで動かしたサービス（認証なし）
  環境変数 LLM_JUDGE_SERVICE_AGENT / LLM_JUDGE_SERVICE_URL でも指定できる

使い方:
  # 実験を作る（評価条件はこの時点で固定される。同じ定義なら既存を返す）
  python scripts/judge_client.py create-experiment --experiment-id exp-2026-11 --project shoplib \
    --variants fixed,hydrafusion --attempts 1 --timeout-hours 24
  # make_row.py の出力（評価データと対応表）を Run として登録し、採点の完了を待つ
  python scripts/judge_client.py submit --experiment-id exp-2026-11 --dataset out/runs.jsonl --manifest out/manifest.jsonl --wait
  # 任意の操作（compare_task / sweep / ingest_reviews / rejudge_experiment / report / get_experiment / review_packets）
  python scripts/judge_client.py call report --experiment-id exp-2026-11
  python scripts/judge_client.py call rejudge_experiment --experiment-id exp-2026-12 --param from_experiment_id=exp-2026-11 --wait
  # 受け付けた操作の状態
  python scripts/judge_client.py status <invocation_id> [--wait]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TERMINAL = {"completed", "failed", "cancelled"}


class Client:
    def __init__(self, *, base_url: str | None = None, agent: str | None = None,
                 project_endpoint: str | None = None, session_id: str | None = None):
        if base_url:
            self.root, self.query, self.token = base_url.rstrip("/") + "/invocations", {}, None
        elif agent:
            if not project_endpoint:
                raise SystemExit("--project-endpoint か AZURE_AI_PROJECT_ENDPOINT が必要です")
            self.root = f"{project_endpoint.rstrip('/')}/agents/{agent}/endpoint/protocols/invocations"
            self.query, self.token = {"api-version": "v1"}, self._token()
        else:
            raise SystemExit("--agent（Foundry の Hosted Agent）か --base-url（ローカル）を指定してください")
        if session_id:
            self.query["agent_session_id"] = session_id

    @staticmethod
    def _token() -> str:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from azure_auth import credential
        return credential().get_token("https://ai.azure.com/.default").token

    def _url(self, suffix: str = "") -> str:
        return self.root + suffix + (f"?{urllib.parse.urlencode(self.query)}" if self.query else "")

    def _send(self, method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, method=method, headers=headers,
                                     data=None if body is None else json.dumps(body, ensure_ascii=False).encode())
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                return res.status, json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"error": "http_error", "message": raw.decode(errors="replace")[:2000]}
            return exc.code, payload

    def invoke(self, body: dict) -> tuple[int, dict]:
        return self._send("POST", self._url(), body)

    def status(self, invocation_id: str) -> tuple[int, dict]:
        return self._send("GET", self._url(f"/{urllib.parse.quote(invocation_id)}"))

    def wait(self, invocation_id: str, *, timeout: float, interval: float = 15) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            code, job = self.status(invocation_id)
            if code != 200:
                raise SystemExit(f"状態を取得できません（{code}）: {job}")
            if job.get("status") in TERMINAL:
                return job
            if time.monotonic() > deadline:
                raise SystemExit(f"{timeout:g} 秒以内に終わりませんでした: {job}")
            time.sleep(interval)


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def emit(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def check(code: int, body: dict) -> dict:
    if code >= 400:
        emit(body)
        raise SystemExit(f"評価サービスがエラーを返しました（{code}）: {body.get('message', body)}")
    return body


def finish(client: Client, body: dict, args) -> dict:
    if args.wait and body.get("invocation_id") and body.get("status") not in TERMINAL:
        body = client.wait(body["invocation_id"], timeout=args.timeout, interval=args.interval)
        if body.get("status") != "completed":
            emit(body)
            raise SystemExit(f"{body.get('op')} が {body.get('status')} で終わりました: {body.get('error')}")
    return body


def parse_params(values: list[str]) -> dict:
    params = {}
    for value in values:
        key, sep, raw = value.partition("=")
        if not sep:
            raise SystemExit(f"--param は key=value の形で指定してください: {value!r}")
        try:
            params[key] = json.loads(raw)
        except json.JSONDecodeError:
            params[key] = raw
    return params


def cmd_create(client: Client, args) -> None:
    body = {"op": "create_experiment", "experiment_id": args.experiment_id, "project": args.project,
            "variants": [v.strip() for v in args.variants.split(",") if v.strip()],
            "attempts": args.attempts, "timeout_hours": args.timeout_hours, "gap": args.gap, "spread": args.spread,
            "judge_repeats": args.judge_repeats, "pairwise_repeats": args.pairwise_repeats,
            "description": args.description, "base_experiment_id": args.base_experiment_id}
    emit(check(*client.invoke({k: v for k, v in body.items() if v is not None})))


def cmd_submit(client: Client, args) -> None:
    rows = {r["run_id"]: r for r in load_jsonl(args.dataset)} if args.dataset and args.dataset.is_file() else {}
    results = []
    for manifest in load_jsonl(args.manifest):
        row = rows.get(manifest["run_id"]) if manifest.get("hard_gate", "pass") == "pass" else None
        body = check(*client.invoke({"op": "submit_run", "experiment_id": args.experiment_id,
                                     "manifest": manifest, "row": row}))
        results.append(finish(client, body, args))
    emit(results)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        # 方式名は書かない（Actions を見たレビュアーに方式が分からないようにする）
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### llm-judge service: {args.experiment_id}\n\n| run_id | 状態 | 総合点 | 人間レビュー |\n|---|---|---|---|\n")
            for r in results:
                res = r.get("result") or r
                total = res.get("total")
                f.write(f"| {res.get('run_id', '-')} | {res.get('status') or res.get('state')} | "
                        f"{'-' if total is None else f'{total:.1f}'} | {len(res.get('human_review') or [])} 件 |\n")


def cmd_call(client: Client, args) -> None:
    body = {"op": args.op, **parse_params(args.param)}
    if args.experiment_id:
        body["experiment_id"] = args.experiment_id
    emit(finish(client, check(*client.invoke(body)), args))


def cmd_status(client: Client, args) -> None:
    body = check(*client.status(args.invocation_id))
    emit(finish(client, {**body, "invocation_id": args.invocation_id}, args))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default=os.environ.get("LLM_JUDGE_SERVICE_URL"))
    p.add_argument("--agent", default=os.environ.get("LLM_JUDGE_SERVICE_AGENT"))
    p.add_argument("--project-endpoint", default=os.environ.get("AZURE_AI_PROJECT_ENDPOINT"))
    p.add_argument("--session-id", default=os.environ.get("LLM_JUDGE_SERVICE_SESSION"),
                   help="agent_session_id（同じ値を使うと同じコンピューティングに届く）")
    wait = argparse.ArgumentParser(add_help=False)
    wait.add_argument("--wait", action="store_true", help="202 で受け付けた操作の完了を待つ")
    wait.add_argument("--timeout", type=float, default=3600)
    wait.add_argument("--interval", type=float, default=15)
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("create-experiment")
    c.add_argument("--experiment-id", required=True)
    c.add_argument("--project", required=True)
    c.add_argument("--variants", required=True, help="比較する方式（カンマ区切り）")
    c.add_argument("--attempts", type=int)
    c.add_argument("--timeout-hours", type=float)
    c.add_argument("--gap", type=float)
    c.add_argument("--spread", type=float)
    c.add_argument("--judge-repeats", type=int)
    c.add_argument("--pairwise-repeats", type=int)
    c.add_argument("--description")
    c.add_argument("--base-experiment-id")
    c.set_defaults(func=cmd_create)

    s = sub.add_parser("submit", parents=[wait])
    s.add_argument("--experiment-id", required=True)
    s.add_argument("--dataset", type=Path, help="make_row.py の評価データ（Hard Gate 不合格だけなら不要）")
    s.add_argument("--manifest", type=Path, required=True, help="make_row.py の対応表")
    s.set_defaults(func=cmd_submit)

    k = sub.add_parser("call", parents=[wait])
    k.add_argument("op")
    k.add_argument("--experiment-id")
    k.add_argument("--param", action="append", default=[], help="key=value（値は JSON として解釈できればそうする）")
    k.set_defaults(func=cmd_call)

    t = sub.add_parser("status", parents=[wait])
    t.add_argument("invocation_id")
    t.set_defaults(func=cmd_status)

    args = p.parse_args()
    client = Client(base_url=args.base_url, agent=None if args.base_url else args.agent,
                    project_endpoint=args.project_endpoint, session_id=args.session_id)
    args.func(client, args)


if __name__ == "__main__":
    main()
