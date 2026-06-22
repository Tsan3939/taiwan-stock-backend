"""Yahoo Finance 歷史報價資料來源。"""

from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import yfinance as yf

REQUEST_TIMEOUT = 15


def fetch_ohlcv(
    symbol: str, start_date: str, end_date: str
) -> pd.DataFrame:
    """
    取得指定區間的 OHLCV 資料。
    回傳 DataFrame，index 為日期，欄位含 Open/High/Low/Close/Volume。
    """
    end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
    ticker = yf.Ticker(symbol)
    df = ticker.history(
        start=start_date,
        end=end_dt.strftime("%Y-%m-%d"),
        auto_adjust=False,
        timeout=REQUEST_TIMEOUT,
    )
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.index = df.index.normalize()
    return df


def get_trading_dates(
    symbol: str, start_date: str, end_date: str
) -> list[str]:
    """從 Yahoo 取得交易日清單（字串 YYYY-MM-DD）。"""
    df = fetch_ohlcv(symbol, start_date, end_date)
    if df.empty:
        return []
    return [d.strftime("%Y-%m-%d") for d in df.index]
