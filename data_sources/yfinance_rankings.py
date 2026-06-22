"""yfinance 備援：量大排行 / 漲停（僅高流動性標的 + 快取）。"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

RANKING_CACHE_TTL = timedelta(minutes=15)
TWSE_BLOCK_TTL = timedelta(minutes=30)

# 只取高流動性標的：Top 10 成交量幾乎都在此池；limit-up 備援亦足夠。
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
    picked: list[dict[str, str]] = []
    for code in PRIORITY_LIQUID_CODES:
        stock = by_code.get(code)
        if stock:
            picked.append(stock)
    return picked


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

            if ticker_data.empty:
                continue

            row = ticker_data.iloc[-1]
            volume = int(row.get("Volume", 0) or 0)
            close = float(row.get("Close", 0) or 0)
            if volume <= 0 or close <= 0:
                continue

            prev_rows = ticker_data.iloc[:-1]
            prev_close = (
                float(prev_rows.iloc[-1]["Close"])
                if not prev_rows.empty
                else close
            )
            change = round(close - prev_close, 2)
            change_pct = (
                round(change / prev_close * 100, 2) if prev_close > 0 else 0.0
            )
            results.append(
                {
                    "symbol": sym,
                    "code": stock["code"],
                    "name": stock["name"],
                    "close": close,
                    "change": change,
                    "change_pct": change_pct,
                    "volume": volume,
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
        period="2d",
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
    )
    return _parse_yf_download(data, stocks, symbols)


def fetch_all_ranking_rows() -> list[dict[str, Any]]:
    """取得 yfinance 排行用原始列（含快取，Top / limit-up 共用）。"""
    global _ranking_cache, _ranking_cached_at

    now = datetime.now()
    with _ranking_fetch_lock:
        if (
            _ranking_cache is not None
            and _ranking_cached_at is not None
            and now - _ranking_cached_at < RANKING_CACHE_TTL
        ):
            logger.info("ranking yfinance 快取命中 (%d 筆)", len(_ranking_cache))
            return list(_ranking_cache)

        merged = _fetch_yfinance_rows()
        _ranking_cache = merged
        _ranking_cached_at = datetime.now()
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
