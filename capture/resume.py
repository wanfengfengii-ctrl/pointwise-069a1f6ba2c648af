from dataclasses import dataclass

from capture.actions import find_unsettled_actions
from capture.dispatch import dispatch_take, send_intent
from capture.evidence import get_evidence, persist_receipt
from capture.locks import foreign_locked_slots
from capture.receipts import validate_receipt
from capture.settlement import release_receipt
from device.recover import source_receipt
from device.store import DeviceUnavailable
from orders.manifests import get_manifest


@dataclass(frozen=True)
class ResumeResult:
    order_id: str
    adopted: tuple[str, ...]
    pending: tuple[str, ...]
    retry_actions: tuple[str, ...]


def _missing(station, manifest):
    return tuple(slot for slot in manifest.slots if get_evidence(station, manifest.batch, slot) is None)


def _adopt(station, intent, receipt):
    evidence = validate_receipt(intent, receipt)
    persist_receipt(station, evidence)
    release_receipt(station, intent.action_id)


def _complete_dispatch(station, outcome):
    if outcome.uncertain:
        return False
    if outcome.confirmed_receipt_id is not None:
        release_receipt(
            station, outcome.intent.action_id, confirmed_receipt_id=outcome.confirmed_receipt_id,
        )
    else:
        _adopt(station, outcome.intent, outcome.receipt)
    return True


def _recover(station, intent):
    if intent.receipt_id is not None:
        release_receipt(station, intent.action_id)
        return True
    state = source_receipt(station.source, intent.action_id)
    if state.status == 'not_executed':
        return _complete_dispatch(station, send_intent(station, intent))
    if state.status == 'confirmed':
        release_receipt(station, intent.action_id, confirmed_receipt_id=state.receipt_id)
    else:
        _adopt(station, intent, state.receipt)
    return True


def resume(station, order_id, action_id=None):
    manifest = get_manifest(station, order_id)
    retry = []
    unresolved_own_slots = set()
    for intent in find_unsettled_actions(station, manifest.batch, manifest.slots):
        # Another plan's locked retained slots may only be settled through that
        # plan's own capture order, never through an ordinary order's resume.
        if foreign_locked_slots(station, order_id, intent.batch, intent.slots):
            continue
        try:
            complete = _recover(station, intent)
        except DeviceUnavailable:
            complete = False
        if not complete:
            retry.append(intent.action_id)
            if intent.order_id == order_id and intent.receipt_id is None:
                unresolved_own_slots.update(intent.slots)

    # Other orders' uncertain actions do not reserve these slots. Positive
    # device evidence decides which action actually took an overlapping slot.
    # Slots reserved for a retest stay pending for ordinary capture.
    locked = foreign_locked_slots(station, order_id, manifest.batch, manifest.slots)
    remaining = tuple(
        slot for slot in _missing(station, manifest)
        if slot not in unresolved_own_slots and slot not in locked
    )
    if remaining:
        outcome = dispatch_take(station, manifest, remaining, action_id)
        try:
            complete = _complete_dispatch(station, outcome)
        except DeviceUnavailable:
            complete = False
        if not complete:
            retry.append(outcome.intent.action_id)

    pending = _missing(station, manifest)
    pending_set = set(pending)
    adopted = tuple(slot for slot in manifest.slots if slot not in pending_set)
    return ResumeResult(order_id, adopted, pending, tuple(retry))
