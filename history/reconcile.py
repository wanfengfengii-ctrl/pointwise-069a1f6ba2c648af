from dataclasses import dataclass

from history.export import SourceLine, export_sources
from history.lookup import read
from issuance.measure import measure_total
from issuance.receipts import FrozenMismatch, IssueReceipt, check_receipt_binding
from orders.batches import require_text


@dataclass(frozen=True)
class Reconciliation:
    result_id: str
    shift: str
    sources: tuple[SourceLine, ...]
    total: int
    receipt: IssueReceipt


def reconcile(station, shift, result_id):
    shift = require_text(shift, 'shift')
    result = read(station, result_id)
    if result.shift != shift:
        raise ValueError(f'result {result_id!r} was issued in shift {result.shift!r}')
    check_receipt_binding(result.receipt, result.order_id, result.result_id)
    sources = export_sources(result)
    total = measure_total(result.lines)
    if total != result.total:
        raise FrozenMismatch(f'frozen total differs from detail for {result_id!r}')
    return Reconciliation(result_id, shift, sources, total, result.receipt)
