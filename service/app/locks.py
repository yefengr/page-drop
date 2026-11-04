"""按 UUID 复用的互斥锁注册表。

未知或已结束的发布不应留下常驻锁：条目只在仍有持有者或等待者时保留，
容量满时拒绝新登记，而不是淘汰仍被引用的锁。
"""

import asyncio
from contextlib import asynccontextmanager

from .config import ServiceError


class LockRegistry:
    def __init__(self, limit):
        self.limit = limit
        self.entries = {}

    @asynccontextmanager
    async def hold(self, item_id):
        entry = self.entries.get(item_id)
        if entry is None:
            if len(self.entries) >= self.limit:
                raise ServiceError("BUSY", "Too many concurrent publish locks")
            entry = self.entries[item_id] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] <= 0:
                self.entries.pop(item_id, None)
