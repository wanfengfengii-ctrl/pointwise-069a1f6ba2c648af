from dataclasses import dataclass

from capture.actions import ActionIntent, record_intent
from capture.locks import require_unlocked
from device.store import DeviceUnavailable
from device.take import DeviceReceipt, ReceiptConfirmed, take
from orders.slots import slot_names


@dataclass(frozen=True)
class DispatchOutcome:
    intent: ActionIntent
    receipt: DeviceReceipt | None = None
    uncertain: bool = False
    confirmed_receipt_id: str | None = None


def send_intent(station, intent):
    require_unlocked(station, intent.order_id, intent.batch, intent.slots)
    try:
        receipt = take(station.source, intent.action_id, intent.batch, intent.slots)
    except ReceiptConfirmed as state:
        return DispatchOutcome(intent, confirmed_receipt_id=state.receipt_id)
    except DeviceUnavailable:
        return DispatchOutcome(intent, uncertain=True)
    return DispatchOutcome(intent, receipt=receipt)


def dispatch_take(station, manifest, slots, action_id=None):
    planned = slot_names(slots)
    require_unlocked(station, manifest.order_id, manifest.batch, planned)
    intent = record_intent(station, manifest, planned, action_id)
    return send_intent(station, intent)

