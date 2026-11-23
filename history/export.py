from dataclasses import dataclass

from issuance.receipts import FrozenMismatch
from issuance.store import IssuedResult
from orders.batches import require_text


@dataclass(frozen=True)
class SourceLine:
    batch: str
    slot: str
    position: str
    action_id: str
    device_receipt_id: str


def export_sources(result: IssuedResult):
    if (not result.slots or len(set(result.slots)) != len(result.slots)
            or tuple(line.slot for line in result.lines) != result.slots):
        raise FrozenMismatch(f'details do not match frozen manifest for {result.result_id!r}')
    sources = []
    for line in result.lines:
        if (line.source_batch, line.source_slot) != (result.batch, line.slot):
            raise FrozenMismatch(f'source points to a different slot for {line.slot!r}')
        try:
            require_text(line.source_batch, 'source batch')
            require_text(line.source_slot, 'source slot')
            require_text(line.position, 'device position')
            require_text(line.action_id, 'device action id')
            require_text(line.device_receipt_id, 'device receipt id')
        except ValueError as error:
            raise FrozenMismatch(f'incomplete source for {line.slot!r}') from error
        sources.append(SourceLine(
            line.source_batch, line.source_slot, line.position, line.action_id, line.device_receipt_id,
        ))
    return tuple(sources)
