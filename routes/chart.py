import logging
import os
import traceback

from flask import Blueprint, current_app, jsonify, request

from indicators.chart_data import compute_chart_data

chart_bp = Blueprint("chart", __name__)
logger = logging.getLogger(__name__)
USE_MOCK = os.environ.get("USE_MOCK_DATA", "false").lower() == "true"


def _mock_chart_data() -> list[dict]:
    return [
        {
            "date": "2026-01-20",
            "open": 42.0,
            "high": 43.5,
            "low": 41.5,
            "close": 43.0,
            "volume": 12500000,
            "avg_lot": 1.85,
        },
        {
            "date": "2026-01-21",
            "open": 43.0,
            "high": 44.2,
            "low": 42.8,
            "close": 44.0,
            "volume": 15200000,
            "avg_lot": 2.10,
        },
        {
            "date": "2026-01-22",
            "open": 44.0,
            "high": 45.0,
            "low": 43.5,
            "close": 44.5,
            "volume": 9800000,
            "avg_lot": 1.72,
        },
    ]


@chart_bp.route("/api/stocks/chart", methods=["GET"])
def chart():
    symbol = request.args.get("symbol", "").strip()
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()

    try:
        if not symbol or not start_date or not end_date:
            return jsonify({"error": "缺少 symbol、start_date 或 end_date"}), 400

        if USE_MOCK:
            return jsonify({"symbol": symbol, "data": _mock_chart_data()})

        data = compute_chart_data(symbol, start_date, end_date)
        if not data:
            return jsonify({"error": "查無此股票代碼或資料來源暫時無法取得"}), 404

        return jsonify({"symbol": symbol, "data": data})
    except Exception as exc:
        detail = traceback.format_exc()
        logger.exception(
            "stock chart error symbol=%s start=%s end=%s",
            symbol,
            start_date,
            end_date,
        )
        current_app.logger.error("stock data error: %s", detail)
        return jsonify({"error": str(exc), "detail": detail}), 500
