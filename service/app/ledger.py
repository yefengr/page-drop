from dataclasses import dataclass

from . import config as C
from .config import ServiceError


@dataclass
class Orphan:
    bytes: int
    first_seen: float


class Ledger:
    """Only the event-loop coordinator mutates these mutually exclusive buckets."""
    def __init__(self):
        self.reservations = {}
        self.settled = {}
        self.orphans = {}

    @property
    def total(self):
        return (sum(self.reservations.values()) + sum(self.settled.values())
                + sum(entry.bytes for entry in self.orphans.values()))

    def reserve(self, item_id):
        if self.reservations:
            raise ServiceError("BUSY", "Another upload is active")
        if self.total + C.MAX_CONTENT > C.CONTENT_BUDGET:
            raise ServiceError("QUOTA_EXCEEDED", "Insufficient content budget")
        self.reservations[item_id] = C.MAX_CONTENT

    def settle(self, item_id, size):
        self.reservations.pop(item_id, None)
        self.settled[item_id] = size

    def release(self, item_id):
        self.reservations.pop(item_id, None)
        self.settled.pop(item_id, None)
