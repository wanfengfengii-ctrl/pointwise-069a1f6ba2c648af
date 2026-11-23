"""Durable reservations for retained sample slots held by a retest plan."""

from orders.batches import require_text
from orders.slots import validate_slot_key


SCHEMA = '''
CREATE TABLE IF NOT EXISTS retest_locks (
    batch TEXT NOT NULL,
    slot TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'locked' CHECK (status IN ('locked','released')),
    PRIMARY KEY (batch, slot)
);
'''


RETEST_ORDER_PREFIX = '@retest:'


class SampleLocked(ValueError):
    """An ordinary capture tried to take a slot reserved for retest."""


def retest_order_id(plan_id):
    return f'{RETEST_ORDER_PREFIX}{require_text(plan_id, "plan id")}'


def lock_slot(station, plan_id, batch, slot):
    key = validate_slot_key(batch, slot)
    station.db.execute(
        '''INSERT INTO retest_locks(batch, slot, plan_id, status) VALUES (?, ?, ?, 'locked')''',
        (key.batch, key.slot, plan_id),
    )


def lock_owner(station, batch, slot):
    key = validate_slot_key(batch, slot)
    row = station.db.execute(
        '''SELECT plan_id FROM retest_locks
           WHERE batch = ? AND slot = ? AND status = 'locked' ''',
        (key.batch, key.slot),
    ).fetchone()
    return None if row is None else row['plan_id']


def is_locked(station, batch, slot):
    return lock_owner(station, batch, slot) is not None


def locked_slots(station, batch, slots):
    return tuple(slot for slot in slots if is_locked(station, batch, slot))


def foreign_locked(station, order_id, batch, slots):
    """Plans whose still-locked slots this order may not capture."""
    owners = set()
    for slot in slots:
        owner = lock_owner(station, batch, slot)
        if owner is not None and order_id != retest_order_id(owner):
            owners.add(owner)
    return frozenset(owners)


def foreign_locked_slots(station, order_id, batch, slots):
    """Slot names inside ``slots`` that another retest plan has locked."""
    held = []
    for slot in slots:
        owner = lock_owner(station, batch, slot)
        if owner is not None and order_id != retest_order_id(owner):
            held.append(slot)
    return tuple(held)


def require_unlocked(station, order_id, batch, slots):
    owners = foreign_locked(station, order_id, batch, slots)
    if owners:
        raise SampleLocked(
            f'retained sample slots for {order_id!r} are locked by retest plans {sorted(owners)!r}'
        )


def release_plan_locks(station, plan_id):
    station.db.execute(
        '''UPDATE retest_locks SET status = 'released'
           WHERE plan_id = ? AND status = 'locked' ''',
        (plan_id,),
    )


def release_slot_lock(station, plan_id, batch, slot):
    key = validate_slot_key(batch, slot)
    station.db.execute(
        '''UPDATE retest_locks SET status = 'released'
           WHERE plan_id = ? AND batch = ? AND slot = ? AND status = 'locked' ''',
        (plan_id, key.batch, key.slot),
    )
