# data_sources/twse_source.py
# 完全改用 yfinance，不再呼叫 TWSE API

import logging
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


def fetch_daily_trades(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """
    抓取個股每日成交資料，回傳 DataFrame（index 為日期）。

    symbol 格式：'2330.TW' 或 '6548.TWO'
    start_date / end_date 格式：'YYYY-MM-DD'

    回傳欄位：
    open, high, low, close, volume, change, volume_shares
    （volume_shares 與 volume 相同，供 avg_lot 相容使用）
    """
    try:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
        ticker = yf.Ticker(symbol)
        df = ticker.history(
            start=start_date,
            end=end_dt.strftime("%Y-%m-%d"),
            auto_adjust=True,
        )

        if df is None or df.empty:
            logger.warning(
                "yfinance 無資料: %s %s~%s", symbol, start_date, end_date
            )
            return pd.DataFrame()

        df = df.reset_index()
        date_col = "Date" if "Date" in df.columns else df.columns[0]
        df["date"] = pd.to_datetime(df[date_col]).dt.strftime("%Y-%m-%d")
        df["open"] = df["Open"].round(2)
        df["high"] = df["High"].round(2)
        df["low"] = df["Low"].round(2)
        df["close"] = df["Close"].round(2)
        df["volume"] = df["Volume"].fillna(0).astype(int)
        df["change"] = df["close"].diff().round(2).fillna(0)
        df["volume_shares"] = df["volume"]

        result = df[
            ["date", "open", "high", "low", "close", "volume", "change", "volume_shares"]
        ].copy()
        result = result.set_index(pd.to_datetime(result["date"]))
        result.index = result.index.normalize()
        result = result.drop(columns=["date"])

        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        result = result.loc[(result.index >= start) & (result.index <= end)]
        return result.sort_index()

    except Exception as exc:
        logger.error("yfinance fetch_daily_trades error %s: %s", symbol, exc)
        return pd.DataFrame()
