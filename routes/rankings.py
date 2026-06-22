import logging
import traceback

from flask import Blueprint, current_app, jsonify

from data_sources.twse_index20 import fetch_limit_up, fetch_top_volume

rankings_bp = Blueprint("rankings", __name__)
logger = logging.getLogger(__name__)


@rankings_bp.route("/api/stocks/top_volume", methods=["GET"])
@rankings_bp.route("/api/stocks/top-volume", methods=["GET"])
def top_volume():
    try:
        data = fetch_top_volume(limit=30)
        return jsonify(data)
    except Exception as exc:
        logger.exception("top_volume error")
        current_app.logger.error("top_volume error: %s", exc)
        return jsonify({"error": str(exc), "trade_date": "", "results": []}), 500


@rankings_bp.route("/api/stocks/limit_up", methods=["GET"])
@rankings_bp.route("/api/stocks/limit-up", methods=["GET"])
def limit_up():
    try:
        data = fetch_limit_up()
        return jsonify(data)
    except Exception as exc:
        logger.exception("limit_up error")
        current_app.logger.error("limit_up error: %s", exc)
        return jsonify({"error": str(exc), "trade_date": "", "results": []}), 500
