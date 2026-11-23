from collections.abc import Sequence

from orders.batches import require_text
from orders.manifests import Manifest, save_manifest
from orders.slots import require_registered_slots, slot_names


def order(station, order_id, batch, slots):
    order_id = require_text(order_id, 'order id')
    if order_id.startswith('@retest:'):
        raise ValueError('order id prefix @retest: is reserved for retest capture')
    if not isinstance(slots, Sequence):
        raise ValueError('an order requires an ordered slot sequence')
    names = slot_names(slots)
    require_registered_slots(station, batch, names)
    return save_manifest(station, Manifest(order_id, batch, names))
