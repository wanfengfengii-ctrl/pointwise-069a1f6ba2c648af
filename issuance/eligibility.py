from dataclasses import dataclass

from capture.evidence import get_evidence
from issuance.measure import ResultLine
from orders.slots import SlotKey


@dataclass(frozen=True)
class IssueEligibility:
    lines: tuple[ResultLine, ...]
    pending: tuple[str, ...]


def evaluate_eligibility(station, manifest):
    lines = []
    pending = []
    for slot in manifest.slots:
        original = get_evidence(station, manifest.batch, slot)
        if original is None or original.key != SlotKey(manifest.batch, slot):
            pending.append(slot)
            continue
        if not (original.position.strip() and original.action_id.strip() and original.receipt_id.strip()):
            pending.append(slot)
            continue
        lines.append(ResultLine(
            slot, original.value, original.key.batch, original.key.slot, original.position,
            original.action_id, original.receipt_id,
        ))
    return IssueEligibility(tuple(lines), tuple(pending))
