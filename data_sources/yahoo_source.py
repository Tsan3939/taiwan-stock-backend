"""Yahoo Finance 歷史報價資料來源。"""

import logging
import time
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30
_MAX_RETRIES = 3


def fetch_ohlcv(
    symbol: str, start_date: str, end_date: str
) -> pd.DataFrame:
    """
    取得指定區間的 OHLCV 資料。
    回傳 DataFrame，index 為日期，欄位含 Open/High/Low/Close/Volume。
    """
    end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
    end_str = end_dt.strftime("%Y-%m-%d")
    last_error: Exception | None = None

    for attempt in range(_MAX_RETRIES):
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(
                start=start_date,
                end=end_str,
                auto_adjust=True,
                timeout=REQUEST_TIMEOUT,
            )
            if df is None or df.empty:
                return pd.DataFrame()

            df = df.copy()
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df.index = df.index.normalize()
            df = df.dropna(subset=["Open", "High", "Low", "Close"])
            return df
        except Exception as exc:
            last_error = exc
            logger.warning(
                "yfinance fetch_ohlcv attempt %d/%d failed for %s: %s",
                attempt + 1,
                _MAX_RETRIES,
                symbol,
                exc,
            )
            if attempt < _MAX_RETRIES - 1:
                time.sleep(2**attempt)
                continue
            raise

    if last_error is not None:
        raise last_error
    return pd.DataFrame()


def get_trading_dates(
    symbol: str, start_date: str, end_date: str
) -> list[str]:
    """從 Yahoo 取得交易日清單（字串 YYYY-MM-DD）。"""
    df = fetch_ohlcv(symbol, start_date, end_date)
    if df.empty:
        return []
    return [d.strftime("%Y-%m-%d") for d in df.index]
