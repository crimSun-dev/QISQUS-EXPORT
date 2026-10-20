from __future__ import annotations

from collections.abc import Callable

from qexport.api.client import QiscusClient
from qexport.store.checkpoint import CheckpointStore
from qexport.transform.normalize import Normalizer


class RoomEnumerator:
    def __init__(self, client: QiscusClient, store: CheckpointStore, normalizer: Normalizer,
                 page_limit: int = 100) -> None:
        self._client, self._store, self._norm, self._limit = client, store, normalizer, page_limit

    def run(self, on_page: Callable[[int, int], None] | None = None) -> int:
        """Walks cursor pages starting at store.get_room_cursor(), upserting each page and its
        next cursor in one transaction. Terminates when a page is empty OR next_cursor is None
        OR next_cursor == current. Returns total distinct rooms in store."""
        cursor = self._store.get_room_cursor()
        page_no = 0
        while True:
            raw, nxt = self._client.list_customer_rooms(cursor, self._limit)
            page_no += 1
            if not raw:
                break
            self._store.upsert_rooms_page([self._norm.room(r) for r in raw], nxt)
            if on_page:
                on_page(page_no, self._store.progress().rooms_total)
            if nxt is None or nxt == cursor:
                break
            cursor = nxt
        self._store.mark_enumeration_done()
        return self._store.progress().rooms_total

    def top_up(self, on_page: Callable[[int, int], None] | None = None, max_pages: int = 1000) -> int:
        """Re-walk from the newest room until a page holds nothing new or changed.

        Rooms are ordered by last activity, so every room with new messages since the last walk
        sits above the first fully-unchanged page. Used after enumeration (rooms bumped during the
        walk) and by --since-last-run. Does not touch the saved resume cursor."""
        cursor: str | None = None
        changed_total = 0
        for page_no in range(1, max_pages + 1):
            raw, nxt = self._client.list_customer_rooms(cursor, self._limit)
            if not raw:
                break
            changed = self._store.upsert_rooms_page([self._norm.room(r) for r in raw], nxt,
                                                    advance_cursor=False)
            changed_total += changed
            if on_page:
                on_page(page_no, changed_total)
            if changed == 0 or nxt is None or nxt == cursor:
                break
            cursor = nxt
        return changed_total
