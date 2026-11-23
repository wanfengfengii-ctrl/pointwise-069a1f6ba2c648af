from issuance.store import load_by_result
from orders.batches import require_text


def read(station, result_id):
    result_id = require_text(result_id, 'result id')
    result = load_by_result(station, result_id)
    if result is None:
        raise KeyError(result_id)
    return result
