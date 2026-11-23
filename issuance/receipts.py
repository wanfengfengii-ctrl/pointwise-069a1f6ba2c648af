from dataclasses import dataclass


class FrozenMismatch(ValueError):
    """The frozen issue records disagree."""


@dataclass(frozen=True)
class IssueReceipt:
    receipt_id: str
    result_id: str
    order_id: str


def make_issue_receipt(order_id, result_id):
    return IssueReceipt(f'first:{order_id}', result_id, order_id)


def check_receipt_binding(receipt, order_id, result_id):
    if result_id != f'result:{order_id}' or receipt != make_issue_receipt(order_id, result_id):
        raise FrozenMismatch(f'first receipt does not bind to result {result_id!r}')
