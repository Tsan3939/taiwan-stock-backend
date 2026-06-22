import logging

from flask import Blueprint, jsonify, request

from data_sources.stock_list import search_stocks

search_bp = Blueprint("search", __name__)
logger = logging.getLogger(__name__)


@search_bp.route("/api/stocks/search", methods=["GET"])
def search():
    try:
        query = request.args.get("q", "").strip()
        if not query:
            return jsonify({"results": []})
        results = search_stocks(query)
        return jsonify({"results": results})
    except Exception as exc:
        logger.exception("search failed")
        return jsonify({"error": str(exc), "results": []}), 500
