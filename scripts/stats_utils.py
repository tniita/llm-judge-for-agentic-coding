from math import comb


def sign_test(wins: int, losses: int) -> float:
    """両側の符号検定（引き分けは除外）の p 値。"""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    p = sum(comb(n, i) for i in range(k + 1)) / 2**n * 2
    return min(1.0, p)


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def fmt(value, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"
