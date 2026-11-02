"""サービスと外部（Foundry の評価・可視化基盤・レビュー用リポジトリ）の境目。テストでは差し替える。"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def kit_scripts_dir() -> Path:
    """キットの scripts/。デプロイ時は hosted/sync_kit.py が kit/ にコピーする。ローカルではリポジトリのものを使う。"""
    for candidate in (HERE / "kit" / "scripts", HERE.parents[2] / "scripts"):
        if (candidate / "config.py").is_file():
            return candidate
    raise RuntimeError("キットの scripts/ が見つかりません。python hosted/sync_kit.py を実行してください")


def load_kit() -> None:
    path = str(kit_scripts_dir())
    if path not in sys.path:
        sys.path.insert(0, path)


load_kit()

import config  # noqa: E402
import run_eval  # noqa: E402
import run_pairwise  # noqa: E402
from foundry_eval import current_condition  # noqa: E402


def read_secret(uri: str | None) -> str | None:
    """Key Vault のシークレット（https://<vault>.vault.azure.net/secrets/<name>[/<version>]）を読む。"""
    if not uri:
        return None
    parsed = urllib.parse.urlparse(uri)
    parts = parsed.path.strip("/").split("/")
    if parsed.scheme != "https" or len(parts) < 2 or parts[0] != "secrets":
        raise RuntimeError(f"Key Vault のシークレット URI が不正です: {uri}")
    from azure.keyvault.secrets import SecretClient

    from azure_auth import credential
    client = SecretClient(f"https://{parsed.netloc}", credential())
    return client.get_secret(parts[1], parts[2] if len(parts) > 2 else None).value


class FoundryJudge:
    """Foundry の評価器カタログに登録した評価器で採点する。"""

    def condition(self) -> dict:
        names = [*run_eval.evaluator_names(), config.PAIRWISE["name"]]
        return current_condition(names)

    def judge_rows(self, rows: list[dict], *, name: str, repeats: int) -> Path:
        return run_eval.judge_rows(rows, name=name, repeats=repeats)

    def judge_pairs(self, pairs: list[dict], dataset: dict[str, dict], *, name: str, repeats: int) -> Path:
        return run_pairwise.judge_pairs(pairs, dataset, name=name, repeats=repeats)


class Publisher:
    """publish_results.py と同じ形で可視化基盤に送る。LLM_JUDGE_PUBLISH=1 のときだけ送る。

    送信先（Cosmos DB / Application Insights）ごとに送り、失敗した送信先だけをあとで送り直せるようにする。
    """

    def __init__(self):
        self.enabled = os.environ.get("LLM_JUDGE_PUBLISH") == "1"
        self.cosmos = os.environ.get("LLM_JUDGE_COSMOS_ENDPOINT")
        # Hosted Agent には Foundry プロジェクトの APPLICATIONINSIGHTS_CONNECTION_STRING が自動で入る。
        # Workbook の Application Insights が別なら LLM_JUDGE_APPINSIGHTS_CONNECTION_STRING で指定する
        self.appinsights = (os.environ.get("LLM_JUDGE_APPINSIGHTS_CONNECTION_STRING")
                            or os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"))

    def sinks(self) -> list[str]:
        if not self.enabled:
            return []
        return [name for name, target in (("cosmos", self.cosmos), ("appinsights", self.appinsights)) if target]

    def send(self, sink: str, docs: list[dict]) -> None:
        import publish_results
        if sink == "cosmos":
            publish_results.send_cosmos(self.cosmos, docs)
        elif sink == "appinsights":
            publish_results.send_appinsights(self.appinsights, docs)
        else:
            raise ValueError(f"未知の送信先: {sink}")


class GitHubReviewRepo:
    """レビュー用リポジトリ（評価対象とは別の非公開リポジトリ）に資料を置き、Issue で人間の判断を受け取る。

    LLM_JUDGE_REVIEW_REPO（owner/name）と、そのリポジトリだけに Contents / Issues の読み書き権限を持つ
    fine-grained token を設定する。トークンは Key Vault のシークレットに置き、その URI を
    LLM_JUDGE_GITHUB_TOKEN_SECRET_URI（https://<vault>.vault.azure.net/secrets/<name>）に指定する
    （エージェントのマネージド ID に「Key Vault Secrets User」を付ける）。ローカルでは LLM_JUDGE_GITHUB_TOKEN でもよい。
    """

    LABEL = "llm-judge-review"

    def __init__(self, repo: str, token: str, api: str = "https://api.github.com"):
        self.repo, self.token, self.api = repo, token, api.rstrip("/")

    @classmethod
    def from_env(cls) -> "GitHubReviewRepo | None":
        repo = os.environ.get("LLM_JUDGE_REVIEW_REPO")
        if not repo:
            return None
        token = os.environ.get("LLM_JUDGE_GITHUB_TOKEN") or read_secret(os.environ.get("LLM_JUDGE_GITHUB_TOKEN_SECRET_URI"))
        if not token:
            raise RuntimeError("LLM_JUDGE_REVIEW_REPO を使うには LLM_JUDGE_GITHUB_TOKEN_SECRET_URI"
                               "（または LLM_JUDGE_GITHUB_TOKEN）が必要です")
        return cls(repo, token)

    def _request(self, method: str, path: str, body: dict | None = None) -> dict | list | None:
        req = urllib.request.Request(
            f"{self.api}/repos/{self.repo}/{path}", method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                raw = res.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and method == "GET":
                return None
            raise RuntimeError(f"GitHub API {method} {path}: {exc.code} {exc.read()[:300]!r}") from exc
        return json.loads(raw) if raw else None

    def get_file(self, path: str) -> str | None:
        found = self._request("GET", f"contents/{urllib.parse.quote(path)}")
        return None if found is None else base64.b64decode(found["content"]).decode("utf-8")

    def put_file(self, path: str, text: str, message: str) -> None:
        existing = self._request("GET", f"contents/{urllib.parse.quote(path)}")
        body = {"message": message, "content": base64.b64encode(text.encode("utf-8")).decode()}
        if existing is not None:
            if base64.b64decode(existing["content"]).decode("utf-8") == text:
                return
            body["sha"] = existing["sha"]
        self._request("PUT", f"contents/{urllib.parse.quote(path)}", body)

    def create_issue(self, title: str, body: str) -> dict:
        issue = self._request("POST", "issues", {"title": title, "body": body, "labels": [self.LABEL]})
        return {"number": issue["number"], "url": issue["html_url"]}

    def issue_state(self, number: int) -> str:
        return self._request("GET", f"issues/{number}")["state"]

    def comment(self, number: int, body: str) -> None:
        self._request("POST", f"issues/{number}/comments", {"body": body})

    def file_url(self, path: str) -> str:
        return f"https://github.com/{self.repo}/tree/HEAD/{path}"
