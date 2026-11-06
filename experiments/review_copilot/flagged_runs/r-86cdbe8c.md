# r-86cdbe8c

## タスク

ユーザーごとの API 呼び出しを制限するレートリミッターを追加する。

- 新規モジュール `shoplib/ratelimit.py`
- `RateLimiter(capacity: int, refill_per_sec: float, clock=time.monotonic)`
- `allow(key: str) -> bool`

R1: トークンバケット方式。キーごとに満杯（capacity 個）の状態から始まり、allow が True を返すたびにトークンを 1 消費する。トークンが 1 未満なら False を返す
R2: トークンは経過時間 × refill_per_sec だけ連続的に補充され、capacity を超えない
R3: キーごとに独立して管理する
R4: capacity が 1 未満、または refill_per_sec が 0 以下なら ValueError を送出する
R5: 複数スレッドから同時に呼ばれても、許可回数が capacity を超えない（スレッドセーフ）
R6: 時刻は clock から取得し、テストで差し替えられるようにする
完了条件: 既存テストと追加したテストがすべて通ること

## PR 説明

# ユーザーごとの API レートリミッターを追加

## 変更内容
- 新規 `shoplib/ratelimit.py`: `RateLimiter(capacity, refill_per_sec, clock=time.monotonic)` と `allow(key) -> bool`
  - トークンバケット方式。キーごとに満杯（capacity）から開始し、許可のたびに 1 消費、1 未満なら `False`（R1）
  - 経過時間 × `refill_per_sec` で連続補充し、capacity で頭打ち（R2）
  - キーごとに独立したバケットを `dict` で管理（R3）
  - `capacity < 1` / `refill_per_sec <= 0`（NaN 含む）で `ValueError`（R4）
  - 補充・判定・消費を 1 つの `threading.Lock` 内で行い、スレッドセーフ（R5）
  - 時刻は注入された `clock` から取得（R6、ADR-001 に沿ってテストで差し替え可能）
  - 時計が巻き戻った場合は経過時間を 0 とみなす
- 新規 `tests/test_ratelimit.py`: R1〜R6 を偽クロックで決定的に検証。16 スレッドの同時呼び出しで許可回数が capacity と一致することも確認
- 標準ライブラリのみ使用。`shoplib/__init__.py` の公開 API は変更なし

## 確認方法
```
pytest -q        # 14 passed
ruff check .     # All checks passed!
```

## 補足 / 今後の検討
- バケットは削除されないため、キー数に比例してメモリを使う。キーが多い場合は、満杯になったバケットを削除する仕組みを今後検討する

## 実装

### CI 結果

```
Build: success
Tests (repository): 14 passed in 0.02s
Hidden tests: passed
Lint (ruff): 0 issues
CodeQL: not run
```

### 関連する既存コード

なし

### プロジェクトの Skill（評価時の入力）

## adr.md

# ADR（設計判断の記録）

## ADR-001: 外部 I/O をドメインロジックから分離する

- 決定: ドメインロジックはファイル・ネットワーク・時刻に直接アクセスしない。時刻は引数（`now: datetime`）で受け取る
- 理由: テストを決定的にするため

## architecture.md

# アーキテクチャ方針・レイヤー構成

- レイヤー: `models`（データ構造）→ `pricing` / `inventory`（ドメインロジック）→ `orders`（ユースケース）
- 依存は上記の矢印の向きだけに許す。`models` から他のモジュールを import しない
- 表示用の整形は `formatting` に集め、ドメインロジックに文字列整形を混ぜない

## coding_rules.md

# コーディング規約

- 命名: 関数・変数は snake_case、クラスは PascalCase、定数は UPPER_SNAKE_CASE
- 金額は `Decimal` で扱い、`float` を使わない
- 公開関数には型ヒントを付ける
- 例外は `ValueError` などの標準例外か、`errors.py` に定義したドメイン例外を使う。`Exception` を直接 raise しない
- ログは `logging.getLogger(__name__)` を使う。`print` を残さない

## constraints.md

# プロジェクト固有の制約・今後の方向性

- Python 3.12 以上で動くこと
- 公開 API（`shoplib/__init__.py` で export しているもの）の引数と戻り値は後方互換を保つ
- 今後 `inventory` を外部サービスに置き換える予定のため、`inventory` への直接依存を増やさない

## glossary.md

# 業務用語集

| 用語 | コード上の表現 | 補足 |
|---|---|---|
| 注文 | `Order` | 確定前は `Cart` と呼び、`Order` と混同しない |
| 在庫引当 | `reserve()` | 「確保」「ロック」などの別名を使わない |
| 税込価格 | `price_incl_tax` | 税抜は `price_excl_tax` |

## libraries.md

# 使用推奨・非推奨のライブラリ・API

- 推奨: 標準ライブラリを優先する。日時は `datetime`（タイムゾーン付き）
- 非推奨: 新しい外部依存の追加は要件で求められた場合だけにする
- 禁止: `pickle` で外部入力を読み込まない。`eval` / `exec` を使わない

### 機械的な指標の差分

```
（CI が PR の共通祖先との比較で機械的に計算した指標の差分。静的解析による参考値）
- 循環的複雑度（McCabe）の合計: ΔV = +6（関数ごとの最大値 +0）
  - shoplib/ratelimit.py:21 RateLimiter.__init__: 新規 → 3（+3）
  - shoplib/ratelimit.py:33 RateLimiter.allow: 新規 → 3（+3）
- 重複コード: 重複行 +0（重複率 +0.0pt）
- 結合度: モジュール間の依存 +0 本、不安定度 I の平均 -0.027
- 変更の波及しやすさ: ΔPC = -0.037
- モジュール性: ΔQ = +0.000
```

### 差分

```diff
diff --git a/shoplib/ratelimit.py b/shoplib/ratelimit.py
new file mode 100644
index 0000000..717d24c
--- /dev/null
+++ b/shoplib/ratelimit.py
@@ -0,0 +1,49 @@
+"""ユーザーごとの API 呼び出しを制限するトークンバケット方式のレートリミッター。"""
+
+import threading
+import time
+from collections.abc import Callable
+from dataclasses import dataclass
+
+
+@dataclass
+class _Bucket:
+    tokens: float
+    updated_at: float
+
+
+class RateLimiter:
+    """キーごとに独立したトークンバケットで呼び出しを制限する。
+
+    時刻は ``clock`` から取得するため、テストでは任意の関数に差し替えられる。
+    """
+
+    def __init__(self, capacity: int, refill_per_sec: float, clock: Callable[[], float] = time.monotonic) -> None:
+        if capacity < 1:
+            raise ValueError(f"capacity must be >= 1, got {capacity!r}")
+        # NaN も弾くため否定形で判定する
+        if not refill_per_sec > 0:
+            raise ValueError(f"refill_per_sec must be > 0, got {refill_per_sec!r}")
+        self._capacity = capacity
+        self._refill_per_sec = refill_per_sec
+        self._clock = clock
+        self._buckets: dict[str, _Bucket] = {}
+        self._lock = threading.Lock()
+
+    def allow(self, key: str) -> bool:
+        """``key`` の呼び出しを許可するなら True を返し、トークンを 1 消費する。"""
+        with self._lock:
+            now = self._clock()
+            bucket = self._buckets.get(key)
+            if bucket is None:
+                bucket = _Bucket(tokens=float(self._capacity), updated_at=now)
+                self._buckets[key] = bucket
+            else:
+                # 時計が巻き戻った場合に補充量が負にならないようにする
+                elapsed = max(0.0, now - bucket.updated_at)
+                bucket.tokens = min(float(self._capacity), bucket.tokens + elapsed * self._refill_per_sec)
+                bucket.updated_at = now
+            if bucket.tokens < 1:
+                return False
+            bucket.tokens -= 1
+            return True
diff --git a/tests/test_ratelimit.py b/tests/test_ratelimit.py
new file mode 100644
index 0000000..ae8f2e0
--- /dev/null
+++ b/tests/test_ratelimit.py
@@ -0,0 +1,84 @@
+import threading
+
+import pytest
+
+from shoplib.ratelimit import RateLimiter
+
+
+class FakeClock:
+    def __init__(self, now: float = 0.0) -> None:
+        self.now = now
+
+    def __call__(self) -> float:
+        return self.now
+
+
+def test_starts_full_and_rejects_when_empty():
+    limiter = RateLimiter(3, 1.0, clock=FakeClock())
+    assert [limiter.allow("u") for _ in range(4)] == [True, True, True, False]
+
+
+def test_refills_continuously_over_time():
+    clock = FakeClock()
+    limiter = RateLimiter(2, 2.0, clock=clock)
+    assert limiter.allow("u") and limiter.allow("u")
+    assert not limiter.allow("u")
+    clock.now = 0.25  # 0.5 トークン補充: まだ不足
+    assert not limiter.allow("u")
+    clock.now = 0.5  # 合計 1 トークン
+    assert limiter.allow("u")
+    assert not limiter.allow("u")
+
+
+def test_refill_does_not_exceed_capacity():
+    clock = FakeClock()
+    limiter = RateLimiter(2, 10.0, clock=clock)
+    limiter.allow("u")
+    clock.now = 1000.0
+    assert [limiter.allow("u") for _ in range(3)] == [True, True, False]
+
+
+def test_clock_going_backwards_does_not_remove_tokens():
+    clock = FakeClock(10.0)
+    limiter = RateLimiter(2, 1.0, clock=clock)
+    limiter.allow("u")
+    clock.now = 5.0
+    assert limiter.allow("u")
+    assert not limiter.allow("u")
+
+
+def test_keys_are_independent():
+    limiter = RateLimiter(1, 1.0, clock=FakeClock())
+    assert limiter.allow("alice")
+    assert not limiter.allow("alice")
+    assert limiter.allow("bob")
+
+
+@pytest.mark.parametrize(
+    ("capacity", "refill"),
+    [(0, 1.0), (-1, 1.0), (1, 0.0), (1, -0.5), (1, float("nan"))],
+)
+def test_invalid_arguments_raise_value_error(capacity, refill):
+    with pytest.raises(ValueError):
+        RateLimiter(capacity, refill)
+
+
+def test_thread_safety_never_exceeds_capacity():
+    capacity = 50
+    limiter = RateLimiter(capacity, 1.0, clock=FakeClock())
+    results: list[bool] = []
+    results_lock = threading.Lock()
+    barrier = threading.Barrier(16)
+
+    def worker() -> None:
+        barrier.wait()
+        local = [limiter.allow("u") for _ in range(20)]
+        with results_lock:
+            results.extend(local)
+
+    threads = [threading.Thread(target=worker) for _ in range(16)]
+    for t in threads:
+        t.start()
+    for t in threads:
+        t.join()
+    assert sum(results) == capacity
```
