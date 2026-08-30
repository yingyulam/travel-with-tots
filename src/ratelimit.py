"""Per-caller request limits, held in this process.

The routes that need it are the public ones that spend money: a chat turn, a
plan and a replan each call an LLM, and the search and place lookups call paid
APIs. None of them requires a login, because a parent should be able to try the
app before signing up, so without a limit anyone who finds the URL can empty an
API budget in a few minutes. Login is limited too, for a different reason: it is
the one endpoint where guessing repeatedly is the attack.

In-process, and that is a real limit rather than an oversight. Render runs this
under one gunicorn worker (memory is the binding constraint, see render.yaml),
so one process sees every request and the counts are complete. Add a second
worker and each keeps its own tally, so the effective limit doubles. When that
day comes the answer is Redis, not a cleverer dictionary.

Deliberately not a dependency. Flask-Limiter brings a storage abstraction and a
header spec to solve a problem that is two deques and a lock here.
"""

import threading
import time
from collections import deque

# Stop tracking a caller once their window has fully expired, and cap how many
# are tracked at once. Both matter: the dictionary is keyed on something the
# caller controls, so an attacker rotating addresses would otherwise turn a
# rate limiter into a memory leak. At the cap the oldest entry is dropped,
# which briefly forgives whoever has been quiet longest -- the right one to
# forgive, and much better than growing without bound.
MAX_TRACKED = 10_000


class TooMany(Exception):
    """Raised when a caller is over their limit. Carries seconds to wait."""

    def __init__(self, retry_after):
        super().__init__(f"try again in {retry_after} seconds")
        self.retry_after = retry_after


class RateLimit:
    """Allow `limit` hits per `window` seconds, per key.

    A sliding window rather than a fixed one: fixed windows let a caller send
    `limit` at 0:59 and `limit` again at 1:01, which is twice the rate the
    number claims to allow.
    """

    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self._hits = {}
        self._lock = threading.Lock()

    def check(self, key):
        """Record a hit for `key`, or raise TooMany. Threads share one lock,
        because gunicorn runs 8 threads in the worker and two of them counting
        the same caller must not both see the last slot as free."""
        now = time.monotonic()
        with self._lock:
            self._forget_expired(now)
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                raise TooMany(int(hits[0] + self.window - now) + 1)
            hits.append(now)

    def _forget_expired(self, now):
        """Drop callers whose whole window has passed, then enforce the cap."""
        stale = [key for key, hits in self._hits.items()
                 if not hits or hits[-1] <= now - self.window]
        for key in stale:
            del self._hits[key]
        while len(self._hits) >= MAX_TRACKED:
            oldest = min(self._hits, key=lambda k: self._hits[k][-1])
            del self._hits[oldest]
