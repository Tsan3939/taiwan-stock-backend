import logging
import os
import sys

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sock import Sock

from routes.chart import chart_bp
from routes.search import search_bp
from services.stock_list_scheduler import ensure_stock_list_fresh, start_scheduler
from ws.handlers import handle_websocket

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = Flask(__name__)
CORS(app)
sock = Sock(app)

USE_MOCK = os.environ.get("USE_MOCK_DATA", "false").lower() == "true"

app.register_blueprint(search_bp)
# rankings 路由改由下方 app.route 直接註冊（診斷遞迴問題，避免 blueprint 舊版衝突）
app.register_blueprint(chart_bp)


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "route not found", "path": request.path}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": str(e)}), 500


@sock.route("/ws")
def websocket_route(ws):
    handle_websocket(ws, use_mock=USE_MOCK)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/stocks/top_volume", methods=["GET"])
@app.route("/api/stocks/top-volume", methods=["GET"])
def route_top_volume():
    """量大排行 — 最簡診斷版：不呼叫任何 helper，直接 requests.get。"""
    print("=== route_top_volume called ===", flush=True)
    print(f"recursion limit: {sys.getrecursionlimit()}", flush=True)
    try:
        response = requests.get(
            "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX20"
            "?response=json&type=MS",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        response.raise_for_status()
        raw = response.json()
        print(f"TWSE keys: {list(raw.keys())}", flush=True)
        return jsonify({
            "ok": True,
            "keys": list(raw.keys()),
            "stat": raw.get("stat"),
            "row_count": len(raw.get("data") or []),
        })
    except RecursionError:
        print("=== RecursionError in route_top_volume ===", flush=True)
        return jsonify({
            "error": "recursion detected in route_top_volume",
            "trade_date": "",
            "results": [],
        }), 500
    except Exception as exc:
        print(f"=== route_top_volume error: {exc} ===", flush=True)
        return jsonify({
            "error": str(exc),
            "trade_date": "",
            "results": [],
        }), 500


@app.route("/api/stocks/limit_up", methods=["GET"])
@app.route("/api/stocks/limit-up", methods=["GET"])
def route_limit_up():
    """漲停股 — 最簡診斷版。"""
    print("=== route_limit_up called ===", flush=True)
    print(f"recursion limit: {sys.getrecursionlimit()}", flush=True)
    try:
        response = requests.get(
            "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX20"
            "?response=json&type=UP",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        response.raise_for_status()
        raw = response.json()
        print(f"TWSE keys: {list(raw.keys())}", flush=True)
        return jsonify({
            "ok": True,
            "keys": list(raw.keys()),
            "stat": raw.get("stat"),
            "row_count": len(raw.get("data") or []),
        })
    except RecursionError:
        print("=== RecursionError in route_limit_up ===", flush=True)
        return jsonify({
            "error": "recursion detected in route_limit_up",
            "trade_date": "",
            "results": [],
        }), 500
    except Exception as exc:
        print(f"=== route_limit_up error: {exc} ===", flush=True)
        return jsonify({
            "error": str(exc),
            "trade_date": "",
            "results": [],
        }), 500


with app.app_context():
    ensure_stock_list_fresh()
    start_scheduler(app)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
