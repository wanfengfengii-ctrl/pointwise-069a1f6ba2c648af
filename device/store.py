import sqlite3
from contextlib import contextmanager

from orders.slots import validate_slot_key


SCHEMA = '''
CREATE TABLE IF NOT EXISTS device_actions (
    action_id TEXT PRIMARY KEY NOT NULL,
    batch TEXT NOT NULL,
    slots TEXT NOT NULL,
    receipt_id TEXT UNIQUE NOT NULL,
    body TEXT,
    confirmed INTEGER NOT NULL DEFAULT 0 CHECK (confirmed IN (0, 1)),
    CHECK ((confirmed = 0 AND body IS NOT NULL) OR (confirmed = 1 AND body IS NULL))
);
CREATE TABLE IF NOT EXISTS device_slots (
    batch TEXT NOT NULL,
    slot TEXT NOT NULL,
    load_fingerprint TEXT NOT NULL,
    value TEXT,
    position TEXT,
    taken_by TEXT REFERENCES device_actions(action_id) DEFERRABLE INITIALLY DEFERRED,
    takes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (batch, slot),
    CHECK ((taken_by IS NULL AND value IS NOT NULL AND position IS NOT NULL)
           OR (taken_by IS NOT NULL AND value IS NULL AND position IS NULL))
);
'''


class DeviceUnavailable(RuntimeError):
    """The device operation can be retried."""


class DeviceReplyLost(DeviceUnavailable):
    """The device write committed before its reply was lost."""


class DeviceStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.online = True
        self.blocked = set()
        self.drop_replies = set()
        try:
            self.db.execute('PRAGMA foreign_keys = ON')
            self.db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA + '\nCOMMIT;')
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.close()

    def close(self):
        self.online = False
        self.db.close()

    @contextmanager
    def write_transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def require_available(self, operation, identifier):
        if not self.online or (operation, identifier) in self.blocked:
            raise DeviceUnavailable(f'{operation} temporarily unavailable for {identifier!r}')

    def deliver_reply(self, operation, identifier):
        key = (operation, identifier)
        if key in self.drop_replies:
            self.drop_replies.remove(key)
            raise DeviceReplyLost(f'{operation} committed before reply loss for {identifier!r}')

    def taken_count(self, batch, slot):
        key = validate_slot_key(batch, slot)
        self.require_available('count', key.slot)
        row = self.db.execute(
            'SELECT takes FROM device_slots WHERE batch = ? AND slot = ?', (key.batch, key.slot)
        ).fetchone()
        if row is None:
            raise KeyError(key)
        return row['takes']

    def sample_available(self, batch, slot):
        """Return True when a loaded sample is still physically untaken."""
        key = validate_slot_key(batch, slot)
        self.require_available('count', key.slot)
        row = self.db.execute(
            '''SELECT taken_by, value FROM device_slots
               WHERE batch = ? AND slot = ?''', (key.batch, key.slot),
        ).fetchone()
        return row is not None and row['taken_by'] is None and row['value'] is not None


def open_device_store(path):
    return DeviceStore(path)
