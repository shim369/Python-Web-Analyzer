import logging
import logging.config
from datetime import datetime
from pathlib import Path


def setup_logger() -> None:
    """ロガーの初期設定。

    fileConfig ではフィルターの詳細制御ができない制限を考慮し、dictConfig を採用。
    """
    # 1. ログディレクトリとファイル名の作成（あなたの素晴らしいロジックをそのまま継承！）
    log_directory = Path("logs")
    log_directory.mkdir(parents=True, exist_ok=True)
    log_file = log_directory / f"app_{datetime.now().strftime('%Y%m%d')}.log"

    # 2. dictConfig による厳格な設定
    config = {
        "version": 1,
        # ★要求仕様: 既存のロガー（customtkinter等）を勝手に無効化せず保護する
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s [%(levelname)s] %(name)s:%(lineno)d - %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
                "level": "DEBUG",
            },
            "file": {
                "class": "logging.FileHandler",
                "filename": str(log_file),
                "encoding": "utf-8",
                "mode": "a",
                "formatter": "standard",
                "level": "DEBUG",
            },
        },
        "root": {
            "handlers": ["console", "file"],
            "level": "DEBUG",
        },
    }

    logging.config.dictConfig(config)
    logging.info("ロガーを初期化しました.")


def get_logger(name: str) -> logging.Logger:
    """各モジュールで個別ロガーを取得するための関数"""
    return logging.getLogger(name)
