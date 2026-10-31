# r-cabb54ad

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

## 概要
ユーザーごとの API 呼び出しを制限するトークンバケット方式のレートリミッター `shoplib/ratelimit.py` を追加。

## 変更内容
- `RateLimiter(capacity, refill_per_sec, clock=time.monotonic)` と `allow(key) -> bool`
- キーごとに満杯から開始し、許可のたびに 1 消費。経過時間 × refill_per_sec で連続補充（上限 capacity）
- `capacity < 1`、`refill_per_sec <= 0`（NaN/無限大も含む）は `ValueError`
- 単一の `threading.Lock` で状態の読み書きを保護（スレッドセーフ）
- 時刻は注入可能な `clock` から取得。時計が逆行しても補充は負にならない（経過時間を 0 に丸める）
- テスト `tests/test_ratelimit.py` を追加（R1〜R6）

## 注意点
- キーごとの状態は削除しないため、キー数が無制限に増える用途では将来的に期限切れ掃除が必要。
- ロックは全キーで共有（処理が軽量なため単純さを優先）。

## 確認方法
```
/home/tniita/ghcp-foundry/.venv/bin/python -m pytest -q
/home/tniita/ghcp-foundry/.venv/bin/ruff check .
```

## 実装

### CI 結果

```
Build: success
Tests (repository): 15 passed in 0.02s
Hidden tests: passed
Lint (ruff): 0 issues
CodeQL: not run
```

### 差分

```diff
diff --git a/shoplib/ratelimit.py b/shoplib/ratelimit.py
new file mode 100644
index 0000000..4b0e3b0
--- /dev/null
+++ b/shoplib/ratelimit.py
@@ -0,0 +1,41 @@
+import math
+import threading
+import time
+from collections.abc import Callable
+
+
+class RateLimiter:
+    """キーごとのトークンバケット方式レートリミッター。
+
+    各キーは capacity 個のトークンが入った満杯のバケットから始まり、
+    トークンは refill_per_sec の速度で連続的に補充される（上限 capacity）。
+    スレッドセーフ。
+    """
+
+    def __init__(self, capacity: int, refill_per_sec: float, clock: Callable[[], float] = time.monotonic) -> None:
+        if capacity < 1:
+            raise ValueError("capacity must be >= 1")
+        if not refill_per_sec > 0 or math.isinf(refill_per_sec):
+            raise ValueError("refill_per_sec must be a positive finite number")
+        self._capacity = capacity
+        self._refill_per_sec = refill_per_sec
+        self._clock = clock
+        self._lock = threading.Lock()
+        # key -> (tokens, last_update)
+        self._buckets: dict[str, tuple[float, float]] = {}
+
+    def allow(self, key: str) -> bool:
+        """トークンを 1 つ消費できれば True、できなければ False を返す。"""
+        with self._lock:
+            now = self._clock()
+            if key in self._buckets:
+                tokens, last = self._buckets[key]
+                elapsed = max(0.0, now - last)  # 時計の逆行は無視する
+                tokens = min(float(self._capacity), tokens + elapsed * self._refill_per_sec)
+            else:
+                tokens = float(self._capacity)
+            allowed = tokens >= 1
+            if allowed:
+                tokens -= 1
+            self._buckets[key] = (tokens, now)
+            return allowed
diff --git a/tests/test_ratelimit.py b/tests/test_ratelimit.py
new file mode 100644
index 0000000..6c92892
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
+    def __init__(self) -> None:
+        self.now = 1000.0
+
+    def __call__(self) -> float:
+        return self.now
+
+
+def test_starts_full_and_consumes():
+    rl = RateLimiter(3, 1, clock=FakeClock())
+    assert [rl.allow("a") for _ in range(4)] == [True, True, True, False]
+
+
+def test_continuous_refill():
+    clock = FakeClock()
+    rl = RateLimiter(2, 2.0, clock=clock)
+    assert rl.allow("a") and rl.allow("a") and not rl.allow("a")
+    clock.now += 0.25  # 0.5 トークン
+    assert not rl.allow("a")
+    clock.now += 0.25  # 合計 1 トークン
+    assert rl.allow("a")
+    assert not rl.allow("a")
+
+
+def test_refill_capped_at_capacity():
+    clock = FakeClock()
+    rl = RateLimiter(2, 1.0, clock=clock)
+    assert rl.allow("a")
+    clock.now += 1000
+    assert [rl.allow("a") for _ in range(3)] == [True, True, False]
+
+
+def test_keys_independent():
+    rl = RateLimiter(1, 1, clock=FakeClock())
+    assert rl.allow("a")
+    assert not rl.allow("a")
+    assert rl.allow("b")
+
+
+def test_clock_going_backwards_does_not_add_tokens():
+    clock = FakeClock()
+    rl = RateLimiter(1, 1, clock=clock)
+    assert rl.allow("a")
+    clock.now -= 100
+    assert not rl.allow("a")
+
+
+@pytest.mark.parametrize("cap,rate", [(0, 1), (-1, 1), (1, 0), (1, -0.5), (1, float("nan"))])
+def test_invalid_args(cap, rate):
+    with pytest.raises(ValueError):
+        RateLimiter(cap, rate)
+
+
+def test_default_clock_works():
+    rl = RateLimiter(1, 0.001)
+    assert rl.allow("a")
+    assert not rl.allow("a")
+
+
+def test_thread_safety():
+    capacity = 50
+    # 時間が進まないので補充は起きない
+    rl = RateLimiter(capacity, 1.0, clock=FakeClock())
+    results = []
+    barrier = threading.Barrier(16)
+
+    def worker():
+        barrier.wait()
+        local = [rl.allow("k") for _ in range(100)]
+        results.append(sum(local))
+
+    threads = [threading.Thread(target=worker) for _ in range(16)]
+    for t in threads:
+        t.start()
+    for t in threads:
+        t.join()
+    assert sum(results) == capacity
```
