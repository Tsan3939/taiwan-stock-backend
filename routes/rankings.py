import logging

from flask import Blueprint, jsonify

from data_sources.market_rankings import get_limit_up, get_top_volume

rankings_bp = Blueprint("rankings", __name__)
logger = logging.getLogger(__name__)


@rankings_bp.route("/api/stocks/top-volume", methods=["GET"])
def top_volume():
    try:
        data = get_top_volume(limit=25)
    except Exception as exc:
        logger.exception("top-volume failed")
        return jsonify({"error": str(exc), "trade_date": "", "results": []}), 500
    return jsonify(data)


@rankings_bp.route("/api/stocks/limit-up", methods=["GET"])
def limit_up():
    try:
        data = get_limit_up()
    except Exception as exc:
        logger.exception("limit-up failed")
        return jsonify({"error": str(exc), "trade_date": "", "results": []}), 500
    return jsonify(data)
