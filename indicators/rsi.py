"""RSI 指標計算。"""

import pandas as pd


def compute_rsi(closes: list[float], period: int) -> list[float | None]:
    if len(closes) < period + 1:
        return [None] * len(closes)

    series = pd.Series(closes, dtype="float64")
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    result: list[float | None] = []
    for v in rsi.tolist():
        if pd.isna(v):
            result.append(None)
        else:
            result.append(round(float(v), 2))
    return result
