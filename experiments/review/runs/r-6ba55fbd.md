# r-6ba55fbd

## タスク

`OrderService.list_orders` にページングと並び順を追加する。

- シグネチャ: `list_orders(user: User, page: int = 1, page_size: int = 20) -> list[Order]`

R1: 注文を created_at の新しい順に並べる。created_at が同じなら id の大きい順
R2: page は 1 始まり。page_size 件ずつ区切った page 番目を返す。最後のページより後なら空リストを返す
R3: page が 1 未満、または page_size が 1〜100 の範囲外なら ValueError を送出する
R4: 一般ユーザーは自分の注文だけ、admin ロールのユーザーは全ユーザーの注文を取得できる
R5: 引数を省略した既存の呼び出し `list_orders(user)` は引き続き動く
完了条件: 既存テストと追加したテストがすべて通ること

## PR 説明

# `OrderService.list_orders` にページングと並び順を追加

## 概要

`OrderService.list_orders` を次のシグネチャに拡張しました。

```python
list_orders(user: User, page: int = 1, page_size: int = 20) -> list[Order]
```

## 変更内容

### `shoplib/orders.py`

| 要件 | 実装 |
| --- | --- |
| R1 並び順 | `(created_at, id)` をキーに降順ソート。`created_at` が同じ注文は `id` の大きい順になる |
| R2 ページング | 1 始まりのページ番号で `orders[(page-1)*page_size : page*page_size]` を返す。最後のページより後はスライスの性質上、空リストになる |
| R3 入力チェック | `page < 1` または `page_size` が 1〜100 の範囲外なら `ValueError`。チェックは注文の取得より先に行うので、注文が 0 件でも不正な引数は検出される。上限は `OrderService.MAX_PAGE_SIZE = 100` |
| R4 閲覧範囲 | `user.is_admin` が真なら全ユーザーの注文、それ以外は `user_id == user.id` の注文だけ。**絞り込みはページングの前に行う**ので、一般ユーザーのページが他人の注文で欠けることはない |
| R5 互換性 | 新しい引数はどちらもデフォルト値付き。`list_orders(user)` は今後も動く |

補足:
- `OrderRepository.all()` は毎回新しいリストを返すので、そのリストをソートしてもリポジトリ内部の状態は変わりません（テストで確認済み）。
- **動作の変更点:** これまで `list_orders(user)` は**全件**を挿入順で返していましたが、今後は**新しい順の先頭 20 件**を返します。21 件以上の注文がある既存の呼び出し元で全件が必要な場合は、ページを順に取得する必要があります。現時点でリポジトリ内に `list_orders` の呼び出し元はありません。

### `tests/test_list_orders.py`（新規）

要件ごとのテストです。注文はリポジトリに直接保存し、`created_at` を固定して結果が毎回同じになるようにしています。

- R1: 保存順・id 順と時刻順がずれたデータで新しい順になること。同時刻の注文が id の降順で、前後の時刻とも正しく並ぶこと
- R2: 各ページ・最後の半端なページ・最終ページの次・かなり先のページ。全ページをつなぐと重複も欠けもないこと。件数が page_size のちょうど倍数のとき余分なページが出ないこと。デフォルトの 20 件。注文 0 件。位置引数での呼び出し
- R3: `page` が 0 と -1、`page_size` が 0・-1・101 で `ValueError`。境界値 1 と 100 は通ること。注文 0 件でもチェックされること
- R4: 一般ユーザーは自分の注文だけ見えること。ページングが絞り込み後に行われること。admin は全件（admin 自身の注文も含む）見えること。admin でのページング。注文のないユーザー
- R5 / 結合: `place_order` で作った注文を、引数なしの `list_orders(user)` で取得できること

## 確認方法

```bash
/home/tniita/ghcp-foundry/.venv/bin/python -m pytest -q   # 30 passed（既存 3 + 追加 27）
/home/tniita/ghcp-foundry/.venv/bin/ruff check .          # All checks passed!
```

## 補足・未確認事項

- 元の実装に戻して新テストが失敗するかの確認は、作業環境の制約で実行できていません。テストは並び順・ページング・入力チェックの具体的な値を確認しているため、元の実装（挿入順・全件・チェックなし）では R1〜R3 の多くと admin 関連のテストが失敗するはずです。
- `page` / `page_size` の型チェック（`int` 以外の値や `bool` を渡した場合）は要件にないため追加していません。

## 実装

### CI 結果

```
Build: success
Tests (repository): 30 passed in 0.02s
Hidden tests: passed
Lint (ruff): 0 issues
CodeQL: not run
```

### 差分

```diff
diff --git a/shoplib/orders.py b/shoplib/orders.py
index d5b6fe1..9255968 100644
--- a/shoplib/orders.py
+++ b/shoplib/orders.py
@@ -19,32 +19,54 @@ class OrderRepository:
 
     def get(self, order_id: int) -> Order | None:
         return self._orders.get(order_id)
 
     def all(self) -> list[Order]:
         return list(self._orders.values())
 
 
 class OrderService:
     def __init__(self, inventory: Inventory, repository: OrderRepository, clock=datetime.now) -> None:
         self.inventory = inventory
         self.repository = repository
         self.clock = clock
 
     def place_order(self, user: User, cart: Cart) -> Order:
         if not cart.items:
             raise ValueError("cart is empty")
         for item in cart.items:
             self.inventory.reserve(item.product.sku, item.quantity)
         order = Order(
             id=self.repository.next_id(),
             user_id=user.id,
             items=[CartItem(i.product, i.quantity) for i in cart.items],
             total=cart.subtotal(),
             created_at=self.clock(),
         )
         self.repository.save(order)
         cart.clear()
         return order
 
-    def list_orders(self, user: User) -> list[Order]:
-        return [o for o in self.repository.all() if o.user_id == user.id]
+    MAX_PAGE_SIZE = 100
+
+    def list_orders(self, user: User, page: int = 1, page_size: int = 20) -> list[Order]:
+        """Return one page of orders visible to ``user``, newest first.
+
+        Orders are sorted by ``created_at`` descending, ties broken by ``id`` descending.
+        Admin users see orders from all users; other users see only their own.
+        ``page`` is 1-based; a page beyond the last one yields an empty list.
+
+        Raises:
+            ValueError: if ``page < 1`` or ``page_size`` is not within 1..100.
+        """
+        if page < 1:
+            raise ValueError(f"page must be >= 1, got {page}")
+        if not 1 <= page_size <= self.MAX_PAGE_SIZE:
+            raise ValueError(f"page_size must be between 1 and {self.MAX_PAGE_SIZE}, got {page_size}")
+
+        orders = self.repository.all()
+        if not user.is_admin:
+            orders = [o for o in orders if o.user_id == user.id]
+        orders.sort(key=lambda o: (o.created_at, o.id), reverse=True)
+
+        start = (page - 1) * page_size
+        return orders[start : start + page_size]
diff --git a/tests/test_list_orders.py b/tests/test_list_orders.py
new file mode 100644
index 0000000..a775da9
--- /dev/null
+++ b/tests/test_list_orders.py
@@ -0,0 +1,207 @@
+from datetime import datetime, timedelta
+from decimal import Decimal
+
+import pytest
+
+from shoplib.cart import Cart
+from shoplib.inventory import Inventory
+from shoplib.models import Order, Product, Role, User
+from shoplib.orders import OrderRepository, OrderService
+
+ALICE = User(1, "alice")
+BOB = User(2, "bob")
+ADMIN = User(99, "root", role=Role.ADMIN)
+
+BASE = datetime(2026, 1, 1, 12, 0, 0)
+
+
+def make_order(order_id: int, user_id: int, created_at: datetime) -> Order:
+    return Order(id=order_id, user_id=user_id, items=[], total=Decimal("0"), created_at=created_at)
+
+
+def make_service(orders: list[Order]) -> OrderService:
+    repo = OrderRepository()
+    for order in orders:
+        repo.save(order)
+    return OrderService(Inventory(), repo)
+
+
+def ids(orders: list[Order]) -> list[int]:
+    return [o.id for o in orders]
+
+
+# R1: ordering ---------------------------------------------------------------
+
+
+def test_orders_sorted_by_created_at_desc():
+    # Saved in an order that is neither chronological nor by id.
+    service = make_service(
+        [
+            make_order(1, ALICE.id, BASE + timedelta(minutes=5)),
+            make_order(2, ALICE.id, BASE),
+            make_order(3, ALICE.id, BASE + timedelta(minutes=10)),
+            make_order(4, ALICE.id, BASE - timedelta(minutes=1)),
+        ]
+    )
+    assert ids(service.list_orders(ALICE)) == [3, 1, 2, 4]
+
+
+def test_same_created_at_sorted_by_id_desc():
+    service = make_service(
+        [
+            make_order(5, ALICE.id, BASE),
+            make_order(7, ALICE.id, BASE),
+            make_order(6, ALICE.id, BASE),
+            make_order(8, ALICE.id, BASE - timedelta(seconds=1)),
+            make_order(1, ALICE.id, BASE + timedelta(seconds=1)),
+        ]
+    )
+    assert ids(service.list_orders(ALICE)) == [1, 7, 6, 5, 8]
+
+
+# R2: paging -----------------------------------------------------------------
+
+
+@pytest.fixture
+def five_orders_service():
+    # ids 1..5, id 5 is newest -> expected global order [5, 4, 3, 2, 1]
+    return make_service([make_order(i, ALICE.id, BASE + timedelta(minutes=i)) for i in range(1, 6)])
+
+
+@pytest.mark.parametrize(
+    ("page", "expected"),
+    [
+        (1, [5, 4]),
+        (2, [3, 2]),
+        (3, [1]),  # last, partial page
+        (4, []),  # just past the last page
+        (100, []),  # far past the last page
+    ],
+)
+def test_paging(five_orders_service, page, expected):
+    assert ids(five_orders_service.list_orders(ALICE, page=page, page_size=2)) == expected
+
+
+def test_pages_cover_all_orders_without_overlap(five_orders_service):
+    collected = []
+    page = 1
+    while chunk := five_orders_service.list_orders(ALICE, page=page, page_size=2):
+        collected.extend(ids(chunk))
+        page += 1
+    assert collected == [5, 4, 3, 2, 1]
+
+
+def test_exact_multiple_of_page_size_has_no_extra_page(five_orders_service):
+    assert ids(five_orders_service.list_orders(ALICE, page=1, page_size=5)) == [5, 4, 3, 2, 1]
+    assert five_orders_service.list_orders(ALICE, page=2, page_size=5) == []
+
+
+def test_default_page_size_is_20():
+    service = make_service([make_order(i, ALICE.id, BASE + timedelta(minutes=i)) for i in range(1, 26)])
+    first = service.list_orders(ALICE)
+    assert len(first) == 20
+    assert ids(first) == list(range(25, 5, -1))
+    assert ids(service.list_orders(ALICE, page=2)) == [5, 4, 3, 2, 1]
+
+
+def test_no_orders_returns_empty_list():
+    assert make_service([]).list_orders(ALICE) == []
+
+
+def test_positional_arguments(five_orders_service):
+    assert ids(five_orders_service.list_orders(ALICE, 2, 2)) == [3, 2]
+
+
+# R3: validation -------------------------------------------------------------
+
+
+@pytest.mark.parametrize("page", [0, -1])
+def test_invalid_page_raises(five_orders_service, page):
+    with pytest.raises(ValueError):
+        five_orders_service.list_orders(ALICE, page=page)
+
+
+@pytest.mark.parametrize("page_size", [0, -1, 101])
+def test_invalid_page_size_raises(five_orders_service, page_size):
+    with pytest.raises(ValueError):
+        five_orders_service.list_orders(ALICE, page_size=page_size)
+
+
+@pytest.mark.parametrize("page_size", [1, 100])
+def test_page_size_boundaries_are_allowed(five_orders_service, page_size):
+    result = five_orders_service.list_orders(ALICE, page_size=page_size)
+    assert len(result) == min(page_size, 5)
+
+
+def test_validation_happens_even_without_orders():
+    service = make_service([])
+    with pytest.raises(ValueError):
+        service.list_orders(ALICE, page=0)
+    with pytest.raises(ValueError):
+        service.list_orders(ALICE, page_size=101)
+
+
+# R4: visibility -------------------------------------------------------------
+
+
+@pytest.fixture
+def mixed_service():
+    return make_service(
+        [
+            make_order(1, ALICE.id, BASE + timedelta(minutes=1)),
+            make_order(2, BOB.id, BASE + timedelta(minutes=2)),
+            make_order(3, ALICE.id, BASE + timedelta(minutes=3)),
+            make_order(4, BOB.id, BASE + timedelta(minutes=4)),
+            make_order(5, ADMIN.id, BASE + timedelta(minutes=5)),
+        ]
+    )
+
+
+def test_customer_sees_only_own_orders(mixed_service):
+    assert ids(mixed_service.list_orders(ALICE)) == [3, 1]
+    assert ids(mixed_service.list_orders(BOB)) == [4, 2]
+
+
+def test_customer_paging_applies_after_filtering(mixed_service):
+    assert ids(mixed_service.list_orders(BOB, page=1, page_size=1)) == [4]
+    assert ids(mixed_service.list_orders(BOB, page=2, page_size=1)) == [2]
+    assert mixed_service.list_orders(BOB, page=3, page_size=1) == []
+
+
+def test_admin_sees_all_orders(mixed_service):
+    assert ids(mixed_service.list_orders(ADMIN)) == [5, 4, 3, 2, 1]
+
+
+def test_admin_paging(mixed_service):
+    assert ids(mixed_service.list_orders(ADMIN, page=2, page_size=2)) == [3, 2]
+
+
+def test_user_without_orders_gets_empty_list(mixed_service):
+    assert mixed_service.list_orders(User(42, "carol")) == []
+
+
+# R5 / integration: orders created through place_order -------------------------
+
+
+def test_list_orders_with_place_order_and_default_args():
+    pen = Product("PEN", "Pen", Decimal("120"))
+    inventory = Inventory()
+    inventory.set_stock("PEN", 10)
+    times = iter([BASE, BASE + timedelta(minutes=1), BASE + timedelta(minutes=1)])
+    service = OrderService(inventory, OrderRepository(), clock=lambda: next(times))
+
+    placed = []
+    for user in (ALICE, ALICE, BOB):
+        cart = Cart()
+        cart.add(pen)
+        placed.append(service.place_order(user, cart))
+
+    assert ids(service.list_orders(ALICE)) == [placed[1].id, placed[0].id]
+    assert ids(service.list_orders(BOB)) == [placed[2].id]
+    assert ids(service.list_orders(ADMIN)) == [placed[2].id, placed[1].id, placed[0].id]
+
+
+def test_list_orders_does_not_mutate_repository(five_orders_service):
+    before = ids(five_orders_service.repository.all())
+    five_orders_service.list_orders(ALICE, page=1, page_size=2)
+    assert ids(five_orders_service.repository.all()) == before
```
