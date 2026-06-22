import json
import logging
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


def _json_response(payload: dict[str, Any], status: int = 200) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False),
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


def _fetch_top_volume_payload() -> dict[str, Any]:
    # 方案一：MI_INDEX20（requests）
    status, text = _http_get_text(_TWSE_MS_URL)
    app.logger.info("MI_INDEX20 status=%s body=%s", status, text[:300])
    if status == 200 and text.strip():
        payload = _safe_json_loads(text, "MI_INDEX20")
        if payload and payload.get("stat") == "OK" and payload.get("data"):
            result = _rows_from_mi_index20(payload, 30)
            if result["results"]:
                return result

    # 方案二：MI_INDEX20（curl，繞過 requests/gevent 問題）
    status2, text2 = _http_get_text_via_curl(_TWSE_MS_URL)
    app.logger.info("curl MI_INDEX20 status=%s body=%s", status2, text2[:300])
    if text2.strip():
        payload2 = _safe_json_loads(text2, "curl MI_INDEX20")
        if payload2 and payload2.get("data"):
            result2 = _rows_from_mi_index20(payload2, 30)
            if result2["results"]:
                return result2

    # 方案三：STOCK_DAY_ALL 依最近交易日依成交量排序
    for date_yyyymmdd in _recent_weekday_dates(10):
        status3, text3 = _http_get_text(
            _TWSE_DAY_ALL_URL,
            params={"response": "json", "date": date_yyyymmdd},
        )
        app.logger.info(
            "STOCK_DAY_ALL date=%s status=%s body=%s",
            date_yyyymmdd,
            status3,
            text3[:200],
        )
        if status3 != 200 or not text3.strip():
            continue
        payload3 = _safe_json_loads(text3, f"STOCK_DAY_ALL:{date_yyyymmdd}")
        if not payload3:
            continue
        result3 = _rows_from_stock_day_all(payload3, 30)
        if result3["results"]:
            return result3

    # 方案四：STOCK_DAY_ALL（curl 備援）
    for date_yyyymmdd in _recent_weekday_dates(5):
        curl_url = f"{_TWSE_DAY_ALL_URL}?response=json&date={date_yyyymmdd}"
        status4, text4 = _http_get_text_via_curl(curl_url)
        app.logger.info(
            "curl STOCK_DAY_ALL date=%s status=%s body=%s",
            date_yyyymmdd,
            status4,
            text4[:200],
        )
        if not text4.strip():
            continue
        payload4 = _safe_json_loads(text4, f"curl STOCK_DAY_ALL:{date_yyyymmdd}")
        if not payload4:
            continue
        result4 = _rows_from_stock_day_all(payload4, 30)
        if result4["results"]:
            return result4

    raise RuntimeError("All TWSE data sources failed")


def _fetch_limit_up_payload() -> dict[str, Any]:
    # 方案一：MI_INDEX20 UP（requests）
    status, text = _http_get_text(_TWSE_UP_URL)
    app.logger.info("MI_INDEX20 UP status=%s body=%s", status, text[:300])
    if status == 200 and text.strip():
        payload = _safe_json_loads(text, "MI_INDEX20 UP")
        if payload and payload.get("stat") == "OK" and payload.get("data"):
            result = _rows_from_mi_index20(payload, 999)
            if result["results"]:
                return result

    # 方案二：MI_INDEX20 UP（curl 備援）
    status2, text2 = _http_get_text_via_curl(_TWSE_UP_URL)
    app.logger.info("curl MI_INDEX20 UP status=%s body=%s", status2, text2[:300])
    if text2.strip():
        payload2 = _safe_json_loads(text2, "curl MI_INDEX20 UP")
        if payload2 and payload2.get("data"):
            result2 = _rows_from_mi_index20(payload2, 999)
            if result2["results"]:
                return result2

    # 方案三：STOCK_DAY_ALL 掃描漲停股
    for date_yyyymmdd in _recent_weekday_dates(10):
        status3, text3 = _http_get_text(
            _TWSE_DAY_ALL_URL,
            params={"response": "json", "date": date_yyyymmdd},
        )
        app.logger.info(
            "STOCK_DAY_ALL limit date=%s status=%s body=%s",
            date_yyyymmdd,
            status3,
            text3[:200],
        )
        if status3 != 200 or not text3.strip():
            continue
        payload3 = _safe_json_loads(text3, f"STOCK_DAY_ALL limit:{date_yyyymmdd}")
        if not payload3 or payload3.get("stat") != "OK":
            continue
        all_rows = _rows_from_stock_day_all(payload3, 9999)
        limit_up = [r for r in all_rows["results"] if r["change_pct"] >= 9.5]
        limit_up.sort(key=lambda x: (-x["change_pct"], x["code"]))
        if limit_up:
            return {"trade_date": all_rows["trade_date"], "results": limit_up}

    # 方案四：STOCK_DAY_ALL（curl 備援）
    for date_yyyymmdd in _recent_weekday_dates(5):
        curl_url = f"{_TWSE_DAY_ALL_URL}?response=json&date={date_yyyymmdd}"
        status4, text4 = _http_get_text_via_curl(curl_url)
        app.logger.info(
            "curl STOCK_DAY_ALL limit date=%s status=%s body=%s",
            date_yyyymmdd,
            status4,
            text4[:200],
        )
        if not text4.strip():
            continue
        payload4 = _safe_json_loads(text4, f"curl STOCK_DAY_ALL limit:{date_yyyymmdd}")
        if not payload4 or payload4.get("stat") != "OK":
            continue
        all_rows = _rows_from_stock_day_all(payload4, 9999)
        limit_up = [r for r in all_rows["results"] if r["change_pct"] >= 9.5]
        limit_up.sort(key=lambda x: (-x["change_pct"], x["code"]))
        if limit_up:
            return {"trade_date": all_rows["trade_date"], "results": limit_up}

    return {"trade_date": "", "results": []}


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
        payload = future.result(timeout=60)
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
        payload = future.result(timeout=60)
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
