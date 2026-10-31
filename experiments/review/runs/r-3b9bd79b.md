# r-3b9bd79b

## タスク

CSV から商品を一括登録する機能を追加する。

- 新規モジュール `shoplib/importer.py`
- `ImportResult`（dataclass）: `products: list[Product]`, `errors: list[tuple[int, str]]`（行番号とエラー内容）
- `import_products(csv_text: str) -> ImportResult`

R1: ヘッダー行（1 行目）を持つ CSV を読み、各行を Product に変換する。列は sku, name, price が必須で、category は任意（無ければ "general"）。price は Decimal にする
R2: 必須列がヘッダーに無ければ ValueError を送出する
R3: price が数値でない、または負の行は errors に（行番号, 理由）を記録してスキップする。行番号はヘッダーを 1 行目として数える
R4: 同じ sku が複数回出てきたら最初の行を採用し、2 回目以降は errors に記録してスキップする
R5: 空行は無視する。各値の前後の空白は取り除く。sku または name が空の行は errors に記録してスキップする
完了条件: 既存テストと追加したテストがすべて通ること

## PR 説明

# CSV から商品を一括登録する `shoplib.importer` を追加

## 概要
ヘッダー付き CSV テキストを読み込み、`Product` のリストに変換する機能を追加しました。
不正な行は処理全体を止めずにスキップし、`(行番号, 理由)` として結果に記録します。

## 変更内容
- **新規** `shoplib/importer.py`
  - `ImportResult`（dataclass）: `products: list[Product]`, `errors: list[tuple[int, str]]`
  - `import_products(csv_text: str) -> ImportResult`
- **新規** `tests/test_importer.py`: 要件 R1〜R5 とエッジケースのテスト
- 既存のコードは変更していません。

## 仕様（要件との対応）
| 要件 | 挙動 |
|---|---|
| R1 | 1 行目をヘッダーとして読む。`sku`, `name`, `price` は必須、`category` は任意（列が無い場合は `"general"`）。`price` は `Decimal` に変換。列の順番は自由 |
| R2 | 必須列がヘッダーに無い場合は `ValueError` を送出（メッセージに足りない列名を含む）。空文字列の入力も同じ扱い |
| R3 | `price` が数値でない、または負の行は `errors` に記録してスキップ。行番号はヘッダーを 1 行目として数える |
| R4 | 同じ `sku` が 2 回目以降に出てきた行は `errors` に記録してスキップ（最初の行を採用） |
| R5 | 空行は無視する。各値の前後の空白を取り除く。`sku` か `name` が空の行は `errors` に記録してスキップ |

### 仕様が曖昧な点についての判断
レビューで確認をお願いします。
- **行番号**: CSV の物理的な行番号です。無視した空行も行番号には数えるので、エディタで見た行番号と一致します。引用符の中に改行がある行は、その行の**開始行**を返します。
- **重複の判定**: 「既に採用された sku」と比べています。同じ sku の 1 回目の行が price 不正などでエラーになった場合、その後の正しい行を採用します（入力ミスを後の行で直したケースを救うため）。`sku` は前後の空白を取り除いてから比べ、大文字と小文字は区別します。
- **1 行に複数のエラーがある場合**: `sku 空` → `name 空` → `price 不正` → `sku 重複` の順にチェックし、最初に見つかった 1 件だけを記録します。
- **price**: `0` は有効です。`NaN` / `Infinity` は `Decimal` としては変換できますが、価格としては使えないので「数値でない」として扱います。`1,000` のような桁区切りも不正とします。セルが無い（列数が足りない）場合も「数値でない」になります。
- **category**: 列があっても値が空なら `"general"` にします。
- **空行**: 完全な空行に加えて、空白だけの行や `,,` のように値がすべて空の行も空行とみなして無視します。
- **ヘッダー**: 列名の前後の空白を取り除き、先頭の BOM（Excel で保存した CSV に付くことがある）も取り除きます。列名は大文字と小文字を区別します。同じ名前の列が複数ある場合は、先頭の列を使います。余分な列は無視します。

## 確認方法
```sh
/home/tniita/ghcp-foundry/.venv/bin/python -m pytest -q
/home/tniita/ghcp-foundry/.venv/bin/ruff check .
```

使用例（`tests/test_importer.py::test_usage_example` と同じ内容）:
```python
from shoplib.importer import import_products

result = import_products("sku,name,price\nPEN,Pen,120\nNOTE,Notebook,abc\nPEN,Pen2,130\n")
result.products  # [Product(sku='PEN', name='Pen', price=Decimal('120'), category='general')]
result.errors    # [(3, "price is not a number: 'abc'"), (4, "duplicate sku: 'PEN'")]
```

## 実装

### CI 結果

```
Build: success
Tests (repository): 39 passed in 0.02s
Hidden tests: passed
Lint (ruff): 0 issues
CodeQL: not run
```

### 差分

```diff
diff --git a/shoplib/importer.py b/shoplib/importer.py
new file mode 100644
index 0000000..9f5cc74
--- /dev/null
+++ b/shoplib/importer.py
@@ -0,0 +1,92 @@
+"""CSV からの商品一括登録."""
+
+import csv
+import io
+from dataclasses import dataclass, field
+from decimal import Decimal, InvalidOperation
+
+from shoplib.models import Product
+
+REQUIRED_COLUMNS = ("sku", "name", "price")
+DEFAULT_CATEGORY = "general"
+_BOM = "\N{ZERO WIDTH NO-BREAK SPACE}"
+
+
+@dataclass
+class ImportResult:
+    products: list[Product] = field(default_factory=list)
+    errors: list[tuple[int, str]] = field(default_factory=list)
+
+
+def _parse_price(raw: str) -> Decimal:
+    """price 文字列を Decimal に変換する. 不正なら ValueError."""
+    try:
+        price = Decimal(raw)
+    except InvalidOperation:
+        raise ValueError(f"price is not a number: {raw!r}") from None
+    if not price.is_finite():  # "NaN", "Infinity" などは数値として扱わない
+        raise ValueError(f"price is not a number: {raw!r}")
+    if price < 0:
+        raise ValueError(f"price must not be negative: {raw!r}")
+    return price
+
+
+def import_products(csv_text: str) -> ImportResult:
+    """ヘッダー付き CSV テキストから Product を読み込む.
+
+    - 必須列 (sku, name, price) がヘッダーに無ければ ValueError を送出する。
+    - 不正な行はスキップし、(行番号, 理由) を errors に記録する。
+      行番号はヘッダーを 1 行目とする物理行番号 (複数行にまたがる行はその開始行)。
+    - 同じ sku は最初に採用された行を優先し、以降の行は errors に記録する。
+    """
+    reader = csv.reader(io.StringIO(csv_text))
+
+    header_row = next(reader, None)
+    header = [name.strip() for name in header_row] if header_row is not None else []
+    if header:
+        header[0] = header[0].lstrip(_BOM).strip()  # Excel 等が付ける BOM を除去
+    missing = [col for col in REQUIRED_COLUMNS if col not in header]
+    if missing:
+        raise ValueError(f"missing required column(s): {', '.join(missing)}")
+    index: dict[str, int] = {}
+    for i, column in enumerate(header):
+        index.setdefault(column, i)  # 同名列は先頭を優先
+
+    result = ImportResult()
+    seen_skus: set[str] = set()
+    prev_line_end = reader.line_num
+
+    for row in reader:
+        line_no = prev_line_end + 1
+        prev_line_end = reader.line_num
+
+        values = [v.strip() for v in row]
+        if not any(values):  # 空行 (空白のみ・区切り文字のみの行を含む)
+            continue
+
+        def get(column: str, values: list[str] = values) -> str:
+            i = index.get(column)
+            return values[i] if i is not None and i < len(values) else ""
+
+        sku, name = get("sku"), get("name")
+        if not sku:
+            result.errors.append((line_no, "sku is empty"))
+            continue
+        if not name:
+            result.errors.append((line_no, "name is empty"))
+            continue
+
+        try:
+            price = _parse_price(get("price"))
+        except ValueError as e:
+            result.errors.append((line_no, str(e)))
+            continue
+
+        if sku in seen_skus:
+            result.errors.append((line_no, f"duplicate sku: {sku!r}"))
+            continue
+
+        seen_skus.add(sku)
+        result.products.append(Product(sku, name, price, get("category") or DEFAULT_CATEGORY))
+
+    return result
diff --git a/tests/test_importer.py b/tests/test_importer.py
new file mode 100644
index 0000000..5e0d3f4
--- /dev/null
+++ b/tests/test_importer.py
@@ -0,0 +1,185 @@
+from decimal import Decimal
+
+import pytest
+
+from shoplib.importer import ImportResult, import_products
+from shoplib.models import Product
+
+BOM = "\N{ZERO WIDTH NO-BREAK SPACE}"
+
+
+# R1: 基本的な読み込み
+def test_imports_rows_as_products():
+    csv_text = "sku,name,price,category\nPEN,Pen,120,stationery\nNOTE,Notebook,300.50,stationery\n"
+    result = import_products(csv_text)
+    assert isinstance(result, ImportResult)
+    assert result.products == [
+        Product("PEN", "Pen", Decimal("120"), "stationery"),
+        Product("NOTE", "Notebook", Decimal("300.50"), "stationery"),
+    ]
+    assert result.errors == []
+
+
+def test_price_is_decimal():
+    result = import_products("sku,name,price\nPEN,Pen,0.1\n")
+    price = result.products[0].price
+    assert isinstance(price, Decimal)
+    assert price == Decimal("0.1")
+
+
+def test_category_column_is_optional():
+    result = import_products("sku,name,price\nPEN,Pen,120\n")
+    assert result.products == [Product("PEN", "Pen", Decimal("120"), "general")]
+
+
+def test_empty_category_value_defaults_to_general():
+    result = import_products("sku,name,price,category\nPEN,Pen,120,\nNOTE,Notebook,300,  \n")
+    assert [p.category for p in result.products] == ["general", "general"]
+
+
+def test_column_order_does_not_matter():
+    result = import_products("price,category,name,sku\n120,office,Pen,PEN\n")
+    assert result.products == [Product("PEN", "Pen", Decimal("120"), "office")]
+
+
+def test_quoted_values_with_commas():
+    result = import_products('sku,name,price\nPEN,"Pen, blue",120\n')
+    assert result.products[0].name == "Pen, blue"
+
+
+def test_zero_price_is_allowed():
+    result = import_products("sku,name,price\nFREE,Sample,0\n")
+    assert result.products[0].price == Decimal("0")
+    assert result.errors == []
+
+
+def test_header_only_returns_empty_result():
+    result = import_products("sku,name,price\n")
+    assert result.products == []
+    assert result.errors == []
+
+
+def test_header_names_are_stripped_and_bom_removed():
+    result = import_products(BOM + " sku , name , price \nPEN,Pen,120\n")
+    assert result.products == [Product("PEN", "Pen", Decimal("120"))]
+
+
+# R2: 必須列の欠落
+@pytest.mark.parametrize("header", ["name,price", "sku,price", "sku,name", "sku,name,category"])
+def test_missing_required_column_raises(header):
+    with pytest.raises(ValueError):
+        import_products(f"{header}\nPEN,Pen,120\n")
+
+
+def test_missing_column_error_mentions_column_name():
+    with pytest.raises(ValueError, match="price"):
+        import_products("sku,name\nPEN,Pen\n")
+
+
+def test_empty_input_raises():
+    with pytest.raises(ValueError):
+        import_products("")
+
+
+# R3: price の検証
+@pytest.mark.parametrize("price", ["abc", "", "12a", "1,000", "NaN", "Infinity"])
+def test_non_numeric_price_is_error(price):
+    csv_text = f'sku,name,price\nPEN,Pen,"{price}"\nNOTE,Notebook,300\n'
+    result = import_products(csv_text)
+    assert result.products == [Product("NOTE", "Notebook", Decimal("300"))]
+    assert len(result.errors) == 1
+    line, reason = result.errors[0]
+    assert line == 2
+    assert reason
+
+
+def test_negative_price_is_error():
+    result = import_products("sku,name,price\nPEN,Pen,-1\nNOTE,Notebook,300\n")
+    assert [p.sku for p in result.products] == ["NOTE"]
+    assert len(result.errors) == 1
+    assert result.errors[0][0] == 2
+    assert "negative" in result.errors[0][1]
+
+
+def test_missing_price_cell_is_error():
+    result = import_products("sku,name,price\nPEN,Pen\n")
+    assert result.products == []
+    assert [line for line, _ in result.errors] == [2]
+
+
+def test_line_numbers_count_header_as_line_1():
+    csv_text = "sku,name,price\nA,a,1\nB,b,x\nC,c,3\nD,d,-5\n"
+    result = import_products(csv_text)
+    assert [p.sku for p in result.products] == ["A", "C"]
+    assert [line for line, _ in result.errors] == [3, 5]
+
+
+# R4: 重複 sku
+def test_duplicate_sku_keeps_first_row():
+    csv_text = "sku,name,price\nPEN,Pen,120\nNOTE,Notebook,300\nPEN,Pen v2,999\nPEN,Pen v3,1\n"
+    result = import_products(csv_text)
+    assert result.products == [
+        Product("PEN", "Pen", Decimal("120")),
+        Product("NOTE", "Notebook", Decimal("300")),
+    ]
+    assert [line for line, _ in result.errors] == [4, 5]
+    assert all("PEN" in reason for _, reason in result.errors)
+
+
+def test_duplicate_detection_uses_stripped_sku():
+    result = import_products("sku,name,price\nPEN,Pen,120\n  PEN  ,Pen2,130\n")
+    assert len(result.products) == 1
+    assert [line for line, _ in result.errors] == [3]
+
+
+def test_invalid_first_row_does_not_block_later_valid_row():
+    # 1 回目の行がエラーで採用されなかった場合、後の正しい行が「最初に採用される行」になる
+    result = import_products("sku,name,price\nPEN,Pen,abc\nPEN,Pen,120\n")
+    assert result.products == [Product("PEN", "Pen", Decimal("120"))]
+    assert [line for line, _ in result.errors] == [2]
+
+
+# R5: 空行・空白・空の sku/name
+def test_blank_lines_are_ignored_but_counted():
+    csv_text = "sku,name,price\n\nPEN,Pen,120\n   \n,,\nNOTE,Notebook,bad\n"
+    result = import_products(csv_text)
+    assert result.products == [Product("PEN", "Pen", Decimal("120"))]
+    # 空行も行番号としては数える (NOTE の行は 6 行目)
+    assert [line for line, _ in result.errors] == [6]
+
+
+def test_values_are_stripped():
+    result = import_products("sku,name,price,category\n  PEN , Blue Pen ,  120  , office \n")
+    assert result.products == [Product("PEN", "Blue Pen", Decimal("120"), "office")]
+
+
+@pytest.mark.parametrize(
+    "row,expected_word",
+    [(",Pen,120", "sku"), ("   ,Pen,120", "sku"), ("PEN,,120", "name"), ("PEN,  ,120", "name")],
+)
+def test_empty_sku_or_name_is_error(row, expected_word):
+    result = import_products(f"sku,name,price\n{row}\nNOTE,Notebook,300\n")
+    assert [p.sku for p in result.products] == ["NOTE"]
+    assert len(result.errors) == 1
+    assert result.errors[0][0] == 2
+    assert expected_word in result.errors[0][1]
+
+
+def test_crlf_line_endings():
+    result = import_products("sku,name,price\r\nPEN,Pen,120\r\nNOTE,Notebook,x\r\n")
+    assert [p.sku for p in result.products] == ["PEN"]
+    assert result.errors[0][0] == 3
+
+
+def test_multiline_quoted_field_reports_start_line():
+    csv_text = 'sku,name,price\nPEN,"Pen\nwith newline",120\nNOTE,Notebook,x\n'
+    result = import_products(csv_text)
+    assert result.products[0].name == "Pen\nwith newline"
+    assert result.errors[0][0] == 4
+
+
+def test_usage_example():
+    # PR_DESCRIPTION.md に載せた使用例
+    result = import_products("sku,name,price\nPEN,Pen,120\nNOTE,Notebook,abc\nPEN,Pen2,130\n")
+    assert result.products == [Product("PEN", "Pen", Decimal("120"), "general")]
+    assert result.errors == [(3, "price is not a number: 'abc'"), (4, "duplicate sku: 'PEN'")]
```
