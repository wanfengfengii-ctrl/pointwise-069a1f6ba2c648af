from dataclasses import dataclass

from capture.actions import get_intent, mark_receipt_adopted
from orders.slots import SlotKey, validate_slot_key


SCHEMA = '''
CREATE TABLE IF NOT EXISTS sample_evidence (
    batch TEXT NOT NULL,
    slot TEXT NOT NULL,
    value TEXT NOT NULL,
    position TEXT NOT NULL CHECK (length(position) > 0),
    action_id TEXT NOT NULL REFERENCES capture_actions(action_id),
    receipt_id TEXT NOT NULL,
    PRIMARY KEY (batch, slot),
    FOREIGN KEY (batch, slot) REFERENCES registered_slots(batch, slot)
);
CREATE INDEX IF NOT EXISTS evidence_by_action ON sample_evidence(action_id);
'''


@dataclass(frozen=True)
class SampleEvidence:
    key: SlotKey
    value: int
    position: str
    action_id: str
    receipt_id: str


def _evidence(row):
    return SampleEvidence(
        SlotKey(row['batch'], row['slot']), int(row['value'], 0), row['position'],
        row['action_id'], row['receipt_id'],
    )


def get_evidence(station, batch, slot):
    key = validate_slot_key(batch, slot)
    row = station.db.execute(
        '''SELECT e.* FROM sample_evidence AS e JOIN capture_actions AS a
                  ON e.action_id = a.action_id AND e.batch = a.batch AND e.receipt_id = a.receipt_id
           WHERE e.batch = ? AND e.slot = ? AND a.status IN ('adopted','released')''',
        (key.batch, key.slot),
    ).fetchone()
    return None if row is None else _evidence(row)


def get_committed_receipt(station, action_id):
    return get_intent(station, action_id).receipt_id


def persist_receipt(station, receipt):
    expected = tuple(
        SampleEvidence(reading.key, reading.value, reading.position, receipt.action_id, receipt.receipt_id)
        for reading in receipt.readings
    )
    with station.write('adopt'):
        intent = get_intent(station, receipt.action_id)
        if intent.batch != receipt.batch:
            raise ValueError(f'evidence batch conflicts with action {intent.action_id!r}')
        if intent.receipt_id is not None:
            rows = station.db.execute(
                'SELECT * FROM sample_evidence WHERE action_id = ?', (intent.action_id,)
            )
            if intent.receipt_id != receipt.receipt_id or set(map(_evidence, rows)) != set(expected):
                raise ValueError(f'conflicting receipt replay for {intent.action_id!r}')
            return
        for sample in expected:
            station.db.execute(
                '''INSERT INTO sample_evidence(batch, slot, value, position, action_id, receipt_id)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (sample.key.batch, sample.key.slot, hex(sample.value), sample.position,
                 sample.action_id, sample.receipt_id),
            )
        mark_receipt_adopted(station, intent.action_id, receipt.receipt_id)
