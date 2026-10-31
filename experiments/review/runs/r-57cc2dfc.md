# r-57cc2dfc

## タスク

注文時の在庫引き当てが一部だけ成功したまま残る不具合を直し、複数スレッドから使っても安全にする。

- `Inventory.reserve_many(items: dict[str, int]) -> None` を追加する

R1: `reserve_many` はすべての SKU を引き当てられる場合だけ在庫を減らす。1 つでも足りなければ OutOfStockError を送出し、どの在庫も変更しない
R2: `OrderService.place_order` は `reserve_many` を使い、在庫不足で失敗したときに在庫が一部だけ減った状態を残さない
R3: `reserve` / `reserve_many` / `release` は複数スレッドから同時に呼ばれても在庫を過剰に引き当てない（スレッドセーフ）
R4: `reserve` / `reserve_many` / `release` に 0 以下の数量を渡したら ValueError を送出する
完了条件: 既存テストと追加したテストがすべて通ること

## PR 説明

# 在庫引き当てを全件一括・スレッドセーフにする

## 背景 / 不具合

- `OrderService.place_order` がカートの商品を 1 件ずつ `Inventory.reserve` していたため、
  途中の SKU が在庫不足になると、それより前の SKU だけ在庫が減ったまま `OutOfStockError` になっていた（一部だけ引き当てられた状態が残る）。
- `Inventory.reserve` / `release` は「在庫確認 → 書き込み」を排他制御なしで行っていたため、
  複数スレッドから同時に呼ぶと過剰な引き当てや更新の取りこぼしが起きていた。
  変更前のコードに今回追加したテストを当てると、在庫 20,000 に対して 31,430 件の引き当てが成功し、
  引き当てと解放を繰り返したあとの在庫が 100 から 96 に減ることを確認した。
- 0 や負の数量も受け付けていたため、`reserve(sku, -5)` で在庫を増やすことなどができた。

## 変更内容

### `shoplib/inventory.py`
- **`Inventory.reserve_many(items: dict[str, int]) -> None` を追加**（R1）
  1. まず全数量を検証する（0 以下なら `ValueError`）。
  2. ロックを取った状態で全 SKU の在庫が足りるか確認し、1 つでも足りなければ `OutOfStockError(sku)` を送出する。
  3. すべて足りた場合だけ全 SKU の在庫を減らす。
  - 検証が終わるまで在庫には一切書き込まないので、どちらの例外でも在庫は変わらない。
  - 呼び出し側が渡した dict の内容が検証中に変わっても影響を受けないよう、最初にコピーしてから処理する。
- **`threading.Lock` による排他制御**（R3）
  `_stock` の読み書き（`set_stock` / `available` / `reserve_many` / `release`）はすべてロック内で行う。
  これで「確認 → 更新」が他スレッドから割り込まれずに一度に実行される。
- **`reserve` は `reserve_many({sku: quantity})` に委譲**し、検証とロックの処理を 1 か所にまとめた。
- **数量の検証**（R4）: `reserve` / `reserve_many` / `release` は 0 以下の数量で `ValueError` を送出する。
  `reserve_many` では数量の検証を在庫確認より先に行う。

### `shoplib/orders.py`
- `place_order` はカート内容を `{sku: 数量}` にまとめ、`reserve_many` を 1 回だけ呼ぶようにした（R2）。
  在庫不足で失敗した場合、在庫・注文リポジトリ・カートはどれも変更されない。
  （今の `Cart` は SKU が重複しない作りだが、念のため同じ SKU の数量は合算している）

## 挙動の変化（レビュー時に確認してほしい点）

- 数量 0 の商品が入ったカート（例: `cart.add(product, 0)`）で `place_order` を呼ぶと、
  これまでは数量 0 の明細を含む注文がそのまま作られていたが、今後は `reserve_many` が `ValueError` を送出する。
  R4 に沿った挙動だと考えているが、カート側で 0 個の追加を許すかどうかはこの PR の対象外としている。
- `release` に 0 以下の数量を渡すと `ValueError` になる（これまでは何も起きないか、在庫が減っていた）。

## スコープ外 / 既知の制約

- 引き当てが成功したあとに注文の作成・保存（`repository.next_id()` / `save` / `clock()`）で例外が起きると、
  引き当て済みの在庫は戻らない。今回の要件（在庫不足での失敗）とは別の失敗経路なので手を入れていない。
  必要であれば、`try/except` で失敗時に `release` して引き当てを戻す処理を別 PR で入れる。
- ロックは `Inventory` インスタンスごとのプロセス内ロックで、マルチプロセスや複数サーバー間の排他は対象外。

## 確認方法

```sh
/home/tniita/ghcp-foundry/.venv/bin/python -m pytest -q   # 20 passed
/home/tniita/ghcp-foundry/.venv/bin/ruff check .           # All checks passed!
```

追加したテスト `tests/test_inventory.py`（17 件）:

| 要件 | テスト |
| --- | --- |
| R1 | 全件引き当て成功 / 不足する SKU が末尾・先頭・未登録の場合にどの在庫も変わらない / 空 dict は何もしない |
| R2 | 足りる SKU と足りない SKU が混ざったカートで `place_order` が失敗しても、在庫・注文・カートが変わらない / 複数 SKU の正常系 |
| R3 | 8 スレッドが在庫切れまで `reserve` / `reserve_many` を繰り返したとき、成功回数の合計が初期在庫とちょうど一致する / `reserve` と `release` を並行に繰り返しても在庫の合計が保たれる |
| R4 | `reserve` / `release` / `reserve_many` に 0・負数を渡すと `ValueError` になり在庫は変わらない / `reserve_many` では在庫不足より先に数量の不正を検出する |

- 並行性テストは `sys.setswitchinterval(1e-6)` でスレッド切り替えを頻繁にし（fixture で元の値に戻す）、
  各スレッドがループで何千回も呼び出すことで、ロックがなければ競合が起きるようにしている。
- 変更前のコードに追加テストを当てると 17 件中 16 件が失敗することを確認した
  （`reserve_many` がないことによる `AttributeError`、`ValueError` が出ないこと、一部だけ引き当てられた状態が残ること、
  並行実行時の過剰引き当て・更新の取りこぼし）。通ったのは、変更前から動いていた複数 SKU の正常系
  `test_place_order_multiple_items_reserves_all` の 1 件だけ。
- 並行性テストは当初「100 スレッドがそれぞれ 1 回だけ呼ぶ」形で書いたが、それだと変更前のコードでも通ってしまい
  競合を検出できなかったので、今の「少数のスレッドがループで長時間呼び続ける」形に書き直した。
- 変更後は並行性テストを 5 回連続で実行し、毎回通ることを確認した。

## 実装

### CI 結果

```
Build: success
Tests (repository): 20 passed in 1.99s
Hidden tests: passed
Lint (ruff): 0 issues
CodeQL: not run
```

### 差分

```diff
diff --git a/shoplib/inventory.py b/shoplib/inventory.py
index 53fc3c2..06f14b6 100644
--- a/shoplib/inventory.py
+++ b/shoplib/inventory.py
@@ -1,21 +1,50 @@
+import threading
+
+
 class OutOfStockError(Exception):
     pass
 
 
+def _validate_quantity(sku: str, quantity: int) -> None:
+    if quantity <= 0:
+        raise ValueError(f"quantity must be positive: {sku}={quantity}")
+
+
 class Inventory:
     def __init__(self) -> None:
         self._stock: dict[str, int] = {}
+        # 在庫の確認と更新を不可分に行うためのロック。_stock へのアクセスは必ずこのロック下で行う。
+        self._lock = threading.Lock()
 
     def set_stock(self, sku: str, quantity: int) -> None:
-        self._stock[sku] = quantity
+        with self._lock:
+            self._stock[sku] = quantity
 
     def available(self, sku: str) -> int:
-        return self._stock.get(sku, 0)
+        with self._lock:
+            return self._stock.get(sku, 0)
 
     def reserve(self, sku: str, quantity: int) -> None:
-        if self.available(sku) < quantity:
-            raise OutOfStockError(sku)
-        self._stock[sku] = self.available(sku) - quantity
+        self.reserve_many({sku: quantity})
+
+    def reserve_many(self, items: dict[str, int]) -> None:
+        """すべての SKU を引き当てられる場合だけ在庫を減らす。
+
+        数量が 0 以下なら ValueError、1 つでも在庫が足りなければ OutOfStockError を送出し、
+        いずれの場合もどの在庫も変更しない。
+        """
+        # 検証と反映で同じ内容を使うため、呼び出し側の dict をコピーしておく
+        items = dict(items)
+        for sku, quantity in items.items():
+            _validate_quantity(sku, quantity)
+        with self._lock:
+            for sku, quantity in items.items():
+                if self._stock.get(sku, 0) < quantity:
+                    raise OutOfStockError(sku)
+            for sku, quantity in items.items():
+                self._stock[sku] -= quantity
 
     def release(self, sku: str, quantity: int) -> None:
-        self._stock[sku] = self.available(sku) + quantity
+        _validate_quantity(sku, quantity)
+        with self._lock:
+            self._stock[sku] = self._stock.get(sku, 0) + quantity
diff --git a/shoplib/orders.py b/shoplib/orders.py
index d5b6fe1..f322bdc 100644
--- a/shoplib/orders.py
+++ b/shoplib/orders.py
@@ -6,45 +6,48 @@ from shoplib.inventory import Inventory
 from shoplib.models import CartItem, Order, User
 
 
 class OrderRepository:
     def __init__(self) -> None:
         self._orders: dict[int, Order] = {}
         self._ids = count(1)
 
     def next_id(self) -> int:
         return next(self._ids)
 
     def save(self, order: Order) -> None:
         self._orders[order.id] = order
 
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
+        requested: dict[str, int] = {}
         for item in cart.items:
-            self.inventory.reserve(item.product.sku, item.quantity)
+            requested[item.product.sku] = requested.get(item.product.sku, 0) + item.quantity
+        # 全 SKU をまとめて引き当てる。在庫不足なら何も引き当てずに例外になる。
+        self.inventory.reserve_many(requested)
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
 
     def list_orders(self, user: User) -> list[Order]:
         return [o for o in self.repository.all() if o.user_id == user.id]
diff --git a/tests/test_inventory.py b/tests/test_inventory.py
new file mode 100644
index 0000000..1796cd4
--- /dev/null
+++ b/tests/test_inventory.py
@@ -0,0 +1,212 @@
+import sys
+import threading
+from decimal import Decimal
+
+import pytest
+
+from shoplib.cart import Cart
+from shoplib.inventory import Inventory, OutOfStockError
+from shoplib.models import Product, User
+from shoplib.orders import OrderRepository, OrderService
+
+PEN = Product("PEN", "Pen", Decimal("120"))
+NOTE = Product("NOTE", "Notebook", Decimal("300"))
+
+
+@pytest.fixture
+def inventory():
+    inv = Inventory()
+    inv.set_stock("PEN", 10)
+    inv.set_stock("NOTE", 5)
+    return inv
+
+
+@pytest.fixture
+def fast_switching():
+    """スレッド切り替えを頻繁にして、競合状態が表面化しやすくする。"""
+    original = sys.getswitchinterval()
+    sys.setswitchinterval(1e-6)
+    try:
+        yield
+    finally:
+        sys.setswitchinterval(original)
+
+
+def run_concurrently(n_threads, target):
+    """n_threads 個のスレッドで target() を同時に開始し、各スレッドの戻り値を返す。"""
+    barrier = threading.Barrier(n_threads)
+    results = [None] * n_threads
+    errors = []
+
+    def worker(i):
+        barrier.wait()
+        try:
+            results[i] = target()
+        except Exception as exc:
+            errors.append(exc)
+
+    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
+    for t in threads:
+        t.start()
+    for t in threads:
+        t.join()
+    assert errors == []
+    return results
+
+
+def try_reserve(fn):
+    """fn() が成功したら True、OutOfStockError なら False を返す。"""
+    try:
+        fn()
+        return True
+    except OutOfStockError:
+        return False
+
+
+# --- R1: reserve_many は全部成功か、何も変えないか ---------------------------
+
+
+def test_reserve_many_reserves_all_skus(inventory):
+    inventory.reserve_many({"PEN": 3, "NOTE": 5})
+    assert inventory.available("PEN") == 7
+    assert inventory.available("NOTE") == 0
+
+
+def test_reserve_many_out_of_stock_changes_nothing(inventory):
+    with pytest.raises(OutOfStockError):
+        inventory.reserve_many({"PEN": 3, "NOTE": 6})
+    assert inventory.available("PEN") == 10
+    assert inventory.available("NOTE") == 5
+
+
+def test_reserve_many_out_of_stock_first_sku_changes_nothing(inventory):
+    # 不足している SKU が先頭でも、どの在庫も変わらないこと
+    with pytest.raises(OutOfStockError):
+        inventory.reserve_many({"NOTE": 6, "PEN": 3})
+    assert inventory.available("PEN") == 10
+    assert inventory.available("NOTE") == 5
+
+
+def test_reserve_many_unknown_sku_changes_nothing(inventory):
+    with pytest.raises(OutOfStockError):
+        inventory.reserve_many({"PEN": 1, "UNKNOWN": 1})
+    assert inventory.available("PEN") == 10
+    assert inventory.available("UNKNOWN") == 0
+
+
+def test_reserve_many_empty_is_noop(inventory):
+    inventory.reserve_many({})
+    assert inventory.available("PEN") == 10
+    assert inventory.available("NOTE") == 5
+
+
+# --- R2: place_order が一部だけ引き当てた状態を残さない ---------------------
+
+
+def test_place_order_out_of_stock_leaves_stock_untouched(inventory):
+    service = OrderService(inventory, OrderRepository())
+    cart = Cart()
+    cart.add(PEN, 3)  # 足りる
+    cart.add(NOTE, 6)  # 足りない
+    with pytest.raises(OutOfStockError):
+        service.place_order(User(1, "alice"), cart)
+    assert inventory.available("PEN") == 10
+    assert inventory.available("NOTE") == 5
+    assert service.repository.all() == []
+    assert len(cart.items) == 2  # 失敗時はカートも残る
+
+
+def test_place_order_multiple_items_reserves_all(inventory):
+    service = OrderService(inventory, OrderRepository())
+    cart = Cart()
+    cart.add(PEN, 3)
+    cart.add(NOTE, 2)
+    order = service.place_order(User(1, "alice"), cart)
+    assert order.total == Decimal("960")
+    assert inventory.available("PEN") == 7
+    assert inventory.available("NOTE") == 3
+
+
+# --- R4: 0 以下の数量は ValueError -------------------------------------------
+
+
+@pytest.mark.parametrize("quantity", [0, -1])
+def test_reserve_rejects_non_positive_quantity(inventory, quantity):
+    with pytest.raises(ValueError):
+        inventory.reserve("PEN", quantity)
+    assert inventory.available("PEN") == 10
+
+
+@pytest.mark.parametrize("quantity", [0, -1])
+def test_release_rejects_non_positive_quantity(inventory, quantity):
+    with pytest.raises(ValueError):
+        inventory.release("PEN", quantity)
+    assert inventory.available("PEN") == 10
+
+
+@pytest.mark.parametrize("quantity", [0, -1])
+def test_reserve_many_rejects_non_positive_quantity_and_changes_nothing(inventory, quantity):
+    with pytest.raises(ValueError):
+        inventory.reserve_many({"PEN": 1, "NOTE": quantity})
+    assert inventory.available("PEN") == 10
+    assert inventory.available("NOTE") == 5
+
+
+def test_reserve_many_checks_quantities_before_stock(inventory):
+    # 不正な数量は在庫不足より先に検出され、在庫は変わらない
+    with pytest.raises(ValueError):
+        inventory.reserve_many({"NOTE": 100, "PEN": 0})
+    assert inventory.available("PEN") == 10
+    assert inventory.available("NOTE") == 5
+
+
+# --- R3: スレッドセーフ -------------------------------------------------------
+
+
+N_THREADS = 8
+
+
+def reserve_until_out_of_stock(reserve_one):
+    """在庫切れになるまで reserve_one() を繰り返し、成功回数を返す。"""
+    successes = 0
+    while try_reserve(reserve_one):
+        successes += 1
+    return successes
+
+
+@pytest.mark.usefixtures("fast_switching")
+def test_concurrent_reserve_never_oversells():
+    inv = Inventory()
+    inv.set_stock("PEN", 20_000)
+    results = run_concurrently(N_THREADS, lambda: reserve_until_out_of_stock(lambda: inv.reserve("PEN", 1)))
+    # 成功回数の合計が初期在庫ちょうどなら、過剰な引き当ても更新の取りこぼしもない
+    assert sum(results) == 20_000
+    assert inv.available("PEN") == 0
+
+
+@pytest.mark.usefixtures("fast_switching")
+def test_concurrent_reserve_many_never_oversells():
+    inv = Inventory()
+    inv.set_stock("PEN", 30_000)
+    inv.set_stock("NOTE", 20_000)
+    results = run_concurrently(
+        N_THREADS, lambda: reserve_until_out_of_stock(lambda: inv.reserve_many({"PEN": 1, "NOTE": 1}))
+    )
+    # NOTE がボトルネック。成功した数だけ両方の在庫が減り、マイナスにはならない
+    assert sum(results) == 20_000
+    assert inv.available("NOTE") == 0
+    assert inv.available("PEN") == 10_000
+
+
+@pytest.mark.usefixtures("fast_switching")
+def test_concurrent_reserve_and_release_conserve_stock():
+    inv = Inventory()
+    inv.set_stock("PEN", 100)
+
+    def reserve_then_release():
+        for _ in range(5_000):
+            inv.reserve("PEN", 1)
+            inv.release("PEN", 1)
+
+    run_concurrently(N_THREADS, reserve_then_release)
+    assert inv.available("PEN") == 100
```
