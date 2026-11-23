from dataclasses import dataclass

from device.confirm import confirm_receipt
from device.take import DeviceReceipt, decode_receipt
from orders.batches import require_text


@dataclass(frozen=True)
class SourceReceiptState:
    action_id: str
    status: str
    receipt_id: str | None = None
    receipt: DeviceReceipt | None = None


def lookup_receipt(store, action_id):
    action_id = require_text(action_id, 'action id')
    store.require_available('lookup', action_id)
    row = store.db.execute('SELECT * FROM device_actions WHERE action_id = ?', (action_id,)).fetchone()
    if row is None:
        return SourceReceiptState(action_id, 'not_executed')
    if row['confirmed']:
        return SourceReceiptState(action_id, 'confirmed', row['receipt_id'])
    return SourceReceiptState(action_id, 'unconfirmed', row['receipt_id'], decode_receipt(row))


def source_receipt(store, action_id, *, confirm=None):
    if confirm is None:
        return lookup_receipt(store, action_id)
    receipt_id = confirm_receipt(store, action_id, confirm)
    return SourceReceiptState(action_id, 'confirmed', receipt_id)
