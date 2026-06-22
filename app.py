import json
import logging
import os
import sys
import traceback
import urllib.error
import urllib.request
from typing import Any

from flask import Flask, Response, jsonify, request
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

_TWSE_UA = {"User-Agent": "Mozilla/5.0"}
_TWSE_MS_URL = (
    "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX20"
    "?response=json&type=MS"
)
_TWSE_UP_URL = (
    "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX20"
    "?response=json&type=UP"
)

app.register_blueprint(search_bp)
app.register_blueprint(chart_bp)


def _json_response(payload: dict[str, Any], status: int = 200) -> Response:
    """用 stdlib json + Response，避開 jsonify 在部分 WSGI 環境的異常。"""
    return Response(
        json.dumps(payload, ensure_ascii=False),
        status=status,
        mimetype="application/json",
    )


def _fetch_twse_json(url: str) -> dict[str, Any]:
    """取 TWSE JSON。手動逐次 follow redirect，避免 requests/gevent 內部遞迴。"""
    import ssl

    # 1) 優先 stdlib urllib（Linux/Render 通常正常）
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers=_TWSE_UA)
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            body = resp.read().decode(charset, errors="replace")
        payload = json.loads(body)
        if payload.get("fields") or payload.get("data"):
            return payload
    except (urllib.error.URLError, ssl.SSLError, json.JSONDecodeError) as exc:
        app.logger.warning("urllib TWSE fetch fallback to requests: %s", exc)

    # 2) requests 手動 redirect（allow_redirects=False，最多 5 次）
    import requests as http_client

    current = url
    for _ in range(5):
        resp = http_client.get(
            current,
            headers=_TWSE_UA,
            timeout=15,
            allow_redirects=False,
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            if not location:
                break
            current = location
            continue
        resp.raise_for_status()
        return resp.json()

    raise RuntimeError("TWSE redirect loop or empty response")


def _parse_number(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "")
    if not text or text in ("--", "X", "-"):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _parse_mi_index20_payload(raw: dict[str, Any], limit: int | None) -> dict[str, Any]:
    import re
    from datetime import datetime

    if raw.get("stat") not in (None, "OK"):
        stat = str(raw.get("stat") or "")
        # 非交易日或無資料時 TWSE 回傳中文 stat，不視為伺服器錯誤
        if "沒有符合" in stat or "抱歉" in stat:
            return {"trade_date": "", "results": []}
        raise RuntimeError(stat or "TWSE 回傳非 OK")

    fields = raw.get("fields") or []
    rows = raw.get("data") or []
    title = str(raw.get("title") or "")
    match = re.search(r"(\d+)年(\d+)月(\d+)日", title)
    if match:
        roc, month, day = match.groups()
        trade_date = f"{int(roc) + 1911:04d}-{month}-{day}"
    else:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    results: list[dict[str, Any]] = []
    for row in rows:
        if not row:
            continue
        mapped = dict(zip(fields, row))
        code = str(mapped.get("證券代號", "")).strip()
        if not code or code.startswith("00"):
            continue
        close = _parse_number(mapped.get("收盤價"))
        change = _parse_number(mapped.get("漲跌價差"))
        change_pct_raw = str(mapped.get("漲跌幅", "")).replace(",", "").replace("%", "")
        change_pct = float(change_pct_raw) if change_pct_raw not in ("", "--") else 0.0
        if change_pct == 0.0 and close > 0 and change != 0.0:
            prev = close - change
            if prev > 0:
                change_pct = round(change / prev * 100, 2)
        results.append({
            "symbol": f"{code}.TW",
            "code": code,
            "name": str(mapped.get("證券名稱", "")).strip(),
            "close": close,
            "change": change,
            "change_pct": change_pct,
            "volume": int(_parse_number(mapped.get("成交股數"))),
        })
        if limit is not None and len(results) >= limit:
            break

    return {"trade_date": trade_date, "results": results}


def _ranking_route(url: str, limit: int | None, route_name: str):
    step = "enter"
    try:
        step = "fetch_twse"
        raw = _fetch_twse_json(url)
        step = "parse"
        payload = _parse_mi_index20_payload(raw, limit)
        step = "respond"
        return _json_response(payload)
    except RecursionError:
        app.logger.error("%s RecursionError at step=%s", route_name, step)
        return _json_response({
            "error": f"recursion at step {step} in {route_name}",
            "trade_date": "",
            "results": [],
        }, 500)
    except urllib.error.URLError as exc:
        app.logger.error("%s URLError at step=%s: %s", route_name, step, exc)
        return _json_response({
            "error": f"TWSE 連線失敗 ({step}): {exc}",
            "trade_date": "",
            "results": [],
        }, 500)
    except Exception as exc:
        app.logger.error(
            "%s failed at step=%s: %s",
            route_name,
            step,
            traceback.format_exc(),
        )
        return _json_response({
            "error": str(exc),
            "step": step,
            "trade_date": "",
            "results": [],
        }, 500)


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
    app.logger.info("route_top_volume enter recursion_limit=%s", sys.getrecursionlimit())
    return _ranking_route(_TWSE_MS_URL, 30, "route_top_volume")


@app.route("/api/stocks/limit_up", methods=["GET"])
@app.route("/api/stocks/limit-up", methods=["GET"])
def route_limit_up():
    app.logger.info("route_limit_up enter recursion_limit=%s", sys.getrecursionlimit())
    return _ranking_route(_TWSE_UP_URL, None, "route_limit_up")


with app.app_context():
    ensure_stock_list_fresh()
    start_scheduler(app)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
