"""MACD 指標：DIF(6,9) / MACD(3) / OSC，與凱基證券參數一致。"""

from __future__ import annotations

import pandas as pd

MACD_FAST = 6
MACD_SLOW = 9
MACD_SIGNAL = 3


def _macd_dataframe(close_s: pd.Series) -> pd.DataFrame:
    """與 pandas_ta.macd(fast=6, slow=9, signal=3) 相同算法（EMA span, adjust=False）。"""
    fast_ema = close_s.ewm(span=MACD_FAST, adjust=False).mean()
    slow_ema = close_s.ewm(span=MACD_SLOW, adjust=False).mean()
    dif = fast_ema - slow_ema
    signal = dif.ewm(span=MACD_SIGNAL, adjust=False).mean()
    osc = dif - signal
    return pd.DataFrame({"dif": dif, "macd": signal, "osc": osc})


def compute_macd(
    closes: list[float],
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """
    計算 MACD。
    DIF = EMA(6) - EMA(9)
    MACD 信號線 = DIF 的 EMA(3)
    OSC = DIF - MACD 信號線
    """
    n = len(closes)
    if n == 0:
        return [], [], []

    macd_df = _macd_dataframe(pd.Series(closes, dtype="float64"))
    if macd_df.empty:
        return [None] * n, [None] * n, [None] * n

    difs: list[float | None] = []
    macds: list[float | None] = []
    oscs: list[float | None] = []

    for dif_v, sig_v, hist_v in zip(
        macd_df["dif"].tolist(),
        macd_df["macd"].tolist(),
        macd_df["osc"].tolist(),
    ):
        difs.append(round(float(dif_v), 2) if pd.notna(dif_v) else None)
        macds.append(round(float(sig_v), 2) if pd.notna(sig_v) else None)
        oscs.append(round(float(hist_v), 2) if pd.notna(hist_v) else None)

    return difs, macds, oscs
