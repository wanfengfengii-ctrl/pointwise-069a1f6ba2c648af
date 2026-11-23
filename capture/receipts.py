from dataclasses import dataclass

from device.take import SampleReading
from orders.batches import require_text
from orders.slots import validate_slot_key


@dataclass(frozen=True)
class ReceiptEvidence:
    action_id: str
    batch: str
    receipt_id: str
    readings: tuple[SampleReading, ...]


def validate_receipt(intent, receipt):
    if (receipt.action_id, receipt.batch, receipt.requested_slots) != (
        intent.action_id, intent.batch, intent.slots,
    ):
        raise ValueError(f'receipt does not match action {intent.action_id!r}')
    receipt_id = require_text(receipt.receipt_id, 'device receipt id')
    actual_slots = set()
    for reading in receipt.readings:
        key = validate_slot_key(reading.key.batch, reading.key.slot)
        if key.batch != intent.batch or key.slot not in intent.slots or key.slot in actual_slots:
            raise ValueError(f'invalid actual slot {key!r} in receipt {receipt_id!r}')
        require_text(reading.position, 'device position')
        if type(reading.value) is not int:
            raise ValueError(f'noninteger evidence for {key!r}')
        actual_slots.add(key.slot)
    return ReceiptEvidence(intent.action_id, intent.batch, receipt_id, receipt.readings)
