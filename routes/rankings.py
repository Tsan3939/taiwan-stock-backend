"""量大排行 / 漲停路由。

路由函式命名為 route_*，避免與 data_sources 內 get_top_volume 等同名 import 衝突
導致 maximum recursion depth exceeded。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import requests
from flask import Blueprint, jsonify

rankings_bp = Blueprint("rankings", __name__)
logger = logging.getLogger(__name__)

_TWSE_HEADERS = {"User-Agent": "Mozilla/5.0"}
_TWSE_MS_URL = (
    "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX20"
    "?response=json&type=MS"
)
_TWSE_UP_URL = (
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
    match = re.search(r"(\d+)年(\d+)月(\d+)日", title)
    if not match:
        return datetime.now().strftime("%Y-%m-%d")
    roc, month, day = match.groups()
    year = int(roc) + 1911
    return f"{year:04d}-{month}-{day}"


def _normalize_stock_row(row: dict[str, Any]) -> dict[str, Any]:
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


def _load_mi_index20(url: str, limit: int | None = None) -> dict[str, Any]:
    """向 TWSE 取 MI_INDEX20 資料（只用 requests.get，不呼叫路由函式）。"""
    response = requests.get(url, headers=_TWSE_HEADERS, timeout=15)
    response.raise_for_status()
    payload = response.json()

    if payload.get("stat") not in (None, "OK"):
        raise RuntimeError(payload.get("stat") or "TWSE 回傳非 OK")

    fields = payload.get("fields") or []
    rows = payload.get("data") or []
    trade_date = _trade_date_from_title(str(payload.get("title") or ""))

    results: list[dict[str, Any]] = []
    for row in rows:
        if not row:
            continue
        mapped = dict(zip(fields, row))
        code = str(mapped.get("證券代號", "")).strip()
        if not code or code.startswith("00"):
            continue
        results.append(_normalize_stock_row(mapped))
        if limit is not None and len(results) >= limit:
            break

    return {"trade_date": trade_date, "results": results}


def _error_response(message: str, status: int = 500):
    return jsonify({"error": message, "trade_date": "", "results": []}), status


@rankings_bp.route("/api/stocks/top_volume", methods=["GET"])
@rankings_bp.route("/api/stocks/top-volume", methods=["GET"])
def route_top_volume():
    try:
        payload = _load_mi_index20(_TWSE_MS_URL, limit=30)
        return jsonify(payload)
    except RecursionError:
        logger.exception("route_top_volume recursion")
        return _error_response("recursion detected, check function names")
    except Exception as exc:
        logger.exception("route_top_volume error")
        return _error_response(str(exc))


@rankings_bp.route("/api/stocks/limit_up", methods=["GET"])
@rankings_bp.route("/api/stocks/limit-up", methods=["GET"])
def route_limit_up():
    try:
        payload = _load_mi_index20(_TWSE_UP_URL, limit=None)
        return jsonify(payload)
    except RecursionError:
        logger.exception("route_limit_up recursion")
        return _error_response("recursion detected, check function names")
    except Exception as exc:
        logger.exception("route_limit_up error")
        return _error_response(str(exc))
