import json
from dataclasses import dataclass

from issuance.measure import ResultLine
from issuance.receipts import FrozenMismatch, IssueReceipt


SCHEMA = '''
CREATE TABLE IF NOT EXISTS issued_results (
    result_id TEXT PRIMARY KEY NOT NULL,
    order_id TEXT UNIQUE NOT NULL REFERENCES manifests(order_id),
    batch TEXT NOT NULL,
    slots TEXT NOT NULL,
    total TEXT NOT NULL,
    shift TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issued_lines (
    result_id TEXT NOT NULL REFERENCES issued_results(result_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    slot TEXT NOT NULL,
    value TEXT NOT NULL,
    source_batch TEXT NOT NULL,
    source_slot TEXT NOT NULL,
    position TEXT NOT NULL,
    action_id TEXT NOT NULL,
    device_receipt_id TEXT NOT NULL,
    PRIMARY KEY (result_id, ordinal)
);
CREATE TABLE IF NOT EXISTS issue_receipts (
    receipt_id TEXT PRIMARY KEY NOT NULL,
    result_id TEXT UNIQUE NOT NULL REFERENCES issued_results(result_id),
    order_id TEXT UNIQUE NOT NULL REFERENCES manifests(order_id)
);
CREATE INDEX IF NOT EXISTS issues_by_shift ON issued_results(shift, order_id);
'''


@dataclass(frozen=True)
class IssuedResult:
    result_id: str
    order_id: str
    batch: str
    slots: tuple[str, ...]
    lines: tuple[ResultLine, ...]
    total: int
    shift: str
    receipt: IssueReceipt


def commit_result(station, result):
    with station.write('issue'):
        existing = load_by_order(station, result.order_id)
        if existing is not None:
            return existing
        station.db.execute(
            'INSERT INTO issued_results(result_id, order_id, batch, slots, total, shift) VALUES (?, ?, ?, ?, ?, ?)',
            (result.result_id, result.order_id, result.batch, json.dumps(result.slots),
             hex(result.total), result.shift),
        )
        station.db.executemany(
            '''INSERT INTO issued_lines(result_id, ordinal, slot, value, source_batch, source_slot,
                                        position, action_id, device_receipt_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            [(result.result_id, ordinal, line.slot, hex(line.value), line.source_batch, line.source_slot,
              line.position, line.action_id, line.device_receipt_id) for ordinal, line in enumerate(result.lines)],
        )
        station.db.execute(
            'INSERT INTO issue_receipts(receipt_id, result_id, order_id) VALUES (?, ?, ?)',
            (result.receipt.receipt_id, result.receipt.result_id, result.receipt.order_id),
        )
    return result


def _assemble(station, head):
    result_id = head['result_id']
    receipt = station.db.execute(
        'SELECT receipt_id, result_id, order_id FROM issue_receipts WHERE result_id = ?', (result_id,),
    ).fetchone()
    if receipt is None:
        raise FrozenMismatch(f'missing first receipt for result {result_id!r}')
    rows = station.db.execute('SELECT * FROM issued_lines WHERE result_id = ? ORDER BY ordinal', (result_id,))
    lines = []
    try:
        slots = json.loads(head['slots'])
        if not isinstance(slots, list) or not all(isinstance(slot, str) for slot in slots):
            raise ValueError('invalid frozen manifest')
        for ordinal, row in enumerate(rows):
            if row['ordinal'] != ordinal:
                raise ValueError('missing frozen line ordinal')
            lines.append(ResultLine(
                row['slot'], int(row['value'], 0), row['source_batch'], row['source_slot'],
                row['position'], row['action_id'], row['device_receipt_id'],
            ))
        total = int(head['total'], 0)
    except (TypeError, ValueError) as error:
        raise FrozenMismatch(f'invalid frozen records for result {result_id!r}') from error
    return IssuedResult(
        result_id, head['order_id'], head['batch'], tuple(slots), tuple(lines), total, head['shift'],
        IssueReceipt(receipt['receipt_id'], receipt['result_id'], receipt['order_id']),
    )


def load_by_order(station, order_id):
    row = station.db.execute('SELECT * FROM issued_results WHERE order_id = ?', (order_id,)).fetchone()
    return None if row is None else _assemble(station, row)


def load_by_result(station, result_id):
    row = station.db.execute('SELECT * FROM issued_results WHERE result_id = ?', (result_id,)).fetchone()
    return None if row is None else _assemble(station, row)


def list_by_shift(station, shift):
    rows = station.db.execute('SELECT * FROM issued_results WHERE shift = ? ORDER BY order_id', (shift,))
    return tuple(_assemble(station, row) for row in rows)
