from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator


def env_int(name: str, default: int, min_value: int = 1, max_value: int = 256) -> int:
    try:
        value = int(os.getenv(name) or default)
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(int(value), max_value))


class Limiter:
    def __init__(self, limit: int) -> None:
        self.limit = max(int(limit), 1)
        self._semaphore = threading.BoundedSemaphore(self.limit)

    @contextmanager
    def acquire(self) -> Iterator[None]:
        self._semaphore.acquire()
        try:
            yield
        finally:
            self._semaphore.release()


ai_limiter = Limiter(env_int("AI_OPTION_AI_CONCURRENCY", 4, 1, 32))
yfinance_limiter = Limiter(env_int("AI_OPTION_YF_CONCURRENCY", 3, 1, 16))
longbridge_cli_limiter = Limiter(env_int("AI_OPTION_LONGBRIDGE_CLI_CONCURRENCY", 2, 1, 16))
longbridge_sdk_limiter = Limiter(env_int("AI_OPTION_LONGBRIDGE_SDK_CONCURRENCY", 2, 1, 32))
