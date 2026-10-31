# コーディング規約

<!-- テンプレート: 評価対象リポジトリの規約に書き換える。以下は記入例 -->

- 命名: 関数・変数は snake_case、クラスは PascalCase、定数は UPPER_SNAKE_CASE
- 金額は `Decimal` で扱い、`float` を使わない
- 公開関数には型ヒントを付ける
- 例外は `ValueError` などの標準例外か、`errors.py` に定義したドメイン例外を使う。`Exception` を直接 raise しない
- ログは `logging.getLogger(__name__)` を使う。`print` を残さない
