import json
from dataclasses import dataclass

from orders.batches import require_text
from orders.slots import SlotKey, slot_names


@dataclass(frozen=True)
class SampleReading:
    key: SlotKey
    value: int
    position: str


@dataclass(frozen=True)
class DeviceReceipt:
    action_id: str
    batch: str
    requested_slots: tuple[str, ...]
    receipt_id: str
    readings: tuple[SampleReading, ...]


class ReceiptConfirmed(RuntimeError):
    def __init__(self, receipt_id):
        super().__init__(f'device already confirmed {receipt_id!r}')
        self.receipt_id = receipt_id


def decode_receipt(row):
    batch = row['batch']
    readings = tuple(
        SampleReading(SlotKey(batch, slot), int(value, 0), position)
        for slot, value, position in json.loads(row['body'])
    )
    return DeviceReceipt(
        row['action_id'], batch, tuple(json.loads(row['slots'])), row['receipt_id'], readings,
    )


def _take_available(store, action_id, batch, slots):
    raw_readings = []
    for slot in slots:
        row = store.db.execute(
            '''SELECT value, position FROM device_slots
               WHERE batch = ? AND slot = ? AND taken_by IS NULL''', (batch, slot),
        ).fetchone()
        if row is None:
            continue
        raw_readings.append((slot, row['value'], row['position']))
        store.db.execute(
            '''UPDATE device_slots SET taken_by = ?, takes = takes + 1,
                      value = NULL, position = NULL WHERE batch = ? AND slot = ?''',
            (action_id, batch, slot),
        )
    return raw_readings


def take(store, action_id, batch, slots):
    action_id = require_text(action_id, 'action id')
    batch = require_text(batch, 'batch')
    requested = tuple(sorted(slot_names(slots)))
    store.require_available('take', action_id)
    with store.write_transaction():
        row = store.db.execute('SELECT * FROM device_actions WHERE action_id = ?', (action_id,)).fetchone()
        if row is not None:
            if row['batch'] != batch or tuple(json.loads(row['slots'])) != requested:
                raise ValueError(f'action {action_id!r} has different parameters')
            if row['confirmed']:
                raise ReceiptConfirmed(row['receipt_id'])
            return decode_receipt(row)
        raw_readings = _take_available(store, action_id, batch, requested)
        receipt = {
            'action_id': action_id,
            'batch': batch,
            'slots': json.dumps(requested),
            'receipt_id': f'device:{action_id}',
            'body': json.dumps(raw_readings),
        }
        store.db.execute(
            '''INSERT INTO device_actions(action_id, batch, slots, receipt_id, body)
               VALUES (:action_id, :batch, :slots, :receipt_id, :body)''', receipt,
        )
    store.deliver_reply('take', action_id)
    return decode_receipt(receipt)
