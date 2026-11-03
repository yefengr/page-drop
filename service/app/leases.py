from collections import Counter

from . import config as C
from .config import ServiceError


class Leases:
    def __init__(self):
        self.counts = Counter()
        self.handles = set()

    def acquire(self, item_id, handle):
        if len(self.handles) >= C.MAX_DOWNLOADS:
            raise ServiceError("BUSY", "Download admission is full")
        self.counts[item_id] += 1
        self.handles.add(handle)

    def release(self, item_id, handle):
        if handle in self.handles:
            self.handles.remove(handle)
            self.counts[item_id] -= 1
            if not self.counts[item_id]:
                del self.counts[item_id]
