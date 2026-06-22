"""台股代碼/名稱對照表，從 stock_list.json 載入，支援搜尋與即時備援。"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent.parent
STOCK_LIST_PATH = BACKEND_DIR / "data" / "stock_list.json"

_lock = threading.Lock()
_stock_list: list[dict[str, str]] = []


def load_stock_list_from_file() -> list[dict[str, str]]:
    if not STOCK_LIST_PATH.exists():
        logger.warning("stock_list.json 不存在: %s", STOCK_LIST_PATH)
        return []
    with STOCK_LIST_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    logger.info("已載入 stock_list.json，共 %d 檔", len(data))
    return data


def reload_stock_list() -> int:
    """重新載入 JSON 到記憶體，回傳筆數。"""
    global _stock_list
    with _lock:
        _stock_list = load_stock_list_from_file()
    return len(_stock_list)


def get_stock_list() -> list[dict[str, str]]:
    with _lock:
        if not _stock_list:
            _stock_list.extend(load_stock_list_from_file())
        return list(_stock_list)


def add_stock_to_memory(stock: dict[str, str]) -> None:
    with _lock:
        if not any(s["symbol"] == stock["symbol"] for s in _stock_list):
            _stock_list.append(stock)
            logger.info(
                "即時備援：新增 %s %s (%s) 至記憶體清單",
                stock["code"],
                stock["name"],
                stock["symbol"],
            )


def _score_stock(stock: dict[str, str], q: str) -> int:
    code = stock["code"].lower()
    name = stock["name"].lower()
    symbol = stock["symbol"].lower()

    if code == q or name == q:
        return 100
    if code.startswith(q):
        return 80
    if name.startswith(q):
        return 70
    if q in code or q in name or q in symbol:
        return 50
    return 0


def search_stocks(query: str, limit: int = 20) -> list[dict[str, str]]:
    q = query.strip().lower()
    if not q:
        return []

    stocks = get_stock_list()
    results: list[tuple[int, dict[str, str]]] = []
    for stock in stocks:
        score = _score_stock(stock, q)
        if score > 0:
            results.append((score, stock))

    results.sort(key=lambda x: (-x[0], x[1]["code"]))
    matched = [s for _, s in results[:limit]]

    if not matched and re.fullmatch(r"\d{4}", q):
        fallback = lookup_stock_by_code(q.upper())
        if fallback:
            add_stock_to_memory(fallback)
            matched = [fallback]

    return matched


def lookup_stock_by_code(code: str) -> dict[str, str] | None:
    """即時查詢 TWSE ISIN 單檔，確認股票是否存在。"""
    url = f"https://isin.twse.com.tw/isin/single_main.jsp?owncode={code}"
    try:
        resp = requests.get(url, timeout=15)
        resp.encoding = "big5"
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.find("table", class_="h4")
        if table is None:
            return None

        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if not cells:
                continue
            first = cells[0].get_text(strip=True)
            if "\u3000" not in first:
                continue
            parts = first.split("\u3000", 1)
            if len(parts) != 2:
                continue
            row_code, name = parts[0].strip(), parts[1].strip()
            if row_code != code:
                continue

            sec_type = ""
            for cell in cells:
                text = cell.get_text(strip=True)
                if text in ("股票", "ETF", "權證", "債券"):
                    sec_type = text
                    break
            if sec_type and sec_type != "股票":
                return None

            market = "TWSE"
            suffix = ".TW"
            page_text = soup.get_text()
            if "上櫃" in page_text or "TPEx" in page_text:
                market = "TPEx"
                suffix = ".TWO"

            stock = {
                "symbol": f"{code}{suffix}",
                "code": code,
                "name": name,
                "market": market,
            }
            logger.info("即時備援查詢成功: %s %s (%s)", code, name, stock["symbol"])
            return stock
    except Exception:
        logger.exception("即時備援查詢失敗: code=%s", code)
    return None
