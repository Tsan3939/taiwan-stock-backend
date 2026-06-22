import logging
import os

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sock import Sock

from routes.chart import chart_bp
from routes.rankings import rankings_bp
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
app.register_blueprint(rankings_bp)
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


with app.app_context():
    ensure_stock_list_fresh()
    start_scheduler(app)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
