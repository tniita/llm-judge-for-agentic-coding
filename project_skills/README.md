# プロジェクトの Skill（評価基準）

評価対象のコーディングエージェントと Judge の両方に、同じ Skill を渡すためのテンプレートです。
評価対象リポジトリに合わせて各ファイルを書き換え、`make_row.py --skills <このディレクトリ>` で Judge に渡します。
エージェント側にも同じ内容を渡してください（例: リポジトリの `.github/copilot-instructions.md` や Agent Skills として配置する）。

| ファイル | 内容 |
|---|---|
| [coding_rules.md](coding_rules.md) | コーディング規約（命名、フォーマット、レイヤー） |
| [glossary.md](glossary.md) | 業務用語集（ドメイン用語とコード上の表現の対応） |
| [architecture.md](architecture.md) | アーキテクチャ方針・レイヤー構成 |
| [adr.md](adr.md) | ADR（設計判断の記録） |
| [libraries.md](libraries.md) | 使ってよいライブラリ・使ってはいけないライブラリ、使うべき API |
| [constraints.md](constraints.md) | プロジェクト固有の制約・今後の方向性 |

- この README は Judge に渡しません（`make_row.py` が除外します）
- Judge は、Skill に**明記された**ルールへの違反だけを指摘します。書かれていない好みでは減点しないため、守ってほしいルールは具体的に書いてください
- 長すぎる場合は `--max-skills-chars`（既定 40,000 文字）で切り詰められます。評価に関係するルールに絞ってください
