"""均張 (NumT/ATS) 計算：成交量(張) ÷ 成交筆數。"""

import logging

import pandas as pd
import requests

from models.schemas import IndicatorPoint
from data_sources import twse_source, yahoo_source

logger = logging.getLogger(__name__)

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _parse_number(value: str | int | float | None) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "")
    if not text or text in ("--", "X", "-"):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _fetch_trade_counts(
    symbol: str, start_date: str, end_date: str
) -> dict[str, float]:
    """
    成交筆數來自 FinMind（yfinance 無此欄位）。
    回傳 {YYYY-MM-DD: trade_count}。
    """
    code = symbol.split(".")[0]
    try:
        resp = requests.get(
            FINMIND_URL,
            params={
                "dataset": "TaiwanStockPrice",
                "data_id": code,
                "start_date": start_date,
                "end_date": end_date,
            },
            headers=_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        logger.exception("FinMind trade_count fetch failed for %s", symbol)
        return {}

    if payload.get("status") != 200:
        logger.warning(
            "FinMind 回應異常 %s: %s", symbol, payload.get("msg")
        )
        return {}

    counts: dict[str, float] = {}
    for item in payload.get("data") or []:
        date_iso = str(item.get("date", ""))[:10]
        if not date_iso:
            continue
        counts[date_iso] = _parse_number(item.get("Trading_turnover", 0))
    return counts


def compute_avg_lot(
    symbol: str, start_date: str, end_date: str
) -> list[IndicatorPoint]:
    """
    計算均張指標。
    均張 = (成交股數 / 1000) / 成交筆數
    """
    trading_dates = set(
        yahoo_source.get_trading_dates(symbol, start_date, end_date)
    )

    price_df = twse_source.fetch_daily_trades(symbol, start_date, end_date)
    if price_df.empty and not trading_dates:
        return []

    trade_counts = _fetch_trade_counts(symbol, start_date, end_date)
    points: list[IndicatorPoint] = []

    if not price_df.empty:
        for date_idx, row in price_df.iterrows():
            date_key = (
                date_idx.strftime("%Y-%m-%d")
                if hasattr(date_idx, "strftime")
                else str(date_idx)[:10]
            )
            volume_shares = float(row.get("volume_shares", row.get("volume", 0)))
            volume_lots = volume_shares / 1000.0
            trade_count = trade_counts.get(date_key, 0.0)
            if trade_count <= 0:
                value = None
            else:
                value = round(float(volume_lots / trade_count), 2)
            points.append(IndicatorPoint(date=date_key, value=value))
    else:
        return []

    if trading_dates:
        points = [p for p in points if p.date in trading_dates]

    points.sort(key=lambda p: p.date)

    last_value: float | None = None
    for point in points:
        if point.value is not None:
            last_value = point.value
        elif last_value is not None:
            point.value = last_value

    values = [p.value for p in points]
    ma_series = pd.Series(values, dtype="float64").rolling(3, min_periods=3).mean()
    for i, point in enumerate(points):
        ma = ma_series.iloc[i]
        point.ma_value = round(float(ma), 4) if pd.notna(ma) else None

    return points
