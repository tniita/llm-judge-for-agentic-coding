# 評価の設計

[README に戻る](../README.md)

## 評価器

プロンプトは [evaluators/](../evaluators/) にあり、[scripts/config.py](../scripts/config.py) の `build_prompt()` が「共通の制約（[common.md](../evaluators/common.md)）＋観点ごとの採点基準＋入力」を組み立てて登録します。

| 評価器 | 採点 | 重み | Judge に渡す入力 |
|---|---|---|---|
| `code_pr_requirements` 要件充足度 | 0〜5 | 3 | 要件・差分・周辺コード・CI・Skill |
| `code_pr_test_quality` テストの妥当性 | 0〜5 | 2 | 要件・差分・周辺コード・CI・Skill |
| `code_pr_robustness` エラー処理・堅牢性 | 0〜5 | 2 | 要件・差分・周辺コード・CI・Skill |
| `code_pr_design` 設計の妥当性 | 0〜5 | 1 | 要件・差分・周辺コード・CI・**指標の Δ**・Skill |
| `code_pr_maintainability` 保守性・可読性 | 0〜5 | 1 | 要件・差分・CI・**指標の Δ**・Skill |
| `code_pr_change_scope` 変更の適切さ | 0〜5 | 1 | 要件・差分・CI・Skill |
| `code_pr_explanation` 説明 | 0〜5 | 0.5 | 要件・差分・CI・**PR 説明** |
| `code_pr_uncaught_critical` CI 未検出の重大問題なし | true / false | － | 要件・差分・周辺コード・CI・Skill |
| `code_pr_pairwise` ペア比較 | 1（A 優位）〜5（B 優位） | － | 要件・Skill・2 つの差分と CI・**指標の Δ** |

設計上の工夫:

- **小項目に分けて根拠を書かせる:** 0〜100 点を直接つけさせず、観点ごとに 0〜5 点で、差分のファイルと行番号を根拠として必ず書かせる
- **PR 説明は「説明」の評価器だけに渡す:** 説明の上手さに他の観点が引っ張られないようにするため
- **CI で判定済みの項目は再評価しない:** テストの成否や Lint 件数は前提情報としてのみ使う
- **confidence:** 各評価器は理由の先頭に `[confidence: high|medium|low]` を付ける（Foundry の評価器は `result` と `reason` しか返せないため）
- **指標は Δ だけを、関係する観点にだけ渡す:** 複雑度・重複は保守性に、結合度・波及しやすさ・モジュール性は設計に渡す。Judge には再計算させず、数値の増減だけで減点しないよう指示する
- **Skill はエージェントと同じものを渡す:** 明記されたルールへの違反だけを指摘させ、書かれていない好みでは減点させない。要件と食い違う場合は要件を優先させる
- **要件ごとの判定:** 要件充足度の理由に `R1: met / R2: partial ...` の形で書かせ、集計時に取り出す
- **プロンプトインジェクション対策:** 差分や PR 説明の中の指示文には従わないよう明記

重みは暫定値です。人間との答え合わせの結果を見て [scripts/config.py](../scripts/config.py) の `WEIGHTS` を調整してください。

## 人間レビューに回す条件

次のどれかに当てはまる Run は `aggregate.py` の出力で「要」になります。

- 3 回の採点で、いずれかの観点の差が 2 点以上（`--spread`）
- `confidence: low` の試行がある
- CI で検出されていない重大な問題が報告された
- 同じタスクで、方式間の総合点（0〜100）の差が `--gap`（既定 5）未満
- 機械的な指標の差分が閾値を超えて悪化した（[scripts/config.py](../scripts/config.py) の `METRIC_THRESHOLDS`。既定は ΔV > +15、重複行 > +20、モジュール間の依存 > +5 本、ΔPC > +0.05、ΔQ < −0.05。暫定値）
- ペア比較で、A/B の順序を入れ替えると判定が逆転した（`run_pairwise.py` の出力）

## 評価の信頼性を保つ仕組み

| 仕組み | 内容 |
|---|---|
| 3 回採点して中央値 | 1 回だけ極端な点数が出ても影響しない |
| Judge を別系統のモデルに | 評価対象が Claude 系なので Judge は GPT 系 |
| ブラインド化 | モデル名・方式名・会話ログは Judge に渡さない。対応表は集計時だけ使う |
| A/B 入れ替え | ペア比較の位置バイアスを打ち消し、位置バイアスの大きさも出力する |
| Judge バージョン固定 | 想定と違うモデルバージョンなら実行を止める。意図した更新なら `JUDGE_MODEL_VERSION` を更新するか `ALLOW_JUDGE_VERSION_CHANGE=1` |
| 実行の記録 | `results/<eval_id>/run_meta.json` に Judge のバージョン、評価器のバージョン、ルーブリックのハッシュ、日時、Run ID を残す |
| 評価条件の照合 | 別々に採点した結果をまとめるときは、Judge・評価器のバージョン・ルーブリックのハッシュ・採点回数が同じかを確かめ、違えば止める。評価サービスは実験の作成時に条件を固定し、採点の前後で照合する |
| カナリア | 欠陥を仕込んだ差分で Judge の検出力を定期的に確認する（[カナリア](#カナリアjudge-の検出力チェック)） |

## カナリア（Judge の検出力チェック）

[datasets/canaries/](../datasets/canaries/) に、欠陥を仕込んだ差分と期待する判定（[expected.json](../datasets/canaries/expected.json)）があります。**ルーブリックや Judge モデルを変えたら、実データの前に必ず流して検出力が落ちていないか確かめてください。**

| ID | 仕込んだ欠陥 | 期待する判定 |
|---|---|---|
| C0 | なし（対照群） | 全観点で高得点、重大問題なし |
| C1 | アサーションが弱く、壊れた実装でも通るテスト | テストの妥当性 ≤ 2 |
| C2 | 認可チェック（所有者確認）の欠落。CI はすべて通る | 重大問題あり、要件充足度 ≤ 3 |
| C3 | 正規表現にアンカーがなく、不正入力を受理する | 堅牢性 ≤ 3、テストの妥当性 ≤ 3 |
| C4 | 要件と無関係な関数名変更と設定変更 | 変更の適切さ ≤ 3 |
| C5 | PR 説明に未実装の機能を記載 | 説明 ≤ 2 |

```bash
.venv/bin/python scripts/check_canaries.py build
.venv/bin/python scripts/run_eval.py datasets/canaries/canary_runs.jsonl --repeats 3 --name canary
.venv/bin/python scripts/check_canaries.py check results/<eval_id>
```

2026-10-22 時点の結果は 17/17（100%）です。
