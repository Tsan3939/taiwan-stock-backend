"""證交所 / 櫃買中心 量大排行與漲停股資料。"""

import logging
import time
from datetime import datetime, timedelta
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REQUEST_TIMEOUT = 20
RANKING_DISPLAY_LIMIT = 10
TWSE_DAY_ALL = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL"
TPEX_DAILY_OPENAPI = (
    "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
)
TPEX_DAILY_WEB = (
    "https://www.tpex.org.tw/web/stock/aftertrading/"
    "daily_close_quotes/stk_quote_result.php"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

logger = logging.getLogger(__name__)


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


def _is_etf(code: str) -> bool:
    return code.startswith("00")


def _to_symbol(code: str, market: str) -> str:
    suffix = "TWO" if market == "otc" else "TW"
    return f"{code}.{suffix}"


def _ad_to_roc_slash(date_yyyymmdd: str) -> str:
    """20260619 → 115/06/19"""
    roc = int(date_yyyymmdd[:4]) - 1911
    return f"{roc}/{date_yyyymmdd[4:6]}/{date_yyyymmdd[6:8]}"


def _ad_to_roc_compact(date_yyyymmdd: str) -> str:
    """20260619 → 1150619"""
    roc = int(date_yyyymmdd[:4]) - 1911
    return f"{roc}{date_yyyymmdd[4:6]}{date_yyyymmdd[6:8]}"


def _is_limit_up_row(
    change_sign: str, change_amount: float, change_pct: float, high: float, close: float
) -> bool:
    return (
        change_sign == "+"
        and change_amount > 0
        and change_pct >= 9.5
        and abs(high - close) < 0.01
    )


def _build_otc_row(
    code: str,
    name: str,
    close: float,
    change_sign: str,
    change_amount: float,
    high: float,
    volume: float,
) -> dict[str, Any]:
    change = change_amount if change_sign == "+" else (
        -change_amount if change_sign == "-" else 0.0
    )
    prev_close = close - change
    change_pct = change / prev_close * 100 if prev_close > 0 and change else 0.0
    return {
        "code": code,
        "name": name,
        "symbol": _to_symbol(code, "otc"),
        "market": "otc",
        "volume": volume,
        "close": close,
        "change": round(change, 2),
        "change_pct": round(change_pct, 2),
        "is_limit_up": _is_limit_up_row(
            change_sign, change_amount, change_pct, high, close
        ),
    }


def _try_dates(max_days: int = 10) -> list[str]:
    dates: list[str] = []
    current = datetime.now()
    for _ in range(max_days):
        if current.weekday() < 5:
            dates.append(current.strftime("%Y%m%d"))
        current -= timedelta(days=1)
    return dates


def _parse_change(value: str | int | float | None) -> tuple[str, float]:
    """解析漲跌價差欄位，例如 '+0.25'、'-0.26'。"""
    text = str(value).strip() if value is not None else ""
    if not text or text in ("--", "X", "-"):
        return "", 0.0
    if text.startswith("+"):
        return "+", _parse_number(text[1:])
    if text.startswith("-"):
        return "-", _parse_number(text[1:])
    amount = _parse_number(text)
    if amount > 0:
        return "+", amount
    if amount < 0:
        return "-", abs(amount)
    return "", 0.0


def _fetch_twse_day(date_yyyymmdd: str) -> list[dict[str, Any]]:
    params = {"response": "json", "date": date_yyyymmdd}
    try:
        resp = requests.get(
            TWSE_DAY_ALL, params=params, headers=_HEADERS, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        logger.exception("TWSE daily fetch failed for %s", date_yyyymmdd)
        return []

    if payload.get("stat") != "OK":
        return []

    rows: list[dict[str, Any]] = []
    for row in payload.get("data", []):
        if len(row) < 10:
            continue
        code = str(row[0]).strip()
        name = str(row[1]).strip()
        volume = _parse_number(row[2])
        high_price = _parse_number(row[5])
        close_price = _parse_number(row[7])
        change_sign, change_amount = _parse_change(row[8])

        prev_close = (
            close_price - change_amount
            if change_sign == "+"
            else close_price + change_amount
        )
        change = (
            change_amount
            if change_sign == "+"
            else (-change_amount if change_sign == "-" else 0.0)
        )
        change_pct = 0.0
        if prev_close > 0 and change != 0:
            change_pct = change / prev_close * 100

        rows.append(
            {
                "code": code,
                "name": name,
                "symbol": _to_symbol(code, "twse"),
                "market": "twse",
                "volume": volume,
                "close": close_price,
                "change": round(change, 2),
                "change_pct": round(change_pct, 2),
                "is_limit_up": _is_limit_up_row(
                    change_sign, change_amount, change_pct, high_price, close_price
                ),
            }
        )
    return rows


def _fetch_tpex_day_web(date_yyyymmdd: str) -> list[dict[str, Any]]:
    """上櫃每日收盤：支援指定日期（民國年 d=115/06/19）。"""
    params = {
        "l": "zh-tw",
        "d": _ad_to_roc_slash(date_yyyymmdd),
        "s": "0,asc",
    }
    payload: dict[str, Any] | None = None
    for attempt in range(3):
        try:
            resp = requests.get(
                TPEX_DAILY_WEB,
                params=params,
                headers=_HEADERS,
                timeout=60,
                verify=False,
            )
            resp.raise_for_status()
            payload = resp.json()
            break
        except Exception:
            if attempt == 2:
                logger.exception("TPEx web daily fetch failed for %s", date_yyyymmdd)
                return []
            time.sleep(1)

    if not payload or payload.get("stat") != "ok":
        return []

    tables = payload.get("tables") or []
    if not tables:
        return []

    rows: list[dict[str, Any]] = []
    for row in tables[0].get("data") or []:
        if len(row) < 9:
            continue
        code = str(row[0]).strip()
        if not (code.isdigit() and len(code) == 4) or _is_etf(code):
            continue

        name = str(row[1]).strip()
        close_price = _parse_number(row[2])
        change_sign, change_amount = _parse_change(row[3])
        high_price = _parse_number(row[5])
        volume = _parse_number(row[8])

        rows.append(
            _build_otc_row(
                code, name, close_price, change_sign, change_amount, high_price, volume
            )
        )

    logger.info(
        "TPEx web 載入 %d 檔 (請求 %s, 回應 date=%s)",
        len(rows),
        date_yyyymmdd,
        payload.get("date", ""),
    )
    return rows


def _fetch_tpex_day_openapi(date_yyyymmdd: str) -> list[dict[str, Any]]:
    """OpenAPI 備援：僅含最新一個交易日，日期格式為 1150618。"""
    target = _ad_to_roc_compact(date_yyyymmdd)
    try:
        resp = requests.get(
            TPEX_DAILY_OPENAPI,
            headers=_HEADERS,
            timeout=60,
            verify=False,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        logger.exception("TPEx openapi fetch failed")
        return []

    if not isinstance(payload, list) or not payload:
        return []

    sample_date = str(payload[0].get("Date", ""))
    if sample_date != target:
        logger.info(
            "TPEx openapi 日期不符：需要 %s，最新為 %s，略過",
            target,
            sample_date,
        )
        return []

    rows: list[dict[str, Any]] = []
    for item in payload:
        code = str(item.get("SecuritiesCompanyCode", "")).strip()
        if not (code.isdigit() and len(code) == 4) or _is_etf(code):
            continue

        name = str(item.get("CompanyName", "")).strip()
        close_price = _parse_number(item.get("Close"))
        change_sign, change_amount = _parse_change(item.get("Change"))
        high_price = _parse_number(item.get("High"))
        volume = _parse_number(item.get("TradingShares"))

        rows.append(
            _build_otc_row(
                code, name, close_price, change_sign, change_amount, high_price, volume
            )
        )

    logger.info("TPEx openapi 載入 %d 檔 (date=%s)", len(rows), target)
    return rows


def _fetch_tpex_day(date_yyyymmdd: str) -> list[dict[str, Any]]:
    rows = _fetch_tpex_day_web(date_yyyymmdd)
    if rows:
        return rows
    return _fetch_tpex_day_openapi(date_yyyymmdd)


def _fetch_all_stocks(date_yyyymmdd: str) -> tuple[list[dict[str, Any]], str]:
    twse_rows = _fetch_twse_day(date_yyyymmdd)
    tpex_rows = _fetch_tpex_day(date_yyyymmdd)
    all_rows = twse_rows + tpex_rows
    trade_date = (
        f"{date_yyyymmdd[:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}"
    )
    return all_rows, trade_date


def _load_latest_stocks() -> tuple[list[dict[str, Any]], str]:
    for date_yyyymmdd in _try_dates():
        try:
            rows, trade_date = _fetch_all_stocks(date_yyyymmdd)
        except Exception:
            logger.exception("Failed loading market data for %s", date_yyyymmdd)
            continue
        if rows:
            return rows, trade_date
    return [], ""


def get_top_volume(limit: int = RANKING_DISPLAY_LIMIT) -> dict[str, Any]:
    rows, trade_date = _load_latest_stocks()
    filtered = [r for r in rows if not _is_etf(r["code"]) and r["volume"] > 0]
    filtered.sort(key=lambda r: r["volume"], reverse=True)
    top_rows = filtered[:limit]

    results = [
        {
            "symbol": r["symbol"],
            "code": r["code"],
            "name": r["name"],
            "volume": int(r["volume"]),
            "close": r["close"],
            "change": r["change"],
            "change_pct": r["change_pct"],
        }
        for r in top_rows
    ]
    return {"trade_date": trade_date, "results": results}


def get_limit_up() -> dict[str, Any]:
    rows, trade_date = _load_latest_stocks()
    filtered = [
        r
        for r in rows
        if not _is_etf(r["code"]) and r.get("is_limit_up")
    ]
    filtered.sort(key=lambda r: (-r["change_pct"], r["code"]))

    results = [
        {
            "symbol": r["symbol"],
            "code": r["code"],
            "name": r["name"],
            "volume": int(r["volume"]),
            "close": r["close"],
            "change": r["change"],
            "change_pct": r["change_pct"],
        }
        for r in filtered[:RANKING_DISPLAY_LIMIT]
    ]
    return {"trade_date": trade_date, "results": results}
