import logging
import logging.config
from datetime import datetime
from pathlib import Path


def setup_logger(level: int = logging.DEBUG) -> None:
    """ロガーの初期設定。

    実行ごとに日時を秒単位まで含めた個別のログファイルを生成する。
    """
    log_directory = Path("logs")
    log_directory.mkdir(parents=True, exist_ok=True)

    # ★ 実行時の「年月日時分秒」をファイル名に組み込む (例: app_20260803_162005.log)
    current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_directory / f"app_{current_time}.log"

    config = {
        "version": 1,
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
                "level": logging.INFO,  # 画面はスッキリINFOのみ
            },
            "file": {
                # ★ 実行ごとにファイルを分けるため、通常の FileHandler に戻す
                "class": "logging.FileHandler",
                "filename": str(log_file),
                "encoding": "utf-8",
                "mode": "w",  # ★ 'a'(追記) ではなく 'w'(新規書き込み) にすることで確実に新ファイルにする
                "formatter": "standard",
                "level": logging.DEBUG,  # ファイルには全デバッグログを記録
            },
        },
        "root": {
            "handlers": ["console", "file"],
            "level": logging.DEBUG,
        },
    }

    logging.config.dictConfig(config)
    logging.info("ロガーを初期化しました。新規ログファイル: %s", log_file)


def get_logger(name: str) -> logging.Logger:
    """各モジュールで個別ロガーを取得するための関数"""
    return logging.getLogger(name)
