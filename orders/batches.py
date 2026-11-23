from dataclasses import dataclass


SCHEMA = '''
CREATE TABLE IF NOT EXISTS trial_batches (
    batch TEXT PRIMARY KEY NOT NULL
);
'''


@dataclass(frozen=True)
class TrialBatch:
    batch: str


def require_text(value, field):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError(f'{field} must be a nonempty string')
    return value


def register_batch(station, batch):
    batch = require_text(batch, 'batch')
    with station.write():
        station.db.execute(
            'INSERT INTO trial_batches(batch) VALUES (?) ON CONFLICT(batch) DO NOTHING',
            (batch,),
        )
    return TrialBatch(batch)


def get_batch(station, batch):
    batch = require_text(batch, 'batch')
    row = station.db.execute('SELECT batch FROM trial_batches WHERE batch = ?', (batch,)).fetchone()
    if row is None:
        raise KeyError(batch)
    return TrialBatch(row['batch'])
