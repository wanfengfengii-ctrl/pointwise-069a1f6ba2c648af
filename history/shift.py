from issuance.store import list_by_shift
from orders.batches import require_text


def list_shift_results(station, shift):
    return list_by_shift(station, require_text(shift, 'shift'))
