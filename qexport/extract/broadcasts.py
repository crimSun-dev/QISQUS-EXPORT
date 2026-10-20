from __future__ import annotations

from collections.abc import Callable
from datetime import date

from qexport.api.client import QiscusClient
from qexport.extract.messages import CursorLoopError
from qexport.store.checkpoint import CheckpointStore
from qexport.transform.normalize import BroadcastLogRecord, Normalizer


class BroadcastEnumerator:
    """Walks broadcast_jobs (newest first) into the store, same resume rules as RoomEnumerator."""

    def __init__(self, client: QiscusClient, store: CheckpointStore, normalizer: Normalizer,
                 page_limit: int = 100, since: date | None = None) -> None:
        self._client, self._store, self._norm = client, store, normalizer
        self._limit, self._since = page_limit, since

    def _keep(self, jobs):
        if self._since is None:
            return jobs, False
        kept = [j for j in jobs if j.created_at_utc.date() >= self._since]
        return kept, len(kept) < len(jobs)    # an older job means we've walked past the window

    def run(self, on_page: Callable[[int, int], None] | None = None) -> int:
        cursor = self._store.get_broadcast_cursor()
        page_no = 0
        while True:
            raw, nxt = self._client.list_broadcast_jobs(cursor, self._limit)
            page_no += 1
            if not raw:
                break
            jobs, past_window = self._keep([self._norm.broadcast_job(j) for j in raw])
            self._store.upsert_jobs_page(jobs, nxt)
            if on_page:
                on_page(page_no, self._store.progress().jobs_total)
            if past_window or nxt is None or nxt == cursor:
                break
            cursor = nxt
        self._store.mark_broadcast_enumeration_done()
        return self._store.progress().jobs_total

    def top_up(self, max_pages: int = 100) -> int:
        """Re-walk from the newest job until a page holds nothing new or changed."""
        cursor: str | None = None
        changed_total = 0
        for _ in range(max_pages):
            raw, nxt = self._client.list_broadcast_jobs(cursor, self._limit)
            if not raw:
                break
            jobs, past_window = self._keep([self._norm.broadcast_job(j) for j in raw])
            changed = self._store.upsert_jobs_page(jobs, nxt, advance_cursor=False)
            changed_total += changed
            if changed == 0 or past_window or nxt is None or nxt == cursor:
                break
            cursor = nxt
        return changed_total


class BroadcastLogFetcher:
    MAX_PAGES_PER_JOB: int = 2000     # 200k recipients per job

    def __init__(self, client: QiscusClient, normalizer: Normalizer, page_limit: int = 100) -> None:
        self._client, self._norm, self._limit = client, normalizer, page_limit

    def fetch_job(self, job_id: int) -> list[BroadcastLogRecord]:
        """Every recipient row of one job. Raises RoomAccessError / AuthError / CursorLoopError."""
        seen: dict[int, BroadcastLogRecord] = {}
        cursor: str | None = None
        for _ in range(self.MAX_PAGES_PER_JOB):
            page, nxt = self._client.list_broadcast_logs(job_id, cursor, self._limit)
            if not page:
                break
            new = 0
            for raw in page:
                if raw.id not in seen:
                    seen[raw.id] = self._norm.broadcast_log(raw, job_id)
                    new += 1
            if new == 0:
                raise CursorLoopError(f"broadcast job {job_id}: page at cursor {cursor} repeated")
            if len(page) < self._limit or nxt is None or nxt == cursor:
                break
            cursor = nxt
        else:
            raise CursorLoopError(f"broadcast job {job_id}: more than {self.MAX_PAGES_PER_JOB} pages")
        return sorted(seen.values(), key=lambda r: r.log_id)
