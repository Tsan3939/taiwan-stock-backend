# 必須是整個檔案的第一行，在任何其他 import 之前
from gevent import monkey

monkey.patch_all()

import json
import logging
import math
import os
import subprocess
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

from flask import Flask, Response, jsonify, request
from flask_cors import CORS
from flask_sock import Sock

from data_sources.yahoo_tw_rankings import (
    fetch_limit_up as _fetch_limit_up_yahoo_tw,
    fetch_top_volume as _fetch_top_volume_yahoo_tw,
)
from data_sources.yfinance_rankings import (
    build_limit_up as _build_limit_up_yfinance,
    build_top_volume as _build_top_volume_yfinance,
    is_twse_blocked,
    mark_twse_blocked,
    should_skip_twse,
)
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

# 量大排行 / 漲停股 API 回傳筆數上限
RANKING_DISPLAY_LIMIT = 10

_TWSE_MS_URL = (
    "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX20"
    "?response=json&type=MS"
)
_TWSE_UP_URL = (
    "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX20"
    "?response=json&type=UP"
)
_TWSE_DAY_ALL_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL"

# gevent/gunicorn worker 下在 greenlet 做 HTTPS 或 yfinance 可能 RecursionError
_http_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="blocking-http")

app.register_blueprint(search_bp)


def _twse_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.twse.com.tw/",
    }


def _sanitize_json_value(value: Any) -> Any:
    """NaN/Infinity 不是標準 JSON，Dart jsonDecode 會失敗。"""
    if value is None:
        return None
    if isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if hasattr(value, "item"):
        try:
            return _sanitize_json_value(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, dict):
        return {k: _sanitize_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_json_value(v) for v in value]
    try:
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        return number
    except (TypeError, ValueError):
        return value


def _json_response(payload: dict[str, Any], status: int = 200) -> Response:
    safe = _sanitize_json_value(payload)
    return Response(
        json.dumps(safe, ensure_ascii=False, allow_nan=False),
        status=status,
        mimetype="application/json",
    )


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


def _parse_change(value: Any) -> float:
    text = str(value).strip().replace(",", "") if value is not None else ""
    if not text or text in ("--", "X", "-"):
        return 0.0
    if text.startswith("+"):
        text = text[1:]
    try:
        return float(text)
    except ValueError:
        return 0.0


def _recent_weekday_dates(max_days: int = 10) -> list[str]:
    dates: list[str] = []
    current = datetime.now()
    while len(dates) < max_days:
        if current.weekday() < 5:
            dates.append(current.strftime("%Y%m%d"))
        current -= timedelta(days=1)
    return dates


def _http_get_text(url: str, params: dict[str, str] | None = None) -> tuple[int, str]:
    """在真實 thread 內做 HTTP GET，回傳 (status_code, text)。"""

    def _blocking() -> tuple[int, str]:
        import requests as http_client

        resp = http_client.get(
            url,
            params=params,
            headers=_twse_headers(),
            timeout=15,
        )
        return resp.status_code, resp.text or ""

    future = _http_executor.submit(_blocking)
    return future.result(timeout=20)


def _http_get_text_via_curl(url: str) -> tuple[int, str]:
    proc = subprocess.run(
        [
            "curl",
            "-sS",
            "-w",
            "\n%{http_code}",
            "--max-time",
            "15",
            "-H",
            f"User-Agent: {_twse_headers()['User-Agent']}",
            "-H",
            "Referer: https://www.twse.com.tw/",
            url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout or ""
    if "\n" in output:
        body, code_text = output.rsplit("\n", 1)
        try:
            return int(code_text), body
        except ValueError:
            return 0, output
    return proc.returncode, output


def _safe_json_loads(text: str, label: str) -> dict[str, Any] | None:
    body = (text or "").strip()
    if not body:
        app.logger.warning("%s: empty response body", label)
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        app.logger.warning(
            "%s: JSON decode failed (%s), body=%s",
            label,
            exc,
            body[:200],
        )
        return None
    if not isinstance(payload, dict):
        app.logger.warning("%s: payload is not object: %s", label, type(payload))
        return None
    return payload


def _normalize_rank_row(
    code: str,
    name: str,
    close: Any,
    change: Any,
    volume: Any,
    change_pct: float | None = None,
) -> dict[str, Any] | None:
    code = str(code).strip()
    if not code or code.startswith("00") or not code.isdigit():
        return None
    close_f = _parse_number(close)
    change_f = _parse_change(change)
    volume_i = int(_parse_number(volume))
    if change_pct is None and close_f > 0 and change_f != 0.0:
        prev = close_f - change_f
        change_pct = round(change_f / prev * 100, 2) if prev > 0 else 0.0
    return {
        "symbol": f"{code}.TW",
        "code": code,
        "name": str(name).strip(),
        "close": close_f,
        "change": change_f,
        "change_pct": change_pct or 0.0,
        "volume": volume_i,
    }


def _rows_from_mi_index20(payload: dict[str, Any], limit: int) -> dict[str, Any]:
    import re

    stat = payload.get("stat")
    if stat not in (None, "OK"):
        stat_text = str(stat)
        if "沒有符合" in stat_text or "抱歉" in stat_text:
            return {"trade_date": "", "results": []}
        raise RuntimeError(stat_text)

    fields = payload.get("fields") or []
    rows = payload.get("data") or []
    title = str(payload.get("title") or "")
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
        item = _normalize_rank_row(
            mapped.get("證券代號", ""),
            mapped.get("證券名稱", ""),
            mapped.get("收盤價"),
            mapped.get("漲跌價差"),
            mapped.get("成交股數"),
            _parse_number(str(mapped.get("漲跌幅", "")).replace("%", "")) or None,
        )
        if item:
            results.append(item)
        if len(results) >= limit:
            break

    return {"trade_date": trade_date, "results": results}


def _rows_from_stock_day_all(payload: dict[str, Any], limit: int) -> dict[str, Any]:
    if payload.get("stat") != "OK":
        return {"trade_date": "", "results": []}

    date_raw = str(payload.get("date") or "")
    if len(date_raw) == 8 and date_raw.isdigit():
        trade_date = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
    else:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    results: list[dict[str, Any]] = []
    for row in payload.get("data") or []:
        if not row or len(row) < 9:
            continue
        item = _normalize_rank_row(
            row[0],
            row[1],
            row[7] if len(row) > 7 else row[2],
            row[8] if len(row) > 8 else 0,
            row[2],
        )
        if item and item["volume"] > 0:
            results.append(item)

    results.sort(key=lambda x: x["volume"], reverse=True)
    return {"trade_date": trade_date, "results": results[:limit]}


def _try_twse_mi_index20(url: str, label: str, limit: int) -> dict[str, Any] | None:
    """單次 TWSE MI_INDEX20；被封鎖時標記並跳過後續 TWSE 嘗試。"""
    if should_skip_twse():
        return None

    status, text = _http_get_text(url)
    app.logger.info("%s status=%s body=%s", label, status, text[:300])
    if is_twse_blocked(status, text):
        mark_twse_blocked()
        return None

    if status != 200 or not text.strip():
        return None

    payload = _safe_json_loads(text, label)
    if not payload or payload.get("stat") not in (None, "OK") or not payload.get("data"):
        return None

    result = _rows_from_mi_index20(payload, limit)
    return result if result["results"] else None


def _try_twse_stock_day_all(limit: int) -> dict[str, Any] | None:
    """TWSE 未封鎖時，僅試最近一個交易日 STOCK_DAY_ALL。"""
    if should_skip_twse():
        return None

    dates = _recent_weekday_dates(1)
    if not dates:
        return None

    date_yyyymmdd = dates[0]
    status, text = _http_get_text(
        _TWSE_DAY_ALL_URL,
        params={"response": "json", "date": date_yyyymmdd},
    )
    app.logger.info(
        "STOCK_DAY_ALL date=%s status=%s body=%s",
        date_yyyymmdd,
        status,
        text[:200],
    )
    if is_twse_blocked(status, text):
        mark_twse_blocked()
        return None
    if status != 200 or not text.strip():
        return None

    payload = _safe_json_loads(text, f"STOCK_DAY_ALL:{date_yyyymmdd}")
    if not payload:
        return None

    if limit >= 999:
        all_rows = _rows_from_stock_day_all(payload, 9999)
        limit_up = [r for r in all_rows["results"] if r["change_pct"] >= 9.5]
        limit_up.sort(key=lambda x: (-x["change_pct"], x["code"]))
        if not limit_up:
            return None
        return {
            "trade_date": all_rows["trade_date"],
            "results": limit_up[:RANKING_DISPLAY_LIMIT],
        }

    result = _rows_from_stock_day_all(payload, limit)
    return result if result["results"] else None


def _try_yahoo_tw_top() -> dict[str, Any] | None:
    try:
        payload = _fetch_top_volume_yahoo_tw(RANKING_DISPLAY_LIMIT)
        if payload.get("results"):
            app.logger.info("top_volume 使用 Yahoo 奇摩股市排行")
            return payload
    except Exception:
        app.logger.exception("Yahoo TW top_volume 失敗")
    return None


def _try_yahoo_tw_limit_up() -> dict[str, Any] | None:
    try:
        payload = _fetch_limit_up_yahoo_tw(RANKING_DISPLAY_LIMIT)
        if payload.get("results"):
            app.logger.info("limit_up 使用 Yahoo 奇摩股市漲幅排行")
            return payload
    except Exception:
        app.logger.exception("Yahoo TW limit_up 失敗")
    return None


def _fetch_top_volume_payload() -> dict[str, Any]:
    result = _try_twse_mi_index20(_TWSE_MS_URL, "MI_INDEX20", RANKING_DISPLAY_LIMIT)
    if result:
        return result

    result = _try_yahoo_tw_top()
    if result:
        return result

    result = _try_twse_stock_day_all(RANKING_DISPLAY_LIMIT)
    if result:
        return result

    app.logger.info("top_volume 使用 yfinance 備援")
    return _build_top_volume_yfinance(RANKING_DISPLAY_LIMIT)


def _fetch_limit_up_payload() -> dict[str, Any]:
    result = _try_twse_mi_index20(_TWSE_UP_URL, "MI_INDEX20 UP", RANKING_DISPLAY_LIMIT)
    if result:
        return result

    result = _try_yahoo_tw_limit_up()
    if result:
        return result

    result = _try_twse_stock_day_all(9999)
    if result:
        return result

    app.logger.info("limit_up 使用 yfinance 備援")
    return _build_limit_up_yfinance(RANKING_DISPLAY_LIMIT)


def _compute_chart_blocking(symbol: str, start_date: str, end_date: str) -> list[dict]:
    from indicators.chart_data import compute_chart_data

    return compute_chart_data(symbol, start_date, end_date)


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
    ]


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
    print("=== route_top_volume called ===", flush=True)
    print(f"recursion limit: {sys.getrecursionlimit()}", flush=True)
    try:
        future = _http_executor.submit(_fetch_top_volume_payload)
        payload = future.result(timeout=150)
        return _json_response(payload)
    except RecursionError:
        app.logger.exception("route_top_volume RecursionError")
        return _json_response({
            "error": "recursion detected in route_top_volume",
            "trade_date": "",
            "results": [],
        }, 500)
    except Exception as exc:
        app.logger.error("route_top_volume error: %s", traceback.format_exc())
        print(traceback.format_exc(), flush=True)
        return _json_response({
            "error": str(exc),
            "trade_date": "",
            "results": [],
        }, 500)


@app.route("/api/stocks/limit_up", methods=["GET"])
@app.route("/api/stocks/limit-up", methods=["GET"])
def route_limit_up():
    print("=== route_limit_up called ===", flush=True)
    try:
        future = _http_executor.submit(_fetch_limit_up_payload)
        payload = future.result(timeout=150)
        return _json_response(payload)
    except RecursionError:
        app.logger.exception("route_limit_up RecursionError")
        return _json_response({
            "error": "recursion detected in route_limit_up",
            "trade_date": "",
            "results": [],
        }, 500)
    except Exception as exc:
        app.logger.error("route_limit_up error: %s", traceback.format_exc())
        return _json_response({
            "error": str(exc),
            "trade_date": "",
            "results": [],
        }, 500)


@app.route("/api/stocks/chart", methods=["GET"])
def route_chart():
    symbol = request.args.get("symbol", "").strip()
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()

    print(
        f"=== route_chart called symbol={symbol} {start_date}~{end_date} ===",
        flush=True,
    )

    try:
        if not symbol or not start_date or not end_date:
            return _json_response({"error": "缺少 symbol、start_date 或 end_date"}, 400)

        if USE_MOCK:
            return _json_response({"symbol": symbol, "data": _mock_chart_data()})

        future = _http_executor.submit(
            _compute_chart_blocking,
            symbol,
            start_date,
            end_date,
        )
        data = future.result(timeout=120)
        if not data:
            return _json_response(
                {"error": "查無此股票代碼或資料來源暫時無法取得"},
                404,
            )
        return _json_response({"symbol": symbol, "data": data})
    except RecursionError:
        detail = traceback.format_exc()
        app.logger.error("route_chart RecursionError: %s", detail)
        return _json_response({"error": "recursion detected in route_chart", "detail": detail}, 500)
    except Exception as exc:
        detail = traceback.format_exc()
        app.logger.error("route_chart error: %s", detail)
        print(detail, flush=True)
        return _json_response({"error": str(exc), "detail": detail}, 500)


with app.app_context():
    ensure_stock_list_fresh()
    start_scheduler(app)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
