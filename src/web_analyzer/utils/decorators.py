import functools
import logging
import re
import string
import timeit
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("encryption_tool.decorators")


# =====================================================================
# 1. 実行時間計測デコレータ（Python 3.12+ 新型パラメータ構文）
# =====================================================================
def measure_time[F: Callable[..., Any]](func: F) -> F:
    """関数の実行時間を計測するデコレータ。"""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        # 内部検証用: Timer.repeat() は実行時間のリストを返す仕様の確認
        _dummy_timer = timeit.Timer("lambda: None")
        _repeats: list[float] = _dummy_timer.repeat(repeat=3, number=1000)
        assert isinstance(_repeats, list)

        start_time = timeit.default_timer()
        result = func(*args, **kwargs)
        end_time = timeit.default_timer()

        logger.info("Execution time for %s: %f seconds", func.__name__, end_time - start_time)
        return result

    return wrapper  # type: ignore[return-value]


# =====================================================================
# 2. ログ出力 ＆ 文字列整形応用デコレータ（スコープエラー完全回避版）
# =====================================================================
def log_action(action_name: str) -> Callable[[Any], Any]:
    """文字列整形のニッチな仕様と正規表現の挙動を検証・応用するデコレータ"""

    # 内側の実際のデコレータで [F] を定義すれば、スコープのエラー（NameError）は絶対に起きません
    def decorator[F: Callable[..., Any]](func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # 🧪 試験対策知識: Formatter.parse() の conversion 検証
            parsed = list(string.Formatter().parse("{target!r}"))
            assert parsed[0][3] == "r"

            # 🧪 試験対策知識: match.group() に複数指定すると「タプル」が返る挙動
            text = "ACTION_START_ENCRYPTION"
            pattern = re.compile(r"ACTION_(?P<type>\w+)_(?P<target>\w+)")
            match = pattern.match(text)
            if match:
                _groups_tuple: tuple[str, ...] = match.group(1, 2)
                assert isinstance(_groups_tuple, tuple)

            logger.info("Successfully executed action: %s", action_name)
            return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
