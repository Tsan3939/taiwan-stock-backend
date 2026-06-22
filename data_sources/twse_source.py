"""證交所 / 櫃買中心每日成交資料來源（含成交筆數）。"""

import logging
import time
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import requests
from dateutil.relativedelta import relativedelta

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15
TWSE_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _parse_twse_number(value: str) -> float:
    """TWSE 回傳數字可能含逗號，'--' 代表無資料。"""
    if not value or value.strip() in ("--", "X"):
        return 0.0
    return float(value.replace(",", "").replace("--", "0"))


def _fetch_twse_month(stock_code: str, year_month: str) -> list[dict[str, Any]]:
    """取得上市股票單月 STOCK_DAY 資料。year_month 格式 YYYYMMDD（取該月最後一天）。"""
    params = {
        "response": "json",
        "date": year_month,
        "stockNo": stock_code,
    }
    resp = requests.get(
        TWSE_URL, params=params, headers=_HEADERS, timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("stat") != "OK":
        # 若指定日期無資料（非交易日或未來日期），往前逐日重試
        try:
            query = datetime.strptime(year_month, "%Y%m%d")
        except ValueError:
            return []
        month_start = query.replace(day=1)
        for offset in range(1, 15):
            fallback = query - timedelta(days=offset)
            if fallback < month_start:
                break
            fallback_str = fallback.strftime("%Y%m%d")
            params["date"] = fallback_str
            resp = requests.get(
                TWSE_URL, params=params, headers=_HEADERS, timeout=REQUEST_TIMEOUT
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("stat") == "OK":
                break
        else:
            return []

    rows: list[dict[str, Any]] = []
    # 欄位：日期, 成交股數, 成交金額, 開盤, 最高, 最低, 收盤, 漲跌價差, 成交筆數
    for row in payload.get("data", []):
        if len(row) < 9:
            continue
        date_str = row[0]
        # 民國年格式 115/02/10
        parts = date_str.split("/")
        if len(parts) != 3:
            continue
        roc_year = int(parts[0]) + 1911
        date_iso = f"{roc_year:04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"

        volume_shares = _parse_twse_number(row[1])
        trade_count = _parse_twse_number(row[8])

        rows.append(
            {
                "date": date_iso,
                "volume_shares": volume_shares,
                "trade_count": trade_count,
            }
        )
    return rows


def _fetch_tpex_range(
    stock_code: str, symbol: str, start_date: str, end_date: str
) -> list[dict[str, Any]]:
    """
    上櫃個股：st43_result.php 已下線，stk_quote_result 亦無法查歷史日期。
    改以 FinMind TaiwanStockPrice 一次取得區間內成交股數與成交筆數。
    """
    rows = _fetch_tpex_range_finmind(stock_code, start_date, end_date)
    if rows:
        return rows

    logger.warning(
        "FinMind 無 %s 資料，略過上櫃均張（symbol=%s）",
        stock_code,
        symbol,
    )
    return []


def _fetch_tpex_range_finmind(
    stock_code: str, start_date: str, end_date: str
) -> list[dict[str, Any]]:
    try:
        resp = requests.get(
            FINMIND_URL,
            params={
                "dataset": "TaiwanStockPrice",
                "data_id": stock_code,
                "start_date": start_date,
                "end_date": end_date,
            },
            headers=_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        logger.exception("FinMind fetch failed for %s", stock_code)
        return []

    if payload.get("status") != 200:
        logger.warning(
            "FinMind 回應異常 %s: %s",
            stock_code,
            payload.get("msg"),
        )
        return []

    rows: list[dict[str, Any]] = []
    for item in payload.get("data") or []:
        date_iso = str(item.get("date", ""))[:10]
        if not date_iso:
            continue
        volume_shares = _parse_twse_number(str(item.get("Trading_Volume", 0)))
        trade_count = _parse_twse_number(str(item.get("Trading_turnover", 0)))
        rows.append(
            {
                "date": date_iso,
                "volume_shares": volume_shares,
                "trade_count": trade_count,
            }
        )
    return rows


def _month_end_dates(start_date: str, end_date: str) -> list[str]:
    """產生需查詢的各月 API 日期參數（YYYYMMDD）。

    TWSE 需傳入該月有效交易日；若查詢月份尚未結束，必須用 end_date
    而非該月最後一天，否則 API 會回傳空資料（例如 6 月用 0630 但今日為 6/21）。
    查詢日不可超過今天（未來日期 TWSE 會回傳空）。
    """
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if end > today:
        end = today
    months: list[str] = []
    current = start.replace(day=1)
    while current <= end:
        month_end = (current + relativedelta(months=1)) - relativedelta(days=1)
        query_date = min(end, month_end)
        months.append(query_date.strftime("%Y%m%d"))
        current += relativedelta(months=1)
    return months


def fetch_daily_trades(
    symbol: str, start_date: str, end_date: str
) -> pd.DataFrame:
    """
    取得成交股數與成交筆數。
    symbol 格式：2330.TW（上市）或 xxxx.TWO（上櫃）
    """
    code = symbol.split(".")[0]
    market = symbol.split(".")[-1].upper() if "." in symbol else "TW"
    is_otc = market == "TWO"

    all_rows: list[dict[str, Any]] = []
    if is_otc:
        all_rows = _fetch_tpex_range(code, symbol, start_date, end_date)
    else:
        for month_date in _month_end_dates(start_date, end_date):
            rows = _fetch_twse_month(code, month_date)
            all_rows.extend(rows)
            time.sleep(0.3)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df = df.drop_duplicates(subset=["date"]).set_index("date").sort_index()
    df.index = pd.to_datetime(df.index)

    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    df = df.loc[(df.index >= start) & (df.index <= end)]
    return df
