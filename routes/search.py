from flask import Blueprint, jsonify, request

from data_sources.stock_list import search_stocks

search_bp = Blueprint("search", __name__)


@search_bp.route("/api/stocks/search", methods=["GET"])
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"results": []})
    results = search_stocks(query)
    return jsonify({"results": results})
