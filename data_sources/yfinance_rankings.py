"""yfinance 備援：量大排行 / 漲停（高流動性標的 + 快取）。"""

from __future__ import annotations

import logging
import math
import threading
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

RANKING_CACHE_TTL = timedelta(minutes=15)
TWSE_BLOCK_TTL = timedelta(minutes=30)
# 解析邏輯變更時遞增，避免沿用舊快取
RANKING_CACHE_VERSION = 3

PRIORITY_LIQUID_CODES: tuple[str, ...] = (
    "2330", "2317", "2454", "2303", "2382", "2412", "2881", "2882", "2891",
    "2886", "2884", "2308", "3711", "3008", "2357", "2324", "3034", "2345",
    "6669", "2603", "2609", "2615", "2887", "2892", "2880", "2885", "2890",
    "5880", "2883", "2888", "1101", "2002", "1301", "1303", "1326", "1402",
    "2207", "1216", "2912", "2105", "1590", "2408", "2409", "2474", "3231",
    "6415", "8046", "2379", "2395", "3443", "3661", "6770", "2301", "2356",
    "2376", "2383", "2449", "3037", "3045", "3406", "3529", "4938", "4958",
    "5871", "5876", "6505", "9910",
)

_ranking_cache: list[dict[str, Any]] | None = None
_ranking_cached_at: datetime | None = None
_ranking_cache_version: int | None = None
_ranking_fetch_lock = threading.Lock()
_twse_blocked_until: datetime | None = None


def is_twse_blocked(status: int, text: str) -> bool:
    if status in (307, 403, 451):
        return True
    sample = (text or "")[:800]
    if "SECURITY REASONS" in sample.upper():
        return True
    return "安全性考量" in sample


def should_skip_twse() -> bool:
    return (
        _twse_blocked_until is not None and datetime.now() < _twse_blocked_until
    )


def mark_twse_blocked() -> None:
    global _twse_blocked_until
    _twse_blocked_until = datetime.now() + TWSE_BLOCK_TTL
    logger.info(
        "TWSE 不可存取，%d 分鐘內排行改走 yfinance",
        int(TWSE_BLOCK_TTL.total_seconds() // 60),
    )


def _ranking_universe() -> list[dict[str, str]]:
    from data_sources.stock_list import get_stock_list

    by_code = {s["code"]: s for s in get_stock_list()}
    return [by_code[code] for code in PRIORITY_LIQUID_CODES if code in by_code]


def _finite(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number) or number <= 0:
        return None
    return number


def _extract_quote(ticker_data: pd.DataFrame) -> dict[str, float] | None:
    """從 yfinance 歷史資料萃取報價。

    當日若只有成交量、Close 為 NaN（盤中或未收盤），改取最近有效收盤價；
    成交量一律取最新一筆有效值（供 Top 排行用）。
    """
    if ticker_data.empty:
        return None

    df = ticker_data.sort_index()
    volumes = df["Volume"].apply(lambda v: _finite(v))
    valid_volume = volumes.dropna()
    if valid_volume.empty:
        return None
    volume = int(valid_volume.iloc[-1])

    closes = df["Close"].apply(lambda v: _finite(v))
    valid_closes = closes.dropna()
    if valid_closes.empty:
        return None

    close = round(float(valid_closes.iloc[-1]), 2)
    if len(valid_closes) >= 2:
        prev_close = round(float(valid_closes.iloc[-2]), 2)
    else:
        prev_close = close

    change = round(close - prev_close, 2)
    change_pct = (
        round(change / prev_close * 100, 2) if prev_close > 0 else 0.0
    )
    return {
        "close": close,
        "volume": float(volume),
        "change": change,
        "change_pct": change_pct,
    }


def _parse_yf_download(
    data: Any,
    stocks: list[dict[str, str]],
    symbols: list[str],
) -> list[dict[str, Any]]:
    if data is None or getattr(data, "empty", True):
        return []

    multi_index = hasattr(data.columns, "nlevels") and data.columns.nlevels > 1
    results: list[dict[str, Any]] = []

    for stock in stocks:
        sym = stock.get("symbol") or f"{stock['code']}.TW"
        try:
            if multi_index:
                if sym not in data.columns.get_level_values(0):
                    continue
                ticker_data = data[sym].dropna(how="all")
            else:
                if len(symbols) == 1 and symbols[0] == sym:
                    ticker_data = data.dropna(how="all")
                else:
                    continue

            quote = _extract_quote(ticker_data)
            if quote is None:
                continue

            results.append(
                {
                    "symbol": sym,
                    "code": stock["code"],
                    "name": stock["name"],
                    "close": quote["close"],
                    "change": quote["change"],
                    "change_pct": quote["change_pct"],
                    "volume": int(quote["volume"]),
                }
            )
        except Exception:
            continue

    return results


def _fetch_yfinance_rows() -> list[dict[str, Any]]:
    import yfinance as yf

    stocks = _ranking_universe()
    if not stocks:
        return []

    symbols = [s.get("symbol") or f"{s['code']}.TW" for s in stocks]
    logger.info("ranking yfinance 單批下載 %d 檔", len(symbols))

    data = yf.download(
        symbols,
        period="5d",
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
    )
    return _parse_yf_download(data, stocks, symbols)


def _cache_has_valid_quotes(rows: list[dict[str, Any]] | None) -> bool:
    if not rows:
        return False
    return any((r.get("close") or 0) > 0 for r in rows)


def fetch_all_ranking_rows() -> list[dict[str, Any]]:
    """取得 yfinance 排行用原始列（含快取，Top / limit-up 共用）。"""
    global _ranking_cache, _ranking_cached_at, _ranking_cache_version

    now = datetime.now()
    with _ranking_fetch_lock:
        cache_valid = (
            _ranking_cache is not None
            and _ranking_cached_at is not None
            and _ranking_cache_version == RANKING_CACHE_VERSION
            and now - _ranking_cached_at < RANKING_CACHE_TTL
            and _cache_has_valid_quotes(_ranking_cache)
        )
        if cache_valid:
            logger.info("ranking yfinance 快取命中 (%d 筆)", len(_ranking_cache))
            return list(_ranking_cache)

        merged = _fetch_yfinance_rows()
        _ranking_cache = merged
        _ranking_cached_at = datetime.now()
        _ranking_cache_version = RANKING_CACHE_VERSION
        logger.info("ranking yfinance 完成 %d 筆", len(merged))
        return list(merged)


def build_top_volume(limit: int) -> dict[str, Any]:
    rows = fetch_all_ranking_rows()
    rows.sort(key=lambda x: x["volume"], reverse=True)
    trade_date = datetime.now().strftime("%Y-%m-%d")
    return {"trade_date": trade_date, "results": rows[:limit]}


def build_limit_up(limit: int) -> dict[str, Any]:
    rows = fetch_all_ranking_rows()
    limit_up = [r for r in rows if r["change_pct"] >= 9.5]
    limit_up.sort(key=lambda x: (-x["change_pct"], x["code"]))
    trade_date = datetime.now().strftime("%Y-%m-%d")
    return {"trade_date": trade_date, "results": limit_up[:limit]}
