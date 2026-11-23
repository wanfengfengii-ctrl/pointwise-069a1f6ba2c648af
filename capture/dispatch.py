from dataclasses import dataclass

from capture.actions import ActionIntent, record_intent
from device.store import DeviceUnavailable
from device.take import DeviceReceipt, ReceiptConfirmed, take


@dataclass(frozen=True)
class DispatchOutcome:
    intent: ActionIntent
    receipt: DeviceReceipt | None = None
    uncertain: bool = False
    confirmed_receipt_id: str | None = None


def send_intent(station, intent):
    try:
        receipt = take(station.source, intent.action_id, intent.batch, intent.slots)
    except ReceiptConfirmed as state:
        return DispatchOutcome(intent, confirmed_receipt_id=state.receipt_id)
    except DeviceUnavailable:
        return DispatchOutcome(intent, uncertain=True)
    return DispatchOutcome(intent, receipt=receipt)


def dispatch_take(station, manifest, slots, action_id=None):
    intent = record_intent(station, manifest, slots, action_id)
    return send_intent(station, intent)
