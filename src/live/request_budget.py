"""Durable conservative Standard REST budget; never waits inside protection.

All bot transports for an L1 wallet must share this file. External clients and
SDK-internal bootstrap traffic are not measured; headroom is not a guarantee.
"""
import sqlite3
import time


class RequestDeferred(RuntimeError):
    def __init__(self, retry_after):
        self.retry_after = max(0., float(retry_after))
        super().__init__(f'API request deferred for {self.retry_after:.1f}s; preserve orders and retry reads')


class EntryDeferred(ValueError):
    """No order intent exists: the candidate may be retried before its deadline."""
    def __init__(self, cause):
        self.retry_after = cause.retry_after
        self.details = getattr(cause, 'details', {})
        super().__init__('insufficient API headroom for entry and protection; entry deferred')


class RequestBudget:
    def __init__(self, path, *, limit=55, clock=time.time):
        self.clock, self.limit = clock, limit
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS requests(stamp REAL NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS cooldown(id INTEGER PRIMARY KEY, until REAL NOT NULL)')
        self.db.commit()

    def close(self): self.db.close()

    def check(self, slots=1, *, consume=False):
        now = self.clock()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('DELETE FROM requests WHERE stamp<=?', (now-60.1,))
            pause = self.db.execute('SELECT until FROM cooldown WHERE id=1').fetchone()
            stamps = [r[0] for r in self.db.execute('SELECT stamp FROM requests ORDER BY stamp')]
            delay = max(0., (pause[0]-now) if pause else 0.)
            if len(stamps)+slots > self.limit:
                delay = max(delay, stamps[min(len(stamps)-1, len(stamps)+slots-self.limit-1)]+60.1-now)
            if delay > 0:
                error = RequestDeferred(delay)
                error.details = {'used':len(stamps), 'limit':self.limit,
                                 'required':slots, 'window_seconds':60.1,
                                 'cooldown_until':pause[0] if pause else None}
                raise error
            if consume: self.db.execute('INSERT INTO requests VALUES(?)', (now,))

    def defer(self, seconds=60):
        until = self.clock()+max(60., float(seconds))
        with self.db:
            self.db.execute('INSERT INTO cooldown VALUES(1,?) ON CONFLICT(id) DO UPDATE SET until=MAX(until,excluded.until)', (until,))
