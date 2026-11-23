from dataclasses import dataclass

from issuance.eligibility import evaluate_eligibility
from issuance.measure import measure_total
from issuance.receipts import make_issue_receipt
from issuance.store import IssuedResult, commit_result, load_by_order
from orders.manifests import get_manifest


@dataclass(frozen=True)
class IssuePending:
    order_id: str
    pending: tuple[str, ...]


def issue(station, order_id):
    manifest = get_manifest(station, order_id)
    existing = load_by_order(station, order_id)
    if existing is not None:
        return existing
    eligibility = evaluate_eligibility(station, manifest)
    if eligibility.pending:
        return IssuePending(order_id, eligibility.pending)
    result_id = f'result:{order_id}'
    result = IssuedResult(
        result_id, order_id, manifest.batch, manifest.slots, eligibility.lines,
        measure_total(eligibility.lines), station.shift, make_issue_receipt(order_id, result_id),
    )
    return commit_result(station, result)
