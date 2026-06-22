"""快速 KD 指標：FK(5) / FD(5,2)。"""

import pandas as pd


def compute_fast_kd(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    k_period: int = 5,
    d_period: int = 2,
) -> tuple[list[float | None], list[float | None]]:
    n = len(closes)
    if n < k_period:
        return [None] * n, [None] * n

    high_s = pd.Series(highs, dtype="float64")
    low_s = pd.Series(lows, dtype="float64")
    close_s = pd.Series(closes, dtype="float64")

    lowest = low_s.rolling(window=k_period, min_periods=k_period).min()
    highest = high_s.rolling(window=k_period, min_periods=k_period).max()
    denom = highest - lowest
    raw_k = ((close_s - lowest) / denom.replace(0, float("nan"))) * 100

    fk = raw_k.rolling(window=1, min_periods=1).mean()
    fd = fk.rolling(window=d_period, min_periods=d_period).mean()

    fk_list: list[float | None] = []
    fd_list: list[float | None] = []
    for fk_v, fd_v in zip(fk.tolist(), fd.tolist()):
        fk_list.append(round(float(fk_v), 2) if pd.notna(fk_v) else None)
        fd_list.append(round(float(fd_v), 2) if pd.notna(fd_v) else None)

    return fk_list, fd_list
