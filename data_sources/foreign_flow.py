"""外資買賣超與持股。

買賣超：FinMind 外陸資（不含外資自營商），單位換算成張。
持股：上市用證交所 MI_QFIIS、上櫃用櫃買中心，全體外資及陸資持有股數換算成張。
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import requests

from data_sources.yfinance_rankings import (
    is_twse_blocked,
    mark_twse_blocked,
    should_skip_twse,
)

logger = logging.getLogger(__name__)

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
_TWSE_QFIIS = "https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS"
_TPEX_QFII = "https://www.tpex.org.tw/www/zh-tw/insti/qfii"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.twse.com.tw/",
}

# 持股是整張市場日報，快取後同一天換股不必重抓。
_holding_cache: dict[str, dict[str, int]] = {}
_cache_lock = threading.Lock()
_holding_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="foreign-hold")

# 避免一次打太多日報；預設一個月約 22 個交易日。
_MAX_HOLDING_DAYS = 80


def _parse_number(value: object) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text or text in ("--", "X", "-", "null"):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _lots(shares: float) -> int:
    return int(round(shares / 1000.0))


def _code_and_market(symbol: str) -> tuple[str, str]:
    text = symbol.strip().upper()
    if "." in text:
        code, suffix = text.split(".", 1)
        market = "TWO" if suffix == "TWO" else "TW"
        return code, market
    return text, "TW"


def _iso_to_compact(iso_date: str) -> str:
    return iso_date.replace("-", "")


def _compact_to_iso(compact: str) -> str:
    if len(compact) == 8 and compact.isdigit():
        return f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"
    return compact


def _roc_to_compact(text: str) -> str:
    parts = str(text).strip().split("/")
    if len(parts) != 3:
        return ""
    try:
        year = int(parts[0]) + 1911
        return f"{year:04d}{int(parts[1]):02d}{int(parts[2]):02d}"
    except ValueError:
        return ""


def _fetch_net_buy_lots(code: str, start_date: str, end_date: str) -> dict[str, int]:
    """外陸資買賣超（張）。key 為 YYYY-MM-DD。"""
    try:
        resp = requests.get(
            FINMIND_URL,
            params={
                "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
                "data_id": code,
                "start_date": start_date,
                "end_date": end_date,
            },
            headers=_HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        logger.exception("FinMind 外資買賣超失敗 %s", code)
        return {}

    if payload.get("status") != 200:
        logger.warning("FinMind 外資買賣超異常 %s: %s", code, payload.get("msg"))
        return {}

    lots: dict[str, int] = {}
    for item in payload.get("data") or []:
        if item.get("name") != "Foreign_Investor":
            continue
        date_iso = str(item.get("date", ""))[:10]
        if not date_iso:
            continue
        net_shares = _parse_number(item.get("buy")) - _parse_number(item.get("sell"))
        lots[date_iso] = _lots(net_shares)
    return lots


def _map_from_rows(fields: list, rows: list, code_name: str, hold_name: str) -> dict[str, int]:
    try:
        code_i = fields.index(code_name)
        hold_i = fields.index(hold_name)
    except ValueError:
        for index, name in enumerate(fields):
            if "持有股數" in str(name) and "尚可" not in str(name):
                hold_i = index
                break
        else:
            logger.warning("持股欄位找不到: %s", fields)
            return {}
        try:
            code_i = fields.index(code_name)
        except ValueError:
            return {}

    mapped: dict[str, int] = {}
    for row in rows:
        if not row or len(row) <= max(code_i, hold_i):
            continue
        code = str(row[code_i]).strip()
        if not code:
            continue
        mapped[code] = _lots(_parse_number(row[hold_i]))
    return mapped


def _fetch_twse_holdings(yyyymmdd: str) -> dict[str, int] | None:
    if should_skip_twse():
        return None
    try:
        resp = requests.get(
            _TWSE_QFIIS,
            params={
                "response": "json",
                "date": yyyymmdd,
                "selectType": "ALLBUT0999",
            },
            headers=_HEADERS,
            timeout=15,
        )
    except Exception:
        logger.exception("MI_QFIIS 請求失敗 %s", yyyymmdd)
        return None

    text = resp.text or ""
    if is_twse_blocked(resp.status_code, text):
        mark_twse_blocked()
        return None
    if resp.status_code != 200 or not text.strip():
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("stat") not in (None, "OK") or not payload.get("data"):
        return {}
    resp_date = str(payload.get("date") or "")
    if resp_date and resp_date != yyyymmdd:
        return {}
    fields = payload.get("fields") or []
    return _map_from_rows(fields, payload.get("data") or [], "證券代號", "全體外資及陸資持有股數")


def _fetch_tpex_holdings(yyyymmdd: str) -> dict[str, int] | None:
    iso = _compact_to_iso(yyyymmdd)
    try:
        resp = requests.get(
            _TPEX_QFII,
            params={"date": iso.replace("-", "/"), "response": "json"},
            headers={**_HEADERS, "Referer": "https://www.tpex.org.tw/"},
            timeout=15,
        )
    except Exception:
        logger.exception("TPEx 外資持股請求失敗 %s", yyyymmdd)
        return None
    if resp.status_code != 200 or not (resp.text or "").strip():
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    tables = payload.get("tables") if isinstance(payload, dict) else None
    if not tables:
        return {}
    table = tables[0] or {}
    table_date = _roc_to_compact(str(table.get("date") or ""))
    if table_date and table_date != yyyymmdd:
        return {}
    fields = table.get("fields") or []
    hold_name = next(
        (name for name in fields if "持有股數" in str(name) and "尚可" not in str(name)),
        "僑外資及陸資持有股數(C)",
    )
    return _map_from_rows(fields, table.get("data") or [], "代號", hold_name)


def _holding_map(market: str, yyyymmdd: str) -> dict[str, int] | None:
    key = f"{market}:{yyyymmdd}"
    with _cache_lock:
        cached = _holding_cache.get(key)
    if cached is not None:
        return cached

    fetched = _fetch_tpex_holdings(yyyymmdd) if market == "TWO" else _fetch_twse_holdings(yyyymmdd)
    if fetched is None:
        return None
    with _cache_lock:
        _holding_cache[key] = fetched
    return fetched


def _weekdays(start_date: str, end_date: str) -> list[str]:
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    dates: list[str] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return dates


def fetch_foreign_flow(symbol: str, start_date: str, end_date: str) -> list[dict]:
    code, market = _code_and_market(symbol)
    if not code:
        return []

    net_lots = _fetch_net_buy_lots(code, start_date, end_date)
    holding_dates = sorted(net_lots.keys())
    if not holding_dates:
        holding_dates = _weekdays(start_date, end_date)
    if len(holding_dates) > _MAX_HOLDING_DAYS:
        holding_dates = holding_dates[-_MAX_HOLDING_DAYS:]

    holdings: dict[str, int] = {}
    futures = {
        _holding_executor.submit(_holding_map, market, _iso_to_compact(iso)): iso
        for iso in holding_dates
    }
    for future in as_completed(futures):
        iso = futures[future]
        try:
            day_map = future.result()
        except Exception:
            logger.exception("外資持股失敗 %s %s", symbol, iso)
            continue
        if not day_map:
            continue
        lots = day_map.get(code)
        if lots is not None:
            holdings[iso] = lots

    rows: list[dict] = []
    for iso in sorted(set(net_lots) | set(holdings)):
        if iso < start_date or iso > end_date:
            continue
        rows.append(
            {
                "date": iso,
                "net_buy_lots": net_lots.get(iso),
                "holding_lots": holdings.get(iso),
            }
        )
    logger.info(
        "外資 %s %s~%s 買賣超 %d 日、持股 %d 日",
        symbol,
        start_date,
        end_date,
        len(net_lots),
        len(holdings),
    )
    return rows
