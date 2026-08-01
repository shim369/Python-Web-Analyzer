import functools
import logging
import timeit
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


# =====================================================================
# 1. 実行時間計測デコレータ
# =====================================================================
def measure_time[F: Callable[..., Any]](func: F) -> F:
    """関数の実行時間を計測し、DEBUGログに出力するデコレータ。"""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        start_time = timeit.default_timer()
        result = func(*args, **kwargs)
        elapsed = timeit.default_timer() - start_time

        logger.debug("Execution time for %s: %.3f seconds", func.__qualname__, elapsed)
        return result

    return wrapper  # type: ignore[return-value]


# =====================================================================
# 2. アクションログ出力デコレータ
# =====================================================================
def log_action(action_name: str) -> Callable[[Any], Any]:
    """処理の開始と完了をINFOログに出力するデコレータ。"""

    def decorator[F: Callable[..., Any]](func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            logger.info("開始: %s", action_name)
            result = func(*args, **kwargs)
            logger.info("完了: %s", action_name)
            return result

        return wrapper  # type: ignore[return-value]

    return decorator
