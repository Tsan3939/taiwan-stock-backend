"""Yahoo 奇摩股市台股排行（直接擷取已排序前 N 名，無需下載全市場）。"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta
from typing import Any

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
}
_VOLUME_URL = "https://tw.stock.yahoo.com/rank/volume?exchange=TAI"
_CHANGE_UP_URL = "https://tw.stock.yahoo.com/rank/change-up?exchange=TAI"

_CACHE_TTL = timedelta(minutes=5)
_cache_lock = threading.Lock()
_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}


def _parse_number(text: str) -> float:
    cleaned = text.strip().replace(",", "").replace("%", "").replace("+", "")
    if not cleaned or cleaned == "-":
        return 0.0
    return float(cleaned)


def _parse_rank_page(html: str, limit: int) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, Any]] = []

    for row in soup.select("div.table-row"):
        link = row.select_one('a[href*="/quote/"]')
        if not link:
            continue
        href = link.get("href", "")
        sym_m = re.search(r"/quote/(\d{4}\.(?:TW|TWO))", href)
        if not sym_m:
            continue
        symbol = sym_m.group(1)
        code = symbol.split(".")[0]

        name_el = row.select_one("div.Lh\\(20px\\)")
        name = name_el.get_text(strip=True) if name_el else code

        cols = [
            s.get_text(strip=True)
            for s in row.select(
                "span.Jc\\(fe\\), span.C\\(\\$c-trend-up\\), span.C\\(\\$c-trend-down\\), "
                "span.C\\(\\$c-trend-flat\\), span.Fw\\(600\\)"
            )
        ]
        if len(cols) < 7:
            continue

        close = _parse_number(cols[0])
        change = _parse_number(cols[1])
        change_pct = _parse_number(cols[2])
        volume = int(_parse_number(cols[6]))
        if close <= 0 or volume <= 0:
            continue

        rows.append(
            {
                "symbol": symbol,
                "code": code,
                "name": name,
                "close": round(close, 2),
                "change": round(change, 2),
                "change_pct": round(change_pct, 2),
                "volume": volume,
            }
        )
        if len(rows) >= limit:
            break

    return rows


def _fetch_cached(kind: str, url: str, limit: int) -> dict[str, Any]:
    now = datetime.now()
    with _cache_lock:
        hit = _cache.get(kind)
        if hit and now - hit[0] < _CACHE_TTL:
            payload = hit[1]
            return {
                "trade_date": payload["trade_date"],
                "results": payload["results"][:limit],
            }

    logger.info("Yahoo TW 擷取排行 kind=%s url=%s", kind, url)
    resp = requests.get(url, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    # 多抓一些供 limit-up 篩選
    parse_limit = max(limit, 50) if kind == "change_up" else limit
    results = _parse_rank_page(resp.text, parse_limit)
    payload = {
        "trade_date": datetime.now().strftime("%Y-%m-%d"),
        "results": results,
    }
    with _cache_lock:
        _cache[kind] = (now, payload)
    return {
        "trade_date": payload["trade_date"],
        "results": results[:limit],
    }


def fetch_top_volume(limit: int = 10) -> dict[str, Any]:
    return _fetch_cached("volume", _VOLUME_URL, limit)


def fetch_limit_up(limit: int = 10) -> dict[str, Any]:
    payload = _fetch_cached("change_up", _CHANGE_UP_URL, 50)
    limit_up = [
        r
        for r in payload["results"]
        if r["change_pct"] >= 9.0
        or (0 < r["close"] < 50 and r["change_pct"] >= 4.5)
    ]
    return {
        "trade_date": payload["trade_date"],
        "results": limit_up[:limit],
    }
