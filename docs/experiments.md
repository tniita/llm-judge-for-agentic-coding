# 検証実験（experiments/）

[README に戻る](../README.md)

実タスクでの最初の検証として、検証用リポジトリ [experiments/shoplib_base/](../experiments/shoplib_base/) に 10 タスク（[experiments/tasks/](../experiments/tasks/)、Hidden test は [experiments/hidden/](../experiments/hidden/)）を用意し、2 方式のコーディングエージェントに解かせて評価しました。HydraFusion の実行環境が無いため、方式は Claude Code + `claude-sonnet-5-5`（`sonnet-single`）と Claude Code + `claude-opus-5-5`（`opus-single`）で代用しています。

```bash
.venv/bin/python experiments/run_agents.py --attempts 1 --workers 4   # エージェント実行 → experiments/data/
.venv/bin/python scripts/run_eval.py experiments/data/runs.jsonl --name shoplib-10tasks
.venv/bin/python scripts/aggregate.py results/<eval_id> experiments/data/manifest.jsonl
.venv/bin/python scripts/run_pairwise.py experiments/data/runs.jsonl experiments/data/manifest.jsonl --repeats 2
```

エージェントが使えるのはファイル操作と、venv の pytest / ruff の実行だけです（それ以外のコマンドは拒否されます）。

GitHub Copilot CLI で `claude-opus-5.5`（`opus-5.5`）と Copilot Fusion（`copilot-fusion`）を比べる場合は `--agent copilot` を付けます。`--metrics` で指標の差分を、`--skills` で Skill（エージェントと Judge の両方に渡す）を評価データに含めます。コストは Copilot の AI クレジット（1 クレジット = $0.01）から換算します。Fusion が実際に使ったモデルは対応表の `agent_models` に記録され（`experiments/work/<run_id>/agent.json` の `models_used` にも残る）、Judge と同系統なら集計で警告します。

```bash
.venv/bin/python experiments/run_agents.py --agent copilot --out-dir experiments/data_copilot --metrics --skills project_skills --workers 4
```

## 結果（2026-10-22、各方式 10 タスク × 1 回）

Judge は `gpt-6.1-sol`（`2026-09-29`）です。絶対評価は 3 回採点（[eval_486c…](../results/eval_486c5635e38a4d768d9ef2fddfb8a692/)）、ペア比較は A/B 入れ替え × 2 回（[eval_0c63…](../results/eval_0c63f1696c2b4124833c8e72535d568b/)）です。

| 項目 | `opus-single` | `sonnet-single` |
|---|---|---|
| Hard Gate 合格率 | 100%（10/10） | 100%（10/10） |
| 平均総合点（0〜100） | 94.1 | 92.0 |
| 要件の充足率 | 98.3% | 98.3% |
| 重大問題の発生率 | 0% | 0% |
| テストの妥当性 / 堅牢性 / 説明（0〜5） | 4.9 / 4.2 / 4.6 | 4.1 / 4.4 / 4.8 |
| 要件充足度・設計・保守性・変更の適切さ（0〜5） | 4.8・5.0・4.8・4.7 | 4.8・5.0・4.8・4.7 |
| 1 試行あたりのコスト | $0.31 | $0.09 |
| 1 試行あたりのトークン（入力 / 出力） | 116k / 10.7k | 51k / 6.4k |
| 1 試行あたりの所要時間 | 145 秒 | 64 秒 |

| 方式どうしの比較 | `opus-single` の勝ち | `sonnet-single` の勝ち | 引き分け | 符号検定の p 値 |
|---|---|---|---|---|
| 絶対評価（総合点） | 7 | 1 | 2 | 0.070 |
| ペア比較 | 9 | 1 | 0 | 0.021 |

読み取れること:

- **天井効果:** 両方式とも合格率 100%、要件の充足率 98% で、総合点の差は平均 2.1 点しかありません。タスクが易しく、差がつきにくい条件でした。方式の差を見るには、難しいタスクを加えて 20 タスク以上 × 複数回の生成で再評価する必要があります
- **差はテストの妥当性に集中:** 観点別では、テストの妥当性（4.9 対 4.1）以外はほぼ同点でした
- **コストと品質のトレードオフ:** `opus-single` はペア比較で優位でした（p = 0.021、位置バイアス 0.05、順序の逆転なし）。一方で、コストは約 3.3 倍、所要時間は約 2.3 倍かかっています
- **人間レビュー:** 集計は `--gap 15` で行ったため、方式間の総合点差がすべての組で 15 未満となり、全 20 Run が人間レビュー対象になりました（うち 5 Run は試行間の差 2 点以上にも該当）。Judge の判断が人間と一致するかは未確認なので、上の優劣は暫定です
- **指標・Skill は未使用:** 実験データは `metrics.py` と Skill の機能を追加する前に作成したため、`metrics_delta` と `project_skills` を含みません（`variants.json` の指標の差分はすべて `null`）。含めて評価するには、`run_agents.py` が呼ぶ `make_row.py` に `--metrics` / `--skills` を渡すよう変更し、データを作り直す必要があります

## 人間による答え合わせ（未実施）

採点資料は [experiments/review/](../experiments/review/) にあります。方式名と Judge の点数は載せていません。

1. **ペア比較（優先）:** [experiments/review/pairs/](../experiments/review/pairs/) の `Q01.md`〜`Q10.md` を読み、[pairs_review.csv](../experiments/review/pairs_review.csv) の `better` 列に `A` / `B` / `same` を入れる
2. **絶対評価:** [experiments/review/runs/](../experiments/review/runs/) の 10 件を読み、[review.csv](../experiments/review/review.csv) に観点ごとの点数（0〜5）と `uncaught_critical`（1=問題なし / 0=問題あり）を入れる。採点基準は [evaluators/](../evaluators/) と同じ
3. 採点が終わったら突き合わせる。`pairs/_key.json` は採点が終わるまで開かない

```bash
.venv/bin/python scripts/calibration.py compare-pairs experiments/review/pairs_review.csv \
  experiments/review/pairs/_key.json results/eval_0c63f1696c2b4124833c8e72535d568b
.venv/bin/python scripts/calibration.py compare experiments/review/review.csv \
  results/eval_486c5635e38a4d768d9ef2fddfb8a692/summary.jsonl
```
