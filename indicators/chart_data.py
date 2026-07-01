"""K 線圖資料：OHLCV + 均張 + 移動平均 + RSI + KD。"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

import pandas as pd

from data_sources import yahoo_source
from indicators.avg_lot import compute_avg_lot, fetch_trade_counts
from indicators.chart_cache import chart_cache
from indicators.rsi import compute_rsi
from indicators.stochastic import compute_fast_kd

logger = logging.getLogger(__name__)

# 最長回看：MA240(240)、RSI12、KD → 取約 380 曆日緩衝
INDICATOR_BUFFER_TRADING_DAYS = 260
BUFFER_CALENDAR_DAYS = 380


def _finite_price(value: object) -> float | None:
    """轉成有限浮點數；NaN/Infinity 回傳 None（不可序列化為標準 JSON）。"""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return round(number, 2)


def _drop_invalid_rows(rows: list[dict]) -> list[dict]:
    """移除 OHLC 含 NaN 或缺必要欄位的列（常見於 yfinance 未來日期）。"""
    cleaned: list[dict] = []
    for row in rows:
        if all(
            _finite_price(row.get(key)) is not None
            for key in ("open", "high", "low", "close")
        ):
            cleaned.append(row)
        else:
            logger.debug("略過無效 OHLC date=%s", row.get("date"))
    return cleaned


def _clip_end_date(end_date: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    return min(end_date, today)


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


def _align_maps_to_ohlcv_dates(
    ohlcv_df: pd.DataFrame,
    avg_lot_map: dict[str, float | None],
    trade_count_map: dict[str, float],
) -> tuple[dict[str, float | None], dict[str, float]]:
    """以 K 線 OHLCV 的交易日為基準，對齊均張與成交筆數。"""
    trading_dates = [
        date_idx.strftime("%Y-%m-%d")
        for date_idx in ohlcv_df.index
        if hasattr(date_idx, "strftime")
    ]
    aligned_avg = {d: avg_lot_map.get(d) for d in trading_dates}
    aligned_trade = {d: trade_count_map.get(d, 0.0) for d in trading_dates}
    return aligned_avg, aligned_trade


def _build_rows(
    ohlcv_df: pd.DataFrame,
    avg_lot_map: dict[str, float | None],
    trade_count_map: dict[str, float],
) -> list[dict]:
    rows: list[dict] = []
    for date_idx, row in ohlcv_df.iterrows():
        date_key = date_idx.strftime("%Y-%m-%d")
        open_p = _finite_price(row["Open"])
        high_p = _finite_price(row["High"])
        low_p = _finite_price(row["Low"])
        close_p = _finite_price(row["Close"])
        if open_p is None or high_p is None or low_p is None or close_p is None:
            logger.debug("略過無效 OHLC date=%s", date_key)
            continue

        volume_raw = row["Volume"]
        if pd.isna(volume_raw):
            logger.debug("略過無成交量 date=%s", date_key)
            continue

        raw_avg = avg_lot_map.get(date_key)
        avg_lot = round(raw_avg, 2) if raw_avg is not None else None
        trade_count = int(trade_count_map.get(date_key, 0))
        rows.append(
            {
                "date": date_key,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p,
                "volume": int(volume_raw),
                "trade_count": trade_count,
                "avg_lot": avg_lot,
            }
        )
    rows.sort(key=lambda r: r["date"])
    return rows


def _ensure_trade_counts(
    rows: list[dict], symbol: str, fetch_start: str, end_date: str
) -> None:
    if not rows or "trade_count" in rows[0]:
        return
    trade_count_map = fetch_trade_counts(symbol, fetch_start, end_date)
    for row in rows:
        row["trade_count"] = int(trade_count_map.get(row["date"], 0))


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
    ma60 = _rolling_mean(closes, 60)
    ma120 = _rolling_mean(closes, 120)
    ma240 = _rolling_mean(closes, 240)
    rsi6 = compute_rsi(closes, 6)
    rsi12 = compute_rsi(closes, 12)
    fk, fd = compute_fast_kd(highs, lows, closes, k_period=5, d_period=2)

    for i, row in enumerate(rows):
        row["ma5"] = ma5[i]
        row["ma10"] = ma10[i]
        row["ma20"] = ma20[i]
        row["ma60"] = ma60[i]
        row["ma120"] = ma120[i]
        row["ma240"] = ma240[i]
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
    end_date = _clip_end_date(end_date)
    fetch_start = _buffer_start(start_date)

    cached = chart_cache.get(symbol, fetch_start, end_date)
    if cached is not None:
        full_rows = _drop_invalid_rows(cached)
        _ensure_trade_counts(full_rows, symbol, fetch_start, end_date)
        if full_rows and "ma240" not in full_rows[0]:
            full_rows = _compute_indicators(full_rows)
            chart_cache.put(symbol, fetch_start, end_date, full_rows)
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
        trade_count_map = fetch_trade_counts(symbol, fetch_start, end_date)
        avg_lot_map, trade_count_map = _align_maps_to_ohlcv_dates(
            ohlcv_df, avg_lot_map, trade_count_map
        )

        if ohlcv_df.empty:
            return []

        full_rows = _build_rows(ohlcv_df, avg_lot_map, trade_count_map)
        _forward_fill_avg_lot(full_rows)
        full_rows = _compute_indicators(full_rows)
        full_rows = _drop_invalid_rows(full_rows)
        chart_cache.put(symbol, fetch_start, end_date, full_rows)

    display_rows = _drop_invalid_rows(
        _slice_display_range(full_rows, start_date, end_date)
    )
    logger.debug(
        "回傳顯示區間 symbol=%s %s~%s (%d 筆)",
        symbol,
        start_date,
        end_date,
        len(display_rows),
    )
    return display_rows
