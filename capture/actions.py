import json
from dataclasses import dataclass

from capture.locks import require_unlocked
from orders.batches import require_text
from orders.slots import slot_names


SCHEMA = '''
CREATE TABLE IF NOT EXISTS capture_actions (
    sequence INTEGER PRIMARY KEY,
    action_id TEXT NOT NULL UNIQUE,
    order_id TEXT NOT NULL REFERENCES manifests(order_id),
    batch TEXT NOT NULL REFERENCES trial_batches(batch),
    slots TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','adopted','released')),
    receipt_id TEXT UNIQUE,
    CHECK ((status = 'pending') = (receipt_id IS NULL))
);
'''


@dataclass(frozen=True)
class ActionIntent:
    action_id: str
    order_id: str
    batch: str
    slots: tuple[str, ...]
    status: str = 'pending'
    receipt_id: str | None = None


def _intent(row):
    return ActionIntent(
        row['action_id'], row['order_id'], row['batch'], tuple(json.loads(row['slots'])),
        row['status'], row['receipt_id'],
    )


def get_intent(station, action_id):
    row = station.db.execute(
        'SELECT * FROM capture_actions WHERE action_id = ?', (action_id,)
    ).fetchone()
    if row is None:
        raise KeyError(action_id)
    return _intent(row)


def record_intent(station, manifest, slots, action_id=None):
    planned = tuple(sorted(slot_names(slots)))
    if not set(planned).issubset(manifest.slots):
        raise ValueError('capture action is outside its initiating manifest')
    with station.write('dispatch'):
        # Re-check inside the write transaction so a concurrent retest plan
        # cannot lock a retained slot between the caller's check and this write.
        require_unlocked(station, manifest.order_id, manifest.batch, planned)
        if action_id is None:
            sequence = station.db.execute(
                'SELECT COALESCE(MAX(sequence), 0) + 1 FROM capture_actions'
            ).fetchone()[0]
            action_id = f'@station:{sequence}'
        else:
            action_id = require_text(action_id, 'action id')
            if action_id.startswith('@station:'):
                raise ValueError('action id prefix @station: is reserved')
            previous = station.db.execute(
                'SELECT * FROM capture_actions WHERE action_id = ?', (action_id,)
            ).fetchone()
            if previous is not None:
                intent = _intent(previous)
                if (intent.order_id, intent.batch, intent.slots) != (manifest.order_id, manifest.batch, planned):
                    raise ValueError(f'capture action {action_id!r} has different parameters')
                return intent
        station.db.execute(
            'INSERT INTO capture_actions(action_id, order_id, batch, slots) VALUES (?, ?, ?, ?)',
            (action_id, manifest.order_id, manifest.batch, json.dumps(planned)),
        )
    return ActionIntent(action_id, manifest.order_id, manifest.batch, planned)


def find_unsettled_actions(station, batch, slots):
    relevant = set(slots)
    rows = station.db.execute(
        '''SELECT * FROM capture_actions WHERE batch = ? AND status != 'released'
           ORDER BY sequence''', (batch,),
    )
    return tuple(intent for intent in map(_intent, rows) if relevant.intersection(intent.slots))


def mark_receipt_adopted(station, action_id, receipt_id):
    changed = station.db.execute(
        '''UPDATE capture_actions SET status = 'adopted', receipt_id = ?
           WHERE action_id = ? AND status = 'pending' ''', (receipt_id, action_id),
    )
    if changed.rowcount != 1:
        raise ValueError(f'cannot adopt receipt for action {action_id!r}')


def mark_receipt_released(station, action_id, receipt_id):
    changed = station.db.execute(
        '''UPDATE capture_actions SET status = 'released'
           WHERE action_id = ? AND receipt_id = ? AND status IN ('adopted','released')''',
        (action_id, receipt_id),
    )
    if changed.rowcount != 1:
        raise ValueError(f'cannot release unadopted receipt for action {action_id!r}')
