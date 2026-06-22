"""K 線圖資料：OHLCV + 均張 + 移動平均 + RSI + KD。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

from data_sources import yahoo_source
from indicators.avg_lot import compute_avg_lot
from indicators.chart_cache import chart_cache
from indicators.rsi import compute_rsi
from indicators.stochastic import compute_fast_kd

logger = logging.getLogger(__name__)

# 最長回看：MA20(20)、RSI12(13)、KD(5+2) → 取 60 交易日緩衝
INDICATOR_BUFFER_TRADING_DAYS = 60
BUFFER_CALENDAR_DAYS = 90


def _rolling_mean(values: list[float | None], window: int) -> list[float | None]:
    series = pd.Series(values, dtype="float64")
    rolled = series.rolling(window=window, min_periods=window).mean()
    return [
        round(float(v), 2) if pd.notna(v) else None for v in rolled.tolist()
    ]


def _buffer_start(start_date: str) -> str:
    dt = datetime.strptime(start_date, "%Y-%m-%d") - timedelta(
        days=BUFFER_CALENDAR_DAYS
    )
    return dt.strftime("%Y-%m-%d")


def _build_rows(
    ohlcv_df: pd.DataFrame, avg_lot_map: dict[str, float | None]
) -> list[dict]:
    rows: list[dict] = []
    for date_idx, row in ohlcv_df.iterrows():
        date_key = date_idx.strftime("%Y-%m-%d")
        raw_avg = avg_lot_map.get(date_key)
        avg_lot = round(raw_avg, 2) if raw_avg is not None else None
        rows.append(
            {
                "date": date_key,
                "open": round(float(row["Open"]), 2),
                "high": round(float(row["High"]), 2),
                "low": round(float(row["Low"]), 2),
                "close": round(float(row["Close"]), 2),
                "volume": int(row["Volume"]),
                "avg_lot": avg_lot,
            }
        )
    rows.sort(key=lambda r: r["date"])
    return rows


def _forward_fill_avg_lot(rows: list[dict]) -> None:
    """成交筆數為零或缺資料時，以前一交易日均張補值，避免折線斷點。"""
    last: float | None = None
    for row in rows:
        value = row.get("avg_lot")
        if value is not None:
            last = value
        elif last is not None:
            row["avg_lot"] = last


def _compute_indicators(rows: list[dict]) -> list[dict]:
    closes = [r["close"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]

    ma5 = _rolling_mean(closes, 5)
    ma10 = _rolling_mean(closes, 10)
    ma20 = _rolling_mean(closes, 20)
    rsi6 = compute_rsi(closes, 6)
    rsi12 = compute_rsi(closes, 12)
    fk, fd = compute_fast_kd(highs, lows, closes, k_period=5, d_period=2)

    for i, row in enumerate(rows):
        row["ma5"] = ma5[i]
        row["ma10"] = ma10[i]
        row["ma20"] = ma20[i]
        row["rsi6"] = rsi6[i]
        row["rsi12"] = rsi12[i]
        row["fk"] = fk[i]
        row["fd"] = fd[i]

    return rows


def _slice_display_range(
    rows: list[dict], start_date: str, end_date: str
) -> list[dict]:
    return [r for r in rows if start_date <= r["date"] <= end_date]


def compute_chart_data(
    symbol: str, start_date: str, end_date: str
) -> list[dict]:
    fetch_start = _buffer_start(start_date)

    cached = chart_cache.get(symbol, fetch_start, end_date)
    if cached is not None:
        full_rows = cached
    else:
        logger.info(
            "抓取含緩衝資料 symbol=%s fetch=%s~%s (顯示 %s~%s)",
            symbol,
            fetch_start,
            end_date,
            start_date,
            end_date,
        )
        ohlcv_df = yahoo_source.fetch_ohlcv(symbol, fetch_start, end_date)
        avg_lot_points = compute_avg_lot(symbol, fetch_start, end_date)
        avg_lot_map = {p.date: p.value for p in avg_lot_points}

        if ohlcv_df.empty:
            return []

        full_rows = _build_rows(ohlcv_df, avg_lot_map)
        _forward_fill_avg_lot(full_rows)
        full_rows = _compute_indicators(full_rows)
        chart_cache.put(symbol, fetch_start, end_date, full_rows)

    display_rows = _slice_display_range(full_rows, start_date, end_date)
    logger.debug(
        "回傳顯示區間 symbol=%s %s~%s (%d 筆)",
        symbol,
        start_date,
        end_date,
        len(display_rows),
    )
    return display_rows
