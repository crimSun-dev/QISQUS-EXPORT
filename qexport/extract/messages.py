from __future__ import annotations

from qexport.api.client import QiscusClient, QiscusError
from qexport.transform.normalize import MessageRecord, Normalizer, RoomRecord


class CursorLoopError(QiscusError): ...


class MessageFetcher:
    MAX_PAGES_PER_ROOM: int = 500   # guards against a cursor loop (50k msgs/room)

    def __init__(self, client: QiscusClient, normalizer: Normalizer, page_limit: int = 100) -> None:
        self._client, self._norm, self._limit = client, normalizer, page_limit

    def fetch_room(self, room: RoomRecord) -> list[MessageRecord]:
        """Pages backwards via last_comment_id = oldest id of previous page, until a page returns
        < limit rows. De-duplicates by msg_id. Returns records sorted ascending by (timestamp, msg_id).
        Raises RoomAccessError / AuthError / RateLimitExhausted / CursorLoopError."""
        seen: dict[int, MessageRecord] = {}
        last_id: int | None = None
        for _ in range(self.MAX_PAGES_PER_ROOM):
            page = self._client.load_comments(room.room_id, last_id, self._limit)
            if not page:
                break
            new = 0
            for raw in page:
                if raw.id not in seen:
                    seen[raw.id] = self._norm.message(raw, room)
                    new += 1
            if new == 0:
                raise CursorLoopError(f"room {room.room_id}: page after id {last_id} repeated")
            oldest = min(r.id for r in page)
            if len(page) < self._limit:
                break
            if oldest == last_id:
                raise CursorLoopError(f"room {room.room_id}: oldest id {oldest} repeated")
            last_id = oldest
        else:
            raise CursorLoopError(f"room {room.room_id}: more than {self.MAX_PAGES_PER_ROOM} pages")
        return sorted(seen.values(), key=lambda m: (m.timestamp_utc, m.msg_id))
