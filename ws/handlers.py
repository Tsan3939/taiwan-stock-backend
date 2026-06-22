"""WebSocket 訊息處理與指標 dispatch。"""

import json
import logging
from typing import Any

from indicators.avg_lot import compute_avg_lot
from models.schemas import (
    IndicatorRequest,
    error_response,
    indicator_response,
)

logger = logging.getLogger(__name__)

# 簡單記憶體快取：key -> list[IndicatorPoint]
_cache: dict[str, list] = {}


def _cache_key(indicator: str, symbol: str, start: str, end: str) -> str:
    return f"{indicator}:{symbol}:{start}:{end}"


def _get_mock_avg_lot(symbol: str, start_date: str, end_date: str) -> list:
    """開發用假資料。"""
    from models.schemas import IndicatorPoint

    return [
        IndicatorPoint(date="2026-02-10", value=1.81),
        IndicatorPoint(date="2026-02-11", value=2.22),
        IndicatorPoint(date="2026-02-12", value=1.95),
        IndicatorPoint(date="2026-02-13", value=2.05),
        IndicatorPoint(date="2026-02-14", value=1.73),
        IndicatorPoint(date="2026-02-17", value=2.10),
        IndicatorPoint(date="2026-02-18", value=1.88),
        IndicatorPoint(date="2026-02-19", value=2.35),
        IndicatorPoint(date="2026-02-20", value=1.92),
    ]


def handle_message(raw: str, use_mock: bool = False) -> str:
    """解析 WebSocket 訊息並回傳 JSON 字串回應。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return json.dumps(error_response("無效的 JSON 格式"), ensure_ascii=False)

    req = IndicatorRequest.from_dict(data)

    if req.action != "get_indicator":
        return json.dumps(
            error_response(f"不支援的 action: {req.action}"), ensure_ascii=False
        )

    if not req.symbol or not req.start_date or not req.end_date:
        return json.dumps(
            error_response("缺少 symbol、start_date 或 end_date"), ensure_ascii=False
        )

    cache_key = _cache_key(
        req.indicator, req.symbol, req.start_date, req.end_date
    )

    try:
        if req.indicator == "avg_lot":
            if use_mock:
                points = _get_mock_avg_lot(
                    req.symbol, req.start_date, req.end_date
                )
            else:
                if cache_key in _cache:
                    points = _cache[cache_key]
                else:
                    points = compute_avg_lot(
                        req.symbol, req.start_date, req.end_date
                    )
                    if points:
                        _cache[cache_key] = points

            if not points:
                return json.dumps(
                    error_response("查無此股票代碼或資料來源暫時無法取得"),
                    ensure_ascii=False,
                )

            return json.dumps(
                indicator_response(req.indicator, req.symbol, points),
                ensure_ascii=False,
            )
        else:
            return json.dumps(
                error_response(f"不支援的指標: {req.indicator}"),
                ensure_ascii=False,
            )
    except Exception as exc:
        logger.exception("處理指標請求失敗")
        return json.dumps(
            error_response(f"資料處理失敗: {exc}"), ensure_ascii=False
        )


def handle_websocket(ws, use_mock: bool = False) -> None:
    """WebSocket 連線主迴圈。"""
    while True:
        try:
            message = ws.receive()
            if message is None:
                break
            response = handle_message(message, use_mock=use_mock)
            ws.send(response)
        except Exception as exc:
            logger.exception("WebSocket 錯誤")
            try:
                ws.send(
                    json.dumps(
                        error_response(f"伺服器錯誤: {exc}"),
                        ensure_ascii=False,
                    )
                )
            except Exception:
                break
            break
