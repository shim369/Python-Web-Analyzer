import logging
import logging.config
from datetime import datetime
from pathlib import Path


def setup_logger(level: int = logging.INFO) -> None:
    """ロガーの初期設定。

    fileConfig ではフィルターの詳細制御ができない制限を考慮し、dictConfig を採用。
    """
    log_directory = Path("logs")
    log_directory.mkdir(parents=True, exist_ok=True)
    log_file = log_directory / f"app_{datetime.now().strftime('%Y%m%d')}.log"

    config = {
        "version": 1,
        # 既存のロガー（streamlit等）を勝手に無効化せず保護する
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s [%(levelname)s] %(threadName)s %(name)s:%(lineno)d - %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
                "level": level,
            },
            "file": {
                "class": "logging.FileHandler",
                "filename": str(log_file),
                "encoding": "utf-8",
                "mode": "a",
                "formatter": "standard",
                "level": level,
            },
        },
        "root": {
            "handlers": ["console", "file"],
            "level": level,
        },
    }

    logging.config.dictConfig(config)
    logging.info("ロガーを初期化しました。ログファイル: %s", log_file)


def get_logger(name: str) -> logging.Logger:
    """各モジュールで個別ロガーを取得するための関数"""
    return logging.getLogger(name)
