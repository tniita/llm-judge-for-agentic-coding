import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = ROOT / "evaluators"
# Hosted Agent のコンテナなど、キットの置き場所に書き込めない環境では LLM_JUDGE_RESULTS_DIR で変える
RESULTS_DIR = Path(os.environ.get("LLM_JUDGE_RESULTS_DIR") or ROOT / "results")

# FOUNDRY_PROJECT_ENDPOINT は Foundry の Hosted Agent のコンテナに自動で設定される
PROJECT_ENDPOINT = (os.environ.get("AZURE_AI_PROJECT_ENDPOINT") or os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
                    or "https://foundry-claude-tniita.services.ai.azure.com/api/projects/proj-default")
# 評価対象（Claude 系）と別系統にして自己選好バイアスを避ける
JUDGE_DEPLOYMENT = os.environ.get("JUDGE_DEPLOYMENT") or "gpt-6.1-sol"
# デプロイのモデルが自動更新されると採点基準がずれるため、想定と違えば実行を止める
JUDGE_MODEL_VERSION = os.environ.get("JUDGE_MODEL_VERSION") or "2026-09-29"

FOUNDRY_PREVIEW_HEADERS = {"Foundry-Features": "Evaluations=V1Preview"}

# Judge に渡すフィールド。モデル名・実行方式・会話ログはデータセットに含めない
FIELD_LABELS = {
    "query": "タスクの要件と完了条件",
    "response": "コード差分（git diff）",
    "context": "関連する既存コード（差分に含まれない周辺コード。無ければ「なし」）",
    "ci_summary": "CI 結果の要約（テスト Pass/Fail、Lint 件数、CodeQL の指摘）",
    "pr_description": "PR 説明",
    "metrics_delta": "機械的に計算した品質指標の差分（ベースコミットとの比較。無ければ「なし」）",
    "project_skills": "プロジェクトの Skill（コーディング規約・用語集・アーキテクチャ・ADR・ライブラリ・制約。無ければ「なし」）",
    "response_a": "実装 A のコード差分",
    "ci_summary_a": "実装 A の CI 結果の要約",
    "metrics_delta_a": "実装 A の品質指標の差分（共通祖先との比較。無ければ「なし」）",
    "response_b": "実装 B のコード差分",
    "ci_summary_b": "実装 B の CI 結果の要約",
    "metrics_delta_b": "実装 B の品質指標の差分（共通祖先との比較。無ければ「なし」）",
}
DATASET_FIELDS = ["query", "response", "context", "ci_summary", "pr_description", "metrics_delta", "project_skills"]

# 評価対象のエージェントと同じ Skill を Judge にも渡す。PR 説明の評価以外のすべての観点で使う
_BASE = ["query", "response", "ci_summary", "project_skills"]
_WITH_CONTEXT = ["query", "response", "context", "ci_summary", "project_skills"]
# 指標の差分は、指標と対応する観点（設計・保守性）にだけ渡す
_WITH_METRICS = ["query", "response", "context", "ci_summary", "metrics_delta", "project_skills"]

# key: ルーブリック上の項目名（集計に使う）
EVALUATORS = {
    "requirements": {
        "name": "code_pr_requirements",
        "display_name": "Code PR: 要件充足度",
        "scoring": "ordinal",
        "fields": _WITH_CONTEXT,
    },
    "test_quality": {
        "name": "code_pr_test_quality",
        "display_name": "Code PR: テストの妥当性",
        "scoring": "ordinal",
        "fields": _WITH_CONTEXT,
    },
    "design": {
        "name": "code_pr_design",
        "display_name": "Code PR: 設計の妥当性",
        "scoring": "ordinal",
        "fields": _WITH_METRICS,
    },
    "maintainability": {
        "name": "code_pr_maintainability",
        "display_name": "Code PR: 保守性・可読性",
        "scoring": "ordinal",
        "fields": ["query", "response", "ci_summary", "metrics_delta", "project_skills"],
    },
    "change_scope": {
        "name": "code_pr_change_scope",
        "display_name": "Code PR: 変更の適切さ",
        "scoring": "ordinal",
        "fields": _BASE,
    },
    "robustness": {
        "name": "code_pr_robustness",
        "display_name": "Code PR: エラー処理・堅牢性",
        "scoring": "ordinal",
        "fields": _WITH_CONTEXT,
    },
    # PR 説明の上手さに他項目が引っ張られないよう、PR 説明はこの項目だけに渡す
    "explanation": {
        "name": "code_pr_explanation",
        "display_name": "Code PR: 説明",
        "scoring": "ordinal",
        "fields": ["query", "response", "ci_summary", "pr_description"],
    },
    "uncaught_critical": {
        "name": "code_pr_uncaught_critical",
        "display_name": "Code PR: CI 未検出の重大問題なし",
        "scoring": "boolean",
        "fields": _WITH_CONTEXT,
    },
}

# ペア比較は絶対評価と別に実行する（run_pairwise.py）
PAIRWISE = {
    "name": "code_pr_pairwise",
    "display_name": "Code PR: ペア比較（1=A 優位 … 5=B 優位）",
    "scoring": "ordinal",
    "min": 1,
    "max": 5,
    # 絶対評価と同じく Skill と指標の Δ を渡す（Skill は同じプロジェクトなので 1 つ）
    "fields": ["query", "project_skills", "response_a", "ci_summary_a", "metrics_delta_a",
               "response_b", "ci_summary_b", "metrics_delta_b"],
}

ORDINAL_MIN, ORDINAL_MAX, ORDINAL_THRESHOLD = 0, 5, 3

# 総合点（0〜100）の重み。暫定値なので、人間との答え合わせの結果を見て調整する
WEIGHTS = {
    "requirements": 3,
    "test_quality": 2,
    "robustness": 2,
    "design": 1,
    "maintainability": 1,
    "change_scope": 1,
    "explanation": 0.5,
}

# 機械的な指標の差分（metrics.py）がこれを超えて悪化したら人間レビューに回す。暫定値
# (向き, 閾値): "up" は増えると悪化、"down" は減ると悪化
METRIC_THRESHOLDS = {
    "complexity_total": ("up", 15),
    "duplicated_lines": ("up", 20),
    "coupling_edges": ("up", 5),
    "propagation_cost": ("up", 0.05),
    "modularity": ("down", 0.05),
}
METRIC_LABELS = {
    "complexity_total": "循環的複雑度の合計 ΔV",
    "duplicated_lines": "重複行",
    "duplication_rate": "重複率",
    "coupling_edges": "モジュール間の依存",
    "mean_instability": "不安定度 I の平均",
    "propagation_cost": "ΔPC",
    "modularity": "ΔQ",
}


def _output_block(result_desc: str) -> str:
    return (
        "# 出力形式（JSON のみ）\n"
        "{\n"
        f'  "result": {result_desc},\n'
        '  "reason": "<上記の reason の書式に従った根拠>"\n'
        "}"
    )


def _inputs_block(fields: list[str]) -> str:
    return "\n\n".join(f"## {FIELD_LABELS[f]}\n<{f}>\n{{{{{f}}}}}\n</{f}>" for f in fields)


def build_prompt(key: str) -> str:
    spec = EVALUATORS[key]
    common = (PROMPT_DIR / "common.md").read_text(encoding="utf-8").strip()
    rubric = (PROMPT_DIR / f"{key}.md").read_text(encoding="utf-8").strip()
    if spec["scoring"] == "ordinal":
        result_desc = f"<{ORDINAL_MIN} から {ORDINAL_MAX} の整数>"
    else:
        result_desc = "<true または false>"
    return f"{common}\n\n{rubric}\n\n# 入力\n\n{_inputs_block(spec['fields'])}\n\n{_output_block(result_desc)}\n"


def build_pairwise_prompt() -> str:
    rubric = (PROMPT_DIR / "pairwise.md").read_text(encoding="utf-8").strip()
    result_desc = f"<{PAIRWISE['min']} から {PAIRWISE['max']} の整数>"
    return f"{rubric}\n\n# 入力\n\n{_inputs_block(PAIRWISE['fields'])}\n\n{_output_block(result_desc)}\n"


def rubric_fingerprint() -> str:
    """採点の物差しのうち、キット側で決まるもの（評価プロンプト・観点の定義・重み・指標の閾値）のハッシュ。

    評価器のバージョンは Foundry に登録した時点のプロンプトを表すが、重みや閾値（集計の仕方）は含まない。
    両方を評価条件として記録し、異なる条件で採点した結果を同じ物差しで比べないようにする。
    """
    spec = {
        "prompts": {key: build_prompt(key) for key in EVALUATORS},
        "pairwise": build_pairwise_prompt(),
        "evaluators": EVALUATORS,
        "weights": WEIGHTS,
        "metric_thresholds": METRIC_THRESHOLDS,
        "ordinal": [ORDINAL_MIN, ORDINAL_MAX, ORDINAL_THRESHOLD],
    }
    return hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
