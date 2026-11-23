from dataclasses import dataclass

from orders.batches import get_batch, require_text


SCHEMA = '''
CREATE TABLE IF NOT EXISTS registered_slots (
    batch TEXT NOT NULL REFERENCES trial_batches(batch),
    slot TEXT NOT NULL,
    PRIMARY KEY (batch, slot)
);
'''


@dataclass(frozen=True)
class SlotKey:
    batch: str
    slot: str


def validate_slot_key(batch, slot):
    return SlotKey(require_text(batch, 'batch'), require_text(slot, 'slot'))


def slot_names(slots):
    if isinstance(slots, (str, bytes, dict)):
        raise ValueError('slots must be a collection of slot identifiers')
    try:
        names = tuple(require_text(slot, 'slot') for slot in slots)
    except TypeError as error:
        raise ValueError('slots must be iterable') from error
    if not names or len(set(names)) != len(names):
        raise ValueError('slot identifiers must be nonempty and unique')
    return names


def register_slots(station, batch, slots):
    names = slot_names(slots)
    get_batch(station, batch)
    keys = tuple(validate_slot_key(batch, slot) for slot in names)
    with station.write():
        station.db.executemany(
            '''INSERT INTO registered_slots(batch, slot) VALUES (?, ?)
               ON CONFLICT(batch, slot) DO NOTHING''',
            [(key.batch, key.slot) for key in keys],
        )
    return keys


def require_registered_slots(station, batch, slots):
    try:
        get_batch(station, batch)
    except KeyError as error:
        raise ValueError(f'unknown batch {batch!r}') from error
    absent = []
    for slot in slots:
        key = validate_slot_key(batch, slot)
        row = station.db.execute(
            'SELECT 1 FROM registered_slots WHERE batch = ? AND slot = ?', (key.batch, key.slot)
        ).fetchone()
        if row is None:
            absent.append(slot)
    if absent:
        raise ValueError(f'unregistered slots in {batch!r}: {absent!r}')
