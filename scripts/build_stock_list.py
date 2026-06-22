"""從 TWSE ISIN 網頁抓取完整台股清單，輸出 backend/data/stock_list.json。"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_DIR / "data"
OUTPUT_PATH = DATA_DIR / "stock_list.json"

TWSE_URL = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"
TPEX_URL = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _fetch_table(url: str) -> BeautifulSoup:
    resp = requests.get(url, timeout=30)
    resp.encoding = "big5"
    return BeautifulSoup(resp.text, "html.parser")


def _parse_stocks(soup: BeautifulSoup, market: str, suffix: str) -> list[dict[str, str]]:
    table = soup.find("table", class_="h4")
    if table is None:
        logger.warning("找不到 table.h4，market=%s", market)
        return []

    stocks: list[dict[str, str]] = []
    current_section = ""

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue

        if len(cells) == 1:
            current_section = cells[0].get_text(strip=True)
            continue

        first = cells[0].get_text(strip=True)
        if not first or "\u3000" not in first:
            continue

        if current_section != "股票":
            continue

        parts = first.split("\u3000", 1)
        if len(parts) != 2:
            continue
        code, name = parts[0].strip(), parts[1].strip()
        if not code.isdigit():
            continue

        stocks.append(
            {
                "symbol": f"{code}{suffix}",
                "code": code,
                "name": name,
                "market": market,
            }
        )

    return stocks


def build_stock_list() -> list[dict[str, str]]:
    logger.info("抓取上市股票 (TWSE)...")
    twse_soup = _fetch_table(TWSE_URL)
    twse_stocks = _parse_stocks(twse_soup, "TWSE", ".TW")
    logger.info("上市股票 %d 檔", len(twse_stocks))

    logger.info("抓取上櫃股票 (TPEx)...")
    tpex_soup = _fetch_table(TPEX_URL)
    tpex_stocks = _parse_stocks(tpex_soup, "TPEx", ".TWO")
    logger.info("上櫃股票 %d 檔", len(tpex_stocks))

    all_stocks = twse_stocks + tpex_stocks
    all_stocks.sort(key=lambda s: s["code"])
    return all_stocks


def save_stock_list(stocks: list[dict[str, str]]) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(stocks, f, ensure_ascii=False, indent=2)
    logger.info("已寫入 %s（共 %d 檔）", OUTPUT_PATH, len(stocks))
    return OUTPUT_PATH


def main() -> int:
    try:
        stocks = build_stock_list()
        save_stock_list(stocks)

        codes = {s["code"] for s in stocks}
        for check in ("4306", "2243"):
            status = "OK" if check in codes else "MISSING"
            print(f"  [{status}] {check}")

        print(f"清單筆數: {len(stocks)}")
        return 0
    except Exception:
        logger.exception("建立股票清單失敗")
        return 1


if __name__ == "__main__":
    sys.exit(main())
