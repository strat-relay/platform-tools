"""Independent clock worker: slow synchronous bridge reads cannot delay expiry."""
from datetime import datetime, timezone
import logging
import threading
import time

from trade_management.deadlines import process_deadlines

log = logging.getLogger(__name__)


class DeadlineScheduler:
    def __init__(self, conn_factory, health, *, interval_seconds=5.0):
        self.conn_factory = conn_factory
        self.health = health
        self.interval_seconds = interval_seconds
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="tm-deadlines", daemon=True)

    def tick(self):
        conn = self.conn_factory()
        try:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = '5s'")
                cur.execute("SET lock_timeout = '2s'")
            count = process_deadlines(conn, now_utc=datetime.now(timezone.utc))
            self.health.mark_ready("deadline_scheduler", detail={"last_success_at": datetime.now(timezone.utc).isoformat(),
                                                                 "decisions": count})
        finally:
            conn.close()

    def run(self):
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception:
                self.health.mark_unready("deadline_scheduler", detail="Deadline tick failed; inspect runtime logs")
                log.exception("deadline_tick failed; retrying independently of market observations")
            self.stop.wait(max(0.0, self.interval_seconds - (time.monotonic() - started)))

    def start(self):
        self.health.mark_unready("deadline_scheduler", detail="Starting")
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=6)
