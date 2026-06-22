"""股票清單排程與啟動時自動更新。"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent.parent
STOCK_LIST_PATH = BACKEND_DIR / "data" / "stock_list.json"
SCRIPTS_DIR = BACKEND_DIR / "scripts"

TAIPEI_TZ = timezone(timedelta(hours=8))


def _import_build_module():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    from build_stock_list import build_stock_list, save_stock_list

    return build_stock_list, save_stock_list


def rebuild_stock_list() -> int:
    """執行 build_stock_list 邏輯並重新載入記憶體清單。"""
    from data_sources.stock_list import reload_stock_list

    build_stock_list, save_stock_list = _import_build_module()
    logger.info("開始重建股票清單...")
    stocks = build_stock_list()
    save_stock_list(stocks)
    count = reload_stock_list()
    logger.info("股票清單重建完成，共 %d 檔", count)
    return count


def stock_list_is_stale(max_age_hours: float = 24) -> bool:
    if not STOCK_LIST_PATH.exists():
        logger.info("stock_list.json 不存在，需要重建")
        return True
    mtime = datetime.fromtimestamp(STOCK_LIST_PATH.stat().st_mtime, tz=TAIPEI_TZ)
    age = datetime.now(TAIPEI_TZ) - mtime
    stale = age > timedelta(hours=max_age_hours)
    if stale:
        logger.info(
            "stock_list.json 已超過 %.0f 小時（最後更新: %s），需要重建",
            max_age_hours,
            mtime.strftime("%Y-%m-%d %H:%M"),
        )
    return stale


def ensure_stock_list_fresh() -> None:
    """啟動時若清單過舊則自動重建。"""
    if stock_list_is_stale():
        try:
            rebuild_stock_list()
        except Exception:
            logger.exception("啟動時重建股票清單失敗")
    else:
        from data_sources.stock_list import reload_stock_list

        count = reload_stock_list()
        logger.info("stock_list.json 仍在有效期內，已載入 %d 檔", count)


def start_scheduler(app) -> None:
    """註冊每日台股開盤前 08:00（台北時間）自動重建排程。"""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("APScheduler 未安裝，跳過排程註冊")
        return

    scheduler = BackgroundScheduler(timezone="Asia/Taipei")

    def _job():
        with app.app_context():
            try:
                rebuild_stock_list()
            except Exception:
                logger.exception("排程重建股票清單失敗")

    scheduler.add_job(
        _job,
        trigger=CronTrigger(hour=8, minute=0, timezone="Asia/Taipei"),
        id="rebuild_stock_list",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("已註冊排程：每日 08:00（台北時間）重建股票清單")

    @app.teardown_appcontext
    def _shutdown_scheduler(exception):
        pass

    import atexit

    atexit.register(lambda: scheduler.shutdown(wait=False))
