"""圖表資料記憶體快取：同一股票避免重複抓取長區間資料。"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

CACHE_TTL = timedelta(minutes=30)


class ChartDataCache:
    """以 symbol 為 key，快取含緩衝期的完整計算結果。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}

    def get(
        self, symbol: str, fetch_start: str, end_date: str
    ) -> list[dict] | None:
        with self._lock:
            entry = self._entries.get(symbol)
            if entry is None:
                return None
            if datetime.now() - entry["cached_at"] > CACHE_TTL:
                del self._entries[symbol]
                logger.info("快取過期，清除 symbol=%s", symbol)
                return None
            if entry["fetch_start"] <= fetch_start and entry["end_date"] >= end_date:
                logger.debug(
                    "快取命中 symbol=%s cached=%s~%s",
                    symbol,
                    entry["fetch_start"],
                    entry["end_date"],
                )
                return list(entry["rows"])
            return None

    def put(
        self,
        symbol: str,
        fetch_start: str,
        end_date: str,
        rows: list[dict],
    ) -> None:
        with self._lock:
            existing = self._entries.get(symbol)
            if existing and datetime.now() - existing["cached_at"] <= CACHE_TTL:
                by_date = {r["date"]: r for r in existing["rows"]}
                by_date.update({r["date"]: r for r in rows})
                merged_rows = [by_date[d] for d in sorted(by_date)]
                self._entries[symbol] = {
                    "fetch_start": min(existing["fetch_start"], fetch_start),
                    "end_date": max(existing["end_date"], end_date),
                    "rows": merged_rows,
                    "cached_at": datetime.now(),
                }
                logger.info(
                    "快取合併 symbol=%s range=%s~%s (%d 筆)",
                    symbol,
                    self._entries[symbol]["fetch_start"],
                    self._entries[symbol]["end_date"],
                    len(merged_rows),
                )
                return

            self._entries[symbol] = {
                "fetch_start": fetch_start,
                "end_date": end_date,
                "rows": rows,
                "cached_at": datetime.now(),
            }
            logger.info(
                "快取寫入 symbol=%s range=%s~%s (%d 筆)",
                symbol,
                fetch_start,
                end_date,
                len(rows),
            )


chart_cache = ChartDataCache()
