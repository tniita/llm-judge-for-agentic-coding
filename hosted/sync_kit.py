"""評価サービスのデプロイ前に、キット（scripts/ と evaluators/）を src/llm-judge-service/kit/ にコピーする。

Hosted Agent のコードデプロイはサービスのディレクトリだけを送るため、リポジトリ直下のキットを同梱する。
azure.yaml の hooks（prepackage / predeploy）から呼ばれる。手動でも実行できる:
  python hosted/sync_kit.py
"""

import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
KIT = HERE / "src" / "llm-judge-service" / "kit"
# CLI 専用でサービスが使わないもの（metrics.py は radon が必要で、指標は CI 側で計算する）
EXCLUDE = {"metrics.py", "make_row.py", "register_evaluators.py", "check_canaries.py", "judge_client.py"}


def main() -> None:
    if KIT.exists():
        shutil.rmtree(KIT)
    (KIT / "scripts").mkdir(parents=True)
    for path in sorted((REPO / "scripts").glob("*.py")):
        if path.name not in EXCLUDE:
            shutil.copy2(path, KIT / "scripts" / path.name)
    shutil.copytree(REPO / "evaluators", KIT / "evaluators")
    print(f"キットをコピーしました → {KIT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
