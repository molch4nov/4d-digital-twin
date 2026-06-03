from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import json
from functools import wraps
import asyncio

class ResponseCache:
    def __init__(self, ttl_seconds: int = 5):
        self.cache: Dict[str, tuple[Any, datetime]] = {}
        self.ttl = ttl_seconds

    def get(self, key: str) -> Optional[Any]:
        if key in self.cache:
            data, timestamp = self.cache[key]
            if datetime.now() - timestamp < timedelta(seconds=self.ttl):
                return data
            else:
                del self.cache[key]
        return None

    def set(self, key: str, value: Any):
        self.cache[key] = (value, datetime.now())

    def clear(self):
        self.cache.clear()

# Глобальный кэш для списка задач
tasks_cache = ResponseCache(ttl_seconds=3)
