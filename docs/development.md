# 開発

[README に戻る](../README.md)

## 回帰テスト

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
az bicep build --file infra/main.bicep --stdout > /dev/null
```

[tests/test_review_regressions.py](../tests/test_review_regressions.py) は一時 Git リポジトリと一時評価データだけを使います。既存の評価結果は上書きせず、Azure への送信・デプロイもしません。Workbook の JSON とクエリの接続はローカルで確認しますが、実際の KQL 実行・Azure での表示確認は別途必要です。

[tests/test_hosted_service.py](../tests/test_hosted_service.py) は評価サービスを、Foundry・GitHub・可視化基盤を偽物に差し替えて確認します。HTTP の入口のテストは `azure-ai-agentserver-invocations` が入っている場合だけ実行します（「[ローカルで動かす](hosted-service.md#ローカルで動かす)」の依存を入れる）。

## ファイル構成

**評価の本体**

| パス | 内容 |
|---|---|
| [evaluators/](../evaluators/) | 評価器のプロンプト（共通の制約、観点ごとの採点基準、ペア比較） |
| [scripts/config.py](../scripts/config.py) | プロジェクト・Judge・評価器の定義、重み、プロンプトの組み立て |
| [scripts/register_evaluators.py](../scripts/register_evaluators.py) | 評価器を Foundry に登録 |
| [scripts/metrics.py](../scripts/metrics.py) | 機械的な品質指標の計算（共通祖先との差分、ベースラインのキャッシュ） |
| [scripts/make_row.py](../scripts/make_row.py) | 評価データと対応表の作成（匿名化・差分整形・指標の Δ・Skill） |
| [scripts/run_eval.py](../scripts/run_eval.py) | 絶対評価の実行 |
| [scripts/aggregate.py](../scripts/aggregate.py) | 集計・方式比較・人間レビューの振り分け |
| [scripts/run_pairwise.py](../scripts/run_pairwise.py) | ペア比較の実行と集計 |
| [scripts/calibration.py](../scripts/calibration.py) | 人間との答え合わせ、改善レポート、一致率の推移 |
| [scripts/publish_results.py](../scripts/publish_results.py) | 結果を Cosmos DB / Application Insights に送信 |
| [scripts/check_canaries.py](../scripts/check_canaries.py) | カナリアの作成と検出力の判定 |
| [scripts/foundry_eval.py](../scripts/foundry_eval.py) | Foundry の評価 API 呼び出しと実行メタデータの記録 |
| [scripts/stats_utils.py](../scripts/stats_utils.py) | 符号検定などの統計関数 |
| [scripts/azure_auth.py](../scripts/azure_auth.py) | Azure の資格情報の選び方（`LLM_JUDGE_AUTH`: cli / default / managed_identity） |
| [project_skills/](../project_skills/) | プロジェクトの Skill のテンプレート（エージェントと Judge に渡す） |

**GitHub Actions**

| パス | 内容 |
|---|---|
| [ci/llm-judge.yml](../ci/llm-judge.yml) | GitHub Actions のテンプレート（Run ごとの評価） |
| [ci/llm-judge-compare.yml](../ci/llm-judge-compare.yml) | GitHub Actions のテンプレート（タスクごとの方式比較） |
| [scripts/collect_task.py](../scripts/collect_task.py) | CI の Run ごとの成果物を集め、タスクの全方式が揃ったかを判定（llm-judge-compare.yml 用） |

**評価サービス（Foundry Hosted Agent）**

| パス | 内容 |
|---|---|
| [hosted/](../hosted/) | 評価サービス（Foundry Hosted Agent）。`azure.yaml`（エージェントとルーチン）、`sync_kit.py`、`src/llm-judge-service/`（`main.py` HTTP の入口・`service.py` 操作・`store.py` 状態の保存先・`adapters.py` Foundry / GitHub / 可視化基盤） |
| [scripts/judge_client.py](../scripts/judge_client.py) | 評価サービスを呼ぶクライアント（CI・手元から） |

**基盤・データ・検証**

| パス | 内容 |
|---|---|
| [infra/](../infra/) | 可視化基盤の Bicep、APIM ポリシー、Workbook、KQL |
| [datasets/](../datasets/) | サンプルデータとカナリア |
| [experiments/](../experiments/) | 検証実験（検証用リポジトリ・10 タスク・Hidden test・エージェント実行ハーネス・人間用の採点資料） |
| `results/<eval_id>/` | 試行ごとの結果、`run_meta.json`、集計結果 |
| [tests/](../tests/) | 回帰テスト（「[回帰テスト](#回帰テスト)」） |
| [pyrightconfig.json](../pyrightconfig.json) | エディタ（Pylance）の解析パス。`scripts/` などを `sys.path` に足して読み込むモジュールを解決する |
