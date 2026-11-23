import json
from dataclasses import dataclass

from orders.batches import require_text


SCHEMA = '''
CREATE TABLE IF NOT EXISTS manifests (
    order_id TEXT PRIMARY KEY NOT NULL,
    batch TEXT NOT NULL REFERENCES trial_batches(batch),
    slots TEXT NOT NULL
);
'''


@dataclass(frozen=True)
class Manifest:
    order_id: str
    batch: str
    slots: tuple[str, ...]


def _find_manifest(station, order_id):
    row = station.db.execute(
        'SELECT order_id, batch, slots FROM manifests WHERE order_id = ?', (order_id,)
    ).fetchone()
    if row is None:
        return None
    return Manifest(row['order_id'], row['batch'], tuple(json.loads(row['slots'])))


def get_manifest(station, order_id):
    order_id = require_text(order_id, 'order id')
    manifest = _find_manifest(station, order_id)
    if manifest is None:
        raise KeyError(order_id)
    return manifest


def save_manifest(station, manifest):
    with station.write():
        existing = _find_manifest(station, manifest.order_id)
        if existing is not None:
            if existing != manifest:
                raise ValueError(f'order {manifest.order_id!r} has a different manifest')
            return existing
        station.db.execute(
            'INSERT INTO manifests(order_id, batch, slots) VALUES (?, ?, ?)',
            (manifest.order_id, manifest.batch, json.dumps(manifest.slots)),
        )
    return manifest
