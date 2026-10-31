"""ルーブリックの各項目とペア比較を、Foundry の評価器カタログにプロンプト型評価器として登録する。

プロンプトが最新版と同じならスキップし、変わっていれば新しいバージョンを作る。
"""

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import EvaluatorCategory, EvaluatorDefinitionType
from azure.identity import AzureCliCredential

from config import (
    EVALUATORS,
    FOUNDRY_PREVIEW_HEADERS,
    ORDINAL_MAX,
    ORDINAL_MIN,
    PAIRWISE,
    PROJECT_ENDPOINT,
    build_pairwise_prompt,
    build_prompt,
)


def latest_version(client: AIProjectClient, name: str):
    try:
        versions = list(client.evaluators.list_versions(name, type="custom"))
    except Exception:
        return None
    return max(versions, key=lambda v: int(v.version), default=None)


def register(client, *, name, display_name, metric_key, metric, fields, prompt, description) -> None:
    current = latest_version(client, name)
    if current is not None and current.definition.get("prompt_text") == prompt:
        print(f"skip   {name} v{current.version}（変更なし）")
        return

    created = client.evaluators.create_version(
        name=name,
        evaluator_version={
            "name": name,
            "categories": [EvaluatorCategory.QUALITY],
            "display_name": display_name,
            "description": description,
            "metadata": {"rubric_item": metric_key},
            "definition": {
                "type": EvaluatorDefinitionType.PROMPT,
                "prompt_text": prompt,
                "init_parameters": {
                    "type": "object",
                    "properties": {
                        "deployment_name": {"type": "string"},
                        "threshold": {"type": "number"},
                    },
                    "required": ["deployment_name", "threshold"],
                },
                "data_schema": {
                    "type": "object",
                    "properties": {f: {"type": "string"} for f in fields},
                    "required": fields,
                },
                "metrics": {metric_key: metric},
            },
        },
        headers=FOUNDRY_PREVIEW_HEADERS,
    )
    print(f"create {name} v{created.version}")


def main() -> None:
    client = AIProjectClient(PROJECT_ENDPOINT, AzureCliCredential(), allow_preview=True)

    for key, spec in EVALUATORS.items():
        if spec["scoring"] == "ordinal":
            metric = {
                "type": "ordinal",
                "desirable_direction": "increase",
                "min_value": ORDINAL_MIN,
                "max_value": ORDINAL_MAX,
            }
        else:
            metric = {"type": "boolean", "desirable_direction": "increase"}
        register(
            client,
            name=spec["name"],
            display_name=spec["display_name"],
            metric_key=key,
            metric=metric,
            fields=spec["fields"],
            prompt=build_prompt(key),
            description=f"コーディングエージェントの PR を評価する LLM Judge（{key}）",
        )

    register(
        client,
        name=PAIRWISE["name"],
        display_name=PAIRWISE["display_name"],
        metric_key="pairwise",
        metric={
            "type": "ordinal",
            "desirable_direction": "neutral",
            "min_value": PAIRWISE["min"],
            "max_value": PAIRWISE["max"],
        },
        fields=PAIRWISE["fields"],
        prompt=build_pairwise_prompt(),
        description="同じタスクに対する 2 つの PR を比較する LLM Judge",
    )


if __name__ == "__main__":
    main()
