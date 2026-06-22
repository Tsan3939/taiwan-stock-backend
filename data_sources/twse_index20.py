"""證交所 MI_INDEX20 量大排行 / 漲停股（type=MS / UP）。"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import requests

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0"}
_TOP_VOLUME_URL = (
    "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX20"
    "?response=json&type=MS"
)
_LIMIT_UP_URL = (
    "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX20"
    "?response=json&type=UP"
)


def _parse_number(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "")
    if not text or text in ("--", "X", "-"):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _parse_change(value: Any) -> float:
    text = str(value).strip().replace(",", "") if value is not None else ""
    if not text or text in ("--", "X", "-"):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _parse_change_pct(value: Any) -> float:
    text = str(value).strip().replace(",", "").replace("%", "") if value else ""
    if not text or text in ("--", "X", "-"):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _trade_date_from_title(title: str) -> str:
    """MI_INDEX20 title 例：115年06月19日 成交量前二十名證券"""
    match = re.search(r"(\d+)年(\d+)月(\d+)日", title)
    if not match:
        return datetime.now().strftime("%Y-%m-%d")
    roc, month, day = match.groups()
    year = int(roc) + 1911
    return f"{year:04d}-{month}-{day}"


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    code = str(row.get("證券代號", "")).strip()
    name = str(row.get("證券名稱", "")).strip()
    close = _parse_number(row.get("收盤價"))
    change = _parse_change(row.get("漲跌價差"))
    change_pct = _parse_change_pct(row.get("漲跌幅"))
    volume = int(_parse_number(row.get("成交股數")))

    if change_pct == 0.0 and close > 0 and change != 0.0:
        prev_close = close - change
        if prev_close > 0:
            change_pct = round(change / prev_close * 100, 2)

    return {
        "symbol": f"{code}.TW",
        "code": code,
        "name": name,
        "close": close,
        "change": change,
        "change_pct": change_pct,
        "volume": volume,
    }


def _fetch_index20(url: str, limit: int | None = None) -> dict[str, Any]:
    response = requests.get(url, headers=_HEADERS, timeout=15)
    response.raise_for_status()
    payload = response.json()

    if payload.get("stat") not in (None, "OK"):
        message = payload.get("stat") or "TWSE 回傳非 OK"
        raise RuntimeError(message)

    fields = payload.get("fields") or []
    rows = payload.get("data") or []
    title = str(payload.get("title") or "")
    trade_date = _trade_date_from_title(title)

    results: list[dict[str, Any]] = []
    for row in rows:
        if not row:
            continue
        mapped = dict(zip(fields, row))
        code = str(mapped.get("證券代號", "")).strip()
        if not code or code.startswith("00"):
            continue
        results.append(_normalize_row(mapped))
        if limit is not None and len(results) >= limit:
            break

    return {"trade_date": trade_date, "results": results}


def fetch_top_volume(limit: int = 10) -> dict[str, Any]:
    return _fetch_index20(_TOP_VOLUME_URL, limit=limit)


def fetch_limit_up(limit: int = 10) -> dict[str, Any]:
    return _fetch_index20(_LIMIT_UP_URL, limit=limit)
