import threading

import pytest

from shoplib.ratelimit import RateLimiter


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_capacity_then_block():
    clock = FakeClock()
    rl = RateLimiter(3, 1.0, clock=clock)
    assert [rl.allow("u") for _ in range(4)] == [True, True, True, False]


def test_refill_and_cap():
    clock = FakeClock()
    rl = RateLimiter(2, 0.5, clock=clock)
    assert rl.allow("u") and rl.allow("u") and not rl.allow("u")
    clock.now += 1.0  # +0.5 token
    assert not rl.allow("u")
    clock.now += 1.0  # +1.0 token total
    assert rl.allow("u")
    clock.now += 100  # capped at 2
    assert [rl.allow("u") for _ in range(3)] == [True, True, False]


def test_keys_independent():
    rl = RateLimiter(1, 1.0, clock=FakeClock())
    assert rl.allow("a") and rl.allow("b")
    assert not rl.allow("a")


@pytest.mark.parametrize("cap, refill", [(0, 1.0), (1, 0), (1, -1)])
def test_invalid(cap, refill):
    with pytest.raises(ValueError):
        RateLimiter(cap, refill)


def test_thread_safe():
    rl = RateLimiter(10, 0.0001, clock=FakeClock())
    allowed = []
    barrier = threading.Barrier(40)

    def worker():
        barrier.wait()
        if rl.allow("u"):
            allowed.append(1)

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(allowed) == 10
