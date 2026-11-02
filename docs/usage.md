# 使い方（コマンドライン）

[README に戻る](../README.md)

## 1. 機械的な品質指標を計算する

`base` と `head` の共通祖先（merge-base）を改修前として、`head` との差分を `metrics.json` に出力します。PR 差分と同じ比較範囲なので、base ブランチだけに追加された変更は混ざりません。対象は Python で、テストコードは除外します。

```bash
.venv/bin/python scripts/metrics.py --repo ../target-repo --base main --head agent-branch --out metrics.json
```

| 指標 | 定義 | 差分 |
|---|---|---|
| 循環的複雑度（McCabe） | 制御フローの複雑さ（radon）。ネストした関数・クラスのメソッド・async 関数も含む全関数の合計 | ΔV（スコープ付きの関数名で増減を一覧） |
| 重複コード | 空白を正規化した 6 行単位で、2 か所以上に現れる行 | 重複行数・重複率の差（新たな重複箇所も一覧） |
| 結合度 | パッケージ内の import グラフでの Ce（依存する数）・Ca（依存される数）・I = Ce/(Ca+Ce) | ΔCe・ΔCa・ΔI（変わったモジュールごと） |
| 変更の波及しやすさ | Propagation Cost（到達可能性行列の密度） | ΔPC |
| モジュール性 | パッケージ（ディレクトリ）単位の分割に対する Newman のモジュラリティ Q | ΔQ |

ファイルごとの解析結果は git の blob SHA をキーに `--cache-dir`（既定 `.metrics-cache/`）に保存します。**ベースコミットの解析結果はキャッシュから取得され、今回の変更で中身が変わったファイルだけが再計算されます。** 除外したいパスは `--exclude 'vendor/*'` のように追加します。

同じスコープに同名の定義（property の getter/setter など）がある場合は、1 件にまとめて差分を出します（出現順で区別すると、間に定義を足しただけで別の関数と対応づくため）。解析器のバージョンが変わると旧キャッシュは削除します。`--prune-cache` を付けると今回の base / head で使わなかった解析結果も削除します（CI ではキャッシュが増え続けないよう付けています）。改修前・改修後のどちらかに構文エラーがある場合は `status: unavailable` と両側のエラーを記録し、指標の差分を `null` にします。計測不能を「改善」と扱わず、人間レビュー対象にして方式ごとの指標平均から除外します。過去の `metrics.json` は自動更新しないため、修正版で再計算してください。

## 2. プロジェクトの Skill を用意する

[project_skills/](../project_skills/) のテンプレート（コーディング規約・用語集・アーキテクチャ・ADR・ライブラリ・制約）を評価対象リポジトリに合わせて書き換えます。**評価対象のエージェントと Judge に同じ Skill を渡します。** Judge は Skill に明記されたルールへの違反だけを指摘し、書かれていない好みでは減点しません。

## 3. データを作る

Run ごとに [scripts/make_row.py](../scripts/make_row.py) を実行します。Judge に渡す評価データ（`datasets/runs.jsonl`）と、Judge に渡さない対応表（`datasets/manifest.jsonl`）に 1 行ずつ追記されます。

```bash
# リポジトリから差分を取る（周辺コードも見せるため前後 30 行付き）
.venv/bin/python scripts/make_row.py \
  --run-id r-8f3a --task-id T002 --variant fixed --attempt 1 --hard-gate pass \
  --task task.md --repo ../target-repo --base main --head agent-branch \
  --ci ci.txt --pr pr.md --metrics metrics.json --skills project_skills/ \
  --tokens-in 120000 --tokens-out 8000 --cost-usd 0.4 --duration-sec 210

# Hard Gate に落ちた Run は対応表にだけ記録する
.venv/bin/python scripts/make_row.py --run-id r-91c2 --task-id T002 --variant hydrafusion \
  --attempt 1 --hard-gate fail --cost-usd 0.7 --duration-sec 260
```

| 引数 | 内容 |
|---|---|
| `--run-id` | 推測できない ID。モデル名や方式名を含めない |
| `--variant` / `--attempt` | 方式名と、同じタスクでの何回目の生成か。対応表にだけ記録される |
| `--task` | 要件と完了条件。`R1:`、`R2:` のように ID を振ると、要件ごとの判定を集計できる |
| `--diff` または `--repo --base --head` | 変更差分 |
| `--context` | 差分に含まれない関連コード（呼び出し元など）。任意 |
| `--ci` | テストの Pass/Fail、Lint 件数、CodeQL の指摘の要約。Hidden test の結果もここに含める |
| `--pr` | PR 説明 |
| `--metrics` | `metrics.py` の出力。Judge には差分・主要な変化箇所（または計測不能の理由）、対応表には数値と計測状態を記録する。リポジトリ指定時は比較 SHA、`--diff` 指定時は変更された Python ファイルの一致を検証する |
| `--metrics-error` | `metrics.py` が失敗したときの理由。指標は計測不能として記録し、人間レビューに回す（`--metrics` とは併用不可） |
| `--skills` | プロジェクトの Skill（Markdown のディレクトリまたはファイル）。`README.md` と HTML コメントは除く。`--max-skills-chars`（既定 40,000 文字）で切り詰める |
| `--tokens-in` ほか | Run 全体の入力・出力トークン数、コスト、所要時間。実行基盤で集計した usage を入れる。未計測なら省略（0 にしない） |

`make_row.py` は次の処理を自動で行います。

- **匿名化:** `Co-authored-by` / `Generated with` などの行を削除し、モデル名・ベンダー名・方式名を `[REDACTED]` に置き換える。コード中の SDK 名まで伏せたくない場合は `--no-redact-diff`
- **差分の整形:** ロックファイルや `dist/` などの自動生成物を除外。`--max-diff-chars`（既定 150,000 文字）を超えるファイルは省略し、省略したことを Judge に伝える

## 4. 絶対評価

```bash
.venv/bin/python scripts/run_eval.py datasets/runs.jsonl --repeats 3 --name my-eval
```

8 つの評価器で 3 回ずつ採点し、`results/<eval_id>/` に保存します。最後に表示される `portal:` の URL から Foundry ポータルで結果を一覧できます。

## 5. 集計

```bash
.venv/bin/python scripts/aggregate.py results/<eval_id> datasets/manifest.jsonl --gap 5
```

| 出力 | 内容 |
|---|---|
| Run ごと | 観点別の中央値、重み付き総合点（0〜100）、要件の充足率、confidence、人間レビューの要否と理由 |
| 方式ごと | Hard Gate 合格率、平均総合点、要件の充足率、重大問題の発生率、1 試行あたりのコスト・トークン・所要時間（不合格の試行も含む）、指標の差分の平均 |
| 方式どうしの比較 | タスク単位の勝敗、総合点差の平均、符号検定の p 値。片方しか合格しなかったタスクは合格した方の勝ち |

結果は `summary.jsonl`（Run ごと）と `variants.json`（方式ごと・比較）にも保存されます。

Run ごとに別々に評価した結果（CI で Run ごとに `run_eval.py` を実行した場合など）は、`--manifest` と `--out-dir` を付けて複数の結果ディレクトリをまとめて集計できます。各ディレクトリの `run_meta.json` で評価条件（Judge のデプロイとモデルバージョン・評価器のバージョン・ルーブリックのハッシュ・採点回数）を照合し、異なるものが混ざっていれば止めます（承知の上で集計するなら `--allow-mixed-conditions`）。照合した条件は `variants.json` の `conditions` に残ります。

```bash
.venv/bin/python scripts/aggregate.py --manifest merged_manifest.jsonl --out-dir results/merged results/<eval_1> results/<eval_2>
```

## 6. ペア比較

```bash
.venv/bin/python scripts/run_pairwise.py datasets/runs.jsonl datasets/manifest.jsonl --name my-pairwise
```

同じタスクの 2 方式の PR を並べて「どちらをマージすべきか」を判定させます。位置バイアスを打ち消すため、**A/B を入れ替えて両方の順序で評価**します。勝敗は両方の順序の判定がそろった組だけで決め、判定の逆転・片方の順序の欠損や評価エラー・confidence が low の判定がある組は人間レビューに回します。Judge には絶対評価と同じく、プロジェクトの Skill と各実装の指標の差分も渡します（2 つの Run の Skill が異なる場合は同じ条件で比べられないため止めます）。複数回生成している場合は `attempt` の番号順に組にします。集計だけやり直す場合は `--results-dir results/<eval_id>`。

## 7. 人間との答え合わせと継続的な改善（最初に 10 件程度、以後は定期的に）

```bash
# 方式名も Judge の点数も載せない採点シートを書き出す
.venv/bin/python scripts/calibration.py export datasets/runs.jsonl --sample 10 --out review.csv
# review.csv の空欄（各観点 0〜5、uncaught_critical は 1=問題なし / 0=問題あり）を人間が埋める
.venv/bin/python scripts/calibration.py compare review.csv results/<eval_id>/summary.jsonl \
  --report calibration_report.md --history results/calibration_history.jsonl
```

観点ごとに完全一致率・±1 以内の一致率・平均誤差・順位相関を出し、2 点以上ずれたケースを一覧します。**±1 以内の一致率が 0.8 未満、または平均誤差が ±0.5 を超える観点は、ルーブリックを見直してください。** 採点基準は [evaluators/](../evaluators/) と同じものを人間にも渡します。`export --packet-dir review-packets/` の資料には、評価データに保存された周辺コード・Skill・指標の差分も含みます（現在の Skill ファイルは読み直しません）。`export-pairs` はペア比較の Judge と同じ入力（要件・Skill・各実装の CI・指標の差分・差分）を載せます。

人間レビューに振り分けられた Run / 組だけを資料にするには `--flagged` を使い、埋めたシートを最終判断として可視化基盤に送ります。Workbook のレビュー一覧に「未レビュー / レビュー済み」と人間の判断が表示されます。

```bash
.venv/bin/python scripts/calibration.py export datasets/runs.jsonl --flagged results/<eval_id>/summary.jsonl \
  --out review.csv --packet-dir review/
.venv/bin/python scripts/calibration.py export-pairs datasets/runs.jsonl results/<pairwise_eval_id> --flagged \
  --out pairs_review.csv --packet-dir review_pairs/   # 対応表 review_pairs/_key.json も書き出される
.venv/bin/python scripts/publish_results.py --project shoplib --manifest datasets/manifest.jsonl --human-reviews review.csv
.venv/bin/python scripts/publish_results.py --project shoplib --manifest datasets/manifest.jsonl \
  --pairwise-dir results/<pairwise_eval_id> --human-pair-reviews pairs_review.csv --pair-key review_pairs/_key.json
```

改善のループは次のとおりです。

1. `--report` で改善レポート（見直す観点と理由、ずれたケースごとの人間のコメントと Judge の理由）を書き出す
2. ルーブリック（`evaluators/*.md`）を直す。プロジェクト固有のルールが原因なら `project_skills/` に追記する
3. `register_evaluators.py` で再登録し、カナリアで検出力を確かめてから再評価する
4. `--history` に一致率を追記し、前回からの変化を確認する（Judge と評価器のバージョンも記録される）

## 8. 可視化基盤に送る

```bash
export LLM_JUDGE_COSMOS_ENDPOINT=https://<account>.documents.azure.com:443/
export APPLICATIONINSIGHTS_CONNECTION_STRING="InstrumentationKey=...;IngestionEndpoint=..."
.venv/bin/python scripts/publish_results.py --project shoplib --manifest datasets/manifest.jsonl --results-dir results/<eval_id>
# 独立した評価ディレクトリを一緒に送る（ペア比較だけなら --results-dir を省略）
.venv/bin/python scripts/publish_results.py --project shoplib --manifest datasets/manifest.jsonl \
  --results-dir results/<absolute_eval_id> --pairwise-dir results/<pairwise_eval_id>
```

対応表の全試行（Hard Gate 不合格を含む）と、`aggregate.py` / `run_pairwise.py` の結果を Cosmos DB（`llmjudge/results`、パーティションキー `/project`）に保存し、Application Insights にカスタムイベント（`LlmJudgeAttempt` / `LlmJudgeRun` / `LlmJudgeVariant` / `LlmJudgeComparison` / `LlmJudgePairwise` / `LlmJudgePair` / `LlmJudgeHumanReview`）として送ります。送信先は片方だけでも構いません。各ディレクトリの評価 ID を保持し、ペア比較には `summary.jsonl` は不要です。`LlmJudgePair` は両 Run ID と順序逆転のレビュー理由を持ち、Workbook / KQL のレビュー一覧に両 Run を表示します。`--dry-run` で送る内容を確認できます。基盤の作り方は「[可視化基盤](infrastructure.md)」を参照してください。
