"""LLM Judge の評価サービスを Foundry Hosted Agent（Invocations プロトコル）として公開する。

- POST /invocations            本文 {"op": ...}。操作の一覧は service.py の先頭を参照
                               同期の操作は 200、時間のかかる操作は 202 と invocation_id を返す
- GET  /invocations/{id}       202 で受け付けた操作の状態と結果
- POST /invocations/{id}/cancel  まだ始まっていない操作を取り消す
- GET  /invocations/docs/openapi.json  入力の仕様

採点などの重い処理はスレッドで動かし、状態は store.py の保存先（本番は Cosmos DB）に置く。
コンテナが止まって中断した処理は、定期実行の sweep（Foundry のルーチンから呼ぶ）が失敗として片付け、採点し直す。

ローカル実行:
  python hosted/sync_kit.py   # 不要（ローカルではリポジトリの scripts/ を使う）だが、デプロイ前と同じ形で試すなら実行する
  LLM_JUDGE_STATE_DIR=.state python hosted/src/llm-judge-service/main.py
  curl -sS -X POST localhost:8088/invocations -H 'Content-Type: application/json' -d '{"op":"get_experiment","experiment_id":"exp1"}'
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from starlette.requests import Request
from starlette.responses import JSONResponse

from azure.ai.agentserver.invocations import InvocationAgentServerHost

from adapters import FoundryJudge, GitHubReviewRepo, Publisher
from service import OPENAPI_SPEC, Service, ServiceError
from store import store_from_env

logger = logging.getLogger("llm_judge_service")


def default_service() -> Service:
    from azure_auth import credential

    store = store_from_env(credential)
    if type(store).__name__ == "FileStore":
        logger.warning("LLM_JUDGE_STATE_COSMOS_ENDPOINT が未設定のため、状態をコンテナ内のファイルに保存します。"
                       "Hosted Agent ではセッションごとにファイルシステムが分かれるため、本番では Cosmos DB を設定してください")
    return Service(store, FoundryJudge(), Publisher(), GitHubReviewRepo.from_env())


def create_app(service: Service | None = None, *, max_jobs: int | None = None) -> InvocationAgentServerHost:
    service = service or default_service()
    executor = ThreadPoolExecutor(max_workers=max_jobs or int(os.environ.get("LLM_JUDGE_MAX_JOBS") or "2"),
                                  thread_name_prefix="llm-judge-job")
    service.start_job = lambda job_doc_id: executor.submit(service.execute, job_doc_id)
    app = InvocationAgentServerHost(openapi_spec=OPENAPI_SPEC)

    def error(status: int, code: str, message: str) -> JSONResponse:
        return JSONResponse({"error": code, "message": message}, status_code=status)

    @app.invoke_handler
    async def handle_invoke(request: Request):
        raw = await request.body()
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return error(400, "invalid_request", '本文は JSON で、{"op": "..."} の形にしてください')
        if not isinstance(body, dict):
            return error(400, "invalid_request", '本文は JSON オブジェクト（{"op": "..."}）にしてください')
        try:
            status, payload = await asyncio.to_thread(service.handle, body, request.state.invocation_id)
        except ServiceError as exc:
            return error(exc.status, exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("op=%s の処理に失敗しました", body.get("op"))
            return error(500, "internal_error", f"{type(exc).__name__}: {exc}")
        return JSONResponse(payload, status_code=status)

    @app.get_invocation_handler
    async def handle_get(request: Request):
        job = await asyncio.to_thread(service.job_status, request.state.invocation_id)
        if job is None:
            return error(404, "not_found", f"invocation {request.state.invocation_id} はありません")
        return JSONResponse(job)

    @app.cancel_invocation_handler
    async def handle_cancel(request: Request):
        job = await asyncio.to_thread(service.cancel, request.state.invocation_id)
        if job is None:
            return error(404, "not_found", f"invocation {request.state.invocation_id} はありません")
        return JSONResponse(job)

    @app.shutdown_handler
    async def on_shutdown():
        # 実行中の採点は待たない（中断した分は sweep が片付けて採点し直す）
        executor.shutdown(wait=False, cancel_futures=True)

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    create_app().run()
