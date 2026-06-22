"""均張 (NumT/ATS) 計算：成交量(張) ÷ 成交筆數。"""

import pandas as pd

from models.schemas import IndicatorPoint
from data_sources import twse_source, yahoo_source


def compute_avg_lot(
    symbol: str, start_date: str, end_date: str
) -> list[IndicatorPoint]:
    """
    計算均張指標。
    均張 = (成交股數 / 1000) / 成交筆數
    """
    # 先用 Yahoo 確認有交易資料（也可作為日期對齊參考）
    trading_dates = set(
        yahoo_source.get_trading_dates(symbol, start_date, end_date)
    )

    twse_df = twse_source.fetch_daily_trades(symbol, start_date, end_date)
    if twse_df.empty and not trading_dates:
        return []

    points: list[IndicatorPoint] = []

    if not twse_df.empty:
        for date_str, row in twse_df.iterrows():
            date_key = (
                date_str.strftime("%Y-%m-%d")
                if hasattr(date_str, "strftime")
                else str(date_str)[:10]
            )
            volume_lots = row["volume_shares"] / 1000.0
            trade_count = row["trade_count"]
            if trade_count <= 0:
                value = None
            else:
                value = round(float(volume_lots / trade_count), 2)
            points.append(IndicatorPoint(date=date_key, value=value))
    else:
        # TWSE 無資料時回傳空（由 handler 處理錯誤）
        return []

    # 若 Yahoo 有交易日但 TWSE 缺資料，可依 Yahoo 日期過濾
    if trading_dates:
        points = [p for p in points if p.date in trading_dates]

    points.sort(key=lambda p: p.date)

    last_value: float | None = None
    for point in points:
        if point.value is not None:
            last_value = point.value
        elif last_value is not None:
            point.value = last_value

    values = [p.value for p in points]
    ma_series = pd.Series(values, dtype="float64").rolling(3, min_periods=3).mean()
    for i, point in enumerate(points):
        ma = ma_series.iloc[i]
        point.ma_value = round(float(ma), 4) if pd.notna(ma) else None

    return points
