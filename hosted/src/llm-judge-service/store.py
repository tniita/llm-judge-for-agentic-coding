"""評価サービスの状態（実験・Run・タスクの比較・人間レビュー・ジョブ）の保存先。

- CosmosStore: 本番。Hosted Agent はセッションごとにコンピューティングとファイルシステムが分かれ、
  止まったり別のコンテナで再開したりするため、状態はコンテナの外に置く
- FileStore: ローカル実行とテスト用

文書はすべて experiment_id をパーティションキーに持ち、kind で種類を区別する。
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path


class Conflict(Exception):
    """同じ id の文書がすでにある。"""


SYSTEM_FIELDS = ("_rid", "_self", "_etag", "_attachments", "_ts")


class Store:
    def get(self, experiment_id: str, doc_id: str) -> dict | None:
        raise NotImplementedError

    def create(self, doc: dict) -> dict:
        """同じ id があれば Conflict。"""
        raise NotImplementedError

    def upsert(self, doc: dict) -> dict:
        raise NotImplementedError

    def query(self, kind: str, experiment_id: str | None = None, **equals) -> list[dict]:
        raise NotImplementedError

    def find(self, doc_id: str) -> dict | None:
        """パーティションをまたいで id で探す（ジョブの状態の問い合わせ用）。"""
        raise NotImplementedError


class FileStore(Store):
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def _safe(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", lambda m: f"%{ord(m.group()):02x}", value)

    def _path(self, experiment_id: str, doc_id: str) -> Path:
        return self.root / self._safe(experiment_id) / f"{self._safe(doc_id)}.json"

    def get(self, experiment_id, doc_id):
        path = self._path(experiment_id, doc_id)
        with self._lock:
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def _write(self, doc: dict) -> dict:
        path = self._path(doc["experiment_id"], doc["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return doc

    def create(self, doc):
        with self._lock:
            if self._path(doc["experiment_id"], doc["id"]).exists():
                raise Conflict(doc["id"])
            return self._write(doc)

    def upsert(self, doc):
        with self._lock:
            return self._write(doc)

    def _all(self, experiment_id: str | None):
        dirs = [self.root / self._safe(experiment_id)] if experiment_id else sorted(self.root.glob("*/"))
        for d in dirs:
            for path in sorted(d.glob("*.json")):
                yield json.loads(path.read_text(encoding="utf-8"))

    def query(self, kind, experiment_id=None, **equals):
        with self._lock:
            return [d for d in self._all(experiment_id)
                    if d.get("kind") == kind and all(d.get(k) == v for k, v in equals.items())]

    def find(self, doc_id):
        with self._lock:
            return next((d for d in self._all(None) if d.get("id") == doc_id), None)


class CosmosStore(Store):
    def __init__(self, endpoint: str, database: str, container: str, credential):
        from azure.cosmos import CosmosClient

        self._container = CosmosClient(endpoint, credential=credential) \
            .get_database_client(database).get_container_client(container)

    @staticmethod
    def _clean(doc: dict | None) -> dict | None:
        return None if doc is None else {k: v for k, v in doc.items() if k not in SYSTEM_FIELDS}

    def get(self, experiment_id, doc_id):
        from azure.cosmos.exceptions import CosmosResourceNotFoundError
        try:
            return self._clean(self._container.read_item(doc_id, partition_key=experiment_id))
        except CosmosResourceNotFoundError:
            return None

    def create(self, doc):
        from azure.cosmos.exceptions import CosmosResourceExistsError
        try:
            return self._clean(self._container.create_item(doc))
        except CosmosResourceExistsError as exc:
            raise Conflict(doc["id"]) from exc

    def upsert(self, doc):
        return self._clean(self._container.upsert_item(doc))

    def query(self, kind, experiment_id=None, **equals):
        clauses = ["c.kind = @kind"]
        params = [{"name": "@kind", "value": kind}]
        for i, (key, value) in enumerate(equals.items()):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"不正なフィールド名: {key}")
            clauses.append(f"c.{key} = @v{i}")
            params.append({"name": f"@v{i}", "value": value})
        sql = f"SELECT * FROM c WHERE {' AND '.join(clauses)}"
        if experiment_id is None:
            items = self._container.query_items(sql, parameters=params, enable_cross_partition_query=True)
        else:
            items = self._container.query_items(sql, parameters=params, partition_key=experiment_id)
        return [self._clean(d) for d in items]

    def find(self, doc_id):
        items = list(self._container.query_items(
            "SELECT * FROM c WHERE c.id = @id", parameters=[{"name": "@id", "value": doc_id}],
            enable_cross_partition_query=True))
        return self._clean(items[0]) if items else None


def store_from_env(credential_factory) -> Store:
    endpoint = os.environ.get("LLM_JUDGE_STATE_COSMOS_ENDPOINT")
    if endpoint:
        return CosmosStore(endpoint, os.environ.get("LLM_JUDGE_STATE_DATABASE", "llmjudge"),
                           os.environ.get("LLM_JUDGE_STATE_CONTAINER", "service"), credential_factory())
    return FileStore(Path(os.environ.get("LLM_JUDGE_STATE_DIR", ".state")))
