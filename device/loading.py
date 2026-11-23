import hashlib
import json

from orders.batches import require_text
from orders.slots import validate_slot_key


def load(store, batch, slot, value, position):
    key = validate_slot_key(batch, slot)
    position = require_text(position, 'device position')
    if type(value) is not int:
        raise ValueError('sample strength must be an integer')
    stored_value = hex(value)
    provision = json.dumps([stored_value, position], separators=(',', ':')).encode('ascii')
    fingerprint = hashlib.sha256(provision).hexdigest()
    store.require_available('load', key.slot)
    with store.write_transaction():
        row = store.db.execute(
            'SELECT load_fingerprint FROM device_slots WHERE batch = ? AND slot = ?',
            (key.batch, key.slot),
        ).fetchone()
        if row is not None:
            if row['load_fingerprint'] != fingerprint:
                raise ValueError(f'conflicting device load for {key!r}')
            return key
        store.db.execute(
            '''INSERT INTO device_slots(batch, slot, load_fingerprint, value, position)
               VALUES (?, ?, ?, ?, ?)''',
            (key.batch, key.slot, fingerprint, stored_value, position),
        )
    return key
