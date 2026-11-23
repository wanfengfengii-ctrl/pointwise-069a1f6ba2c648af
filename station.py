import argparse
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from capture.actions import SCHEMA as ACTION_SCHEMA
from capture.dispatch import dispatch_take
from capture.evidence import SCHEMA as EVIDENCE_SCHEMA
from capture.resume import resume
from device import DeviceUnavailable, load, open_device_store, source_receipt
from history import export_sources, list_shift_results, read, reconcile
from issuance import IssuePending, IssuedResult, issue, load_by_order
from issuance.store import SCHEMA as RESULT_SCHEMA
from orders import get_manifest, order, register_batch, register_slots
from orders.batches import SCHEMA as BATCH_SCHEMA
from orders.batches import require_text
from orders.manifests import SCHEMA as MANIFEST_SCHEMA
from orders.slots import SCHEMA as SLOT_SCHEMA
from orders.slots import require_registered_slots


SAMPLE = Path(__file__).resolve().parent / 'samples' / 'day.json'


class CommitRejected(RuntimeError):
    """A configured station write was rejected before commit."""


class Station:
    def __init__(self, path, device=None, *, shift='早班'):
        self.shift = require_text(shift, 'shift')
        self.device = device
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self._rejected_commits = set()
        try:
            self.db.execute('PRAGMA foreign_keys = ON')
            schemas = (BATCH_SCHEMA, SLOT_SCHEMA, MANIFEST_SCHEMA, ACTION_SCHEMA, EVIDENCE_SCHEMA, RESULT_SCHEMA)
            self.db.executescript('BEGIN IMMEDIATE;\n' + '\n'.join(schemas) + '\nCOMMIT;')
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.close()

    def close(self):
        self.db.close()

    @property
    def source(self):
        if self.device is None:
            raise DeviceUnavailable('the station has no connected device')
        return self.device

    def reject_next_commit(self, stage):
        if stage not in ('dispatch', 'adopt', 'release', 'issue'):
            raise ValueError(f'unknown station commit stage {stage!r}')
        self._rejected_commits.add(stage)

    @contextmanager
    def write(self, stage=None):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            if stage in self._rejected_commits:
                self._rejected_commits.remove(stage)
                raise CommitRejected(f'{stage} commit rejected')
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def register_batch(self, batch):
        return register_batch(self, batch)

    def register_slots(self, batch, slots):
        return register_slots(self, batch, slots)

    def load_sample(self, batch, slot, value, position):
        require_registered_slots(self, batch, (slot,))
        return load(self.source, batch, slot, value, position)

    def order(self, order_id, batch, slots):
        return order(self, order_id, batch, slots)

    def manifest(self, order_id):
        return get_manifest(self, order_id)

    def start_capture(self, order_id, action_id=None):
        manifest = get_manifest(self, order_id)
        return dispatch_take(self, manifest, manifest.slots, action_id)

    def resume(self, order_id):
        return resume(self, order_id)

    def issue(self, order_id):
        return issue(self, order_id)

    def result_for(self, order_id):
        return load_by_order(self, require_text(order_id, 'order id'))

    def read(self, result_id):
        return read(self, result_id)

    def list_shift_results(self, shift):
        return list_shift_results(self, shift)

    def export_sources(self, result_id):
        return export_sources(self.read(result_id))

    def reconcile(self, shift, result_id):
        return reconcile(self, shift, result_id)


def initialize(station, data):
    batch = data['batch']
    station.register_batch(batch)
    station.register_slots(batch, [sample['slot'] for sample in data['samples']])
    for sample in data['samples']:
        station.load_sample(batch, sample['slot'], sample['value'], sample['position'])
    for request in data['orders']:
        station.order(request['order_id'], batch, request['slots'])


def _result_details(result):
    return {
        'slots': [line.slot for line in result.lines],
        'values': [line.value for line in result.lines],
        'total': result.total,
    }


def demo():
    data = json.loads(SAMPLE.read_text(encoding='utf-8'))
    expected = {
        'Y': {'slots': ['B', 'C'], 'values': [11, 13], 'total': 24},
        'X': {'slots': ['A', 'B'], 'values': [7, 11], 'total': 18},
    }
    expected_handoff = {
        'totals': [24, 18], 'same_B': True, 'same_results': True,
        'B_position': '光谱台/02', 'B_action': 'x', 'B_receipt': 'device:x',
        'B_takes': 1, 'receipts': ['first:Y', 'first:X'],
    }
    with TemporaryDirectory(prefix='spectral-station-') as directory:
        station_path = Path(directory) / 'station.sqlite'
        with open_device_store(Path(directory) / 'device.sqlite') as device:
            with Station(station_path, device, shift=data['shift']) as caller:
                initialize(caller, data)
                device.drop_replies.add(('take', 'x'))
                if not caller.start_capture('X', 'x').uncertain:
                    raise AssertionError('expected a lost device reply after X took its samples')
            device.blocked.add(('lookup', 'x'))
            with Station(station_path, device, shift=data['shift']) as desk:
                partial = desk.resume('Y')
                waiting = desk.issue('Y')
                if (partial.pending != ('B',) or partial.adopted != ('C',)
                        or waiting != IssuePending('Y', ('B',)) or desk.result_for('Y') is not None):
                    raise AssertionError('SHARED_SAMPLE_INCOMPLETE: Y must wait with C in custody')
                device.blocked.remove(('lookup', 'x'))
                desk.resume('Y')
                y = desk.issue('Y')
                if not isinstance(y, IssuedResult) or _result_details(y) != expected['Y']:
                    raise AssertionError('SHARED_SAMPLE_INCOMPLETE: Y did not receive the complete B,C result')
                desk.resume('X')
                x = desk.issue('X')
                if not isinstance(x, IssuedResult) or _result_details(x) != expected['X']:
                    raise AssertionError('SHARED_SAMPLE_INCOMPLETE: X did not recover its A,B result')
                actual = {'Y': _result_details(y), 'X': _result_details(x)}
                take_count = device.taken_count(data['batch'], 'B')
                released = source_receipt(device, 'x')
                if released.status != 'confirmed' or released.receipt is not None:
                    raise AssertionError('the adopted device receipt was not released')
        with Station(station_path, shift='晚班') as next_shift:
            frozen = tuple(next_shift.read(result.result_id) for result in (y, x))
            audits = tuple(next_shift.reconcile(data['shift'], result.result_id) for result in frozen)
            y_source, x_source = audits[0].sources[0], audits[1].sources[1]
            downstream = {
                'totals': [audit.total for audit in audits], 'same_B': y_source == x_source,
                'same_results': frozen == (y, x), 'B_position': y_source.position,
                'B_action': y_source.action_id, 'B_receipt': y_source.device_receipt_id,
                'B_takes': take_count, 'receipts': [audit.receipt.receipt_id for audit in audits],
            }
            if downstream != expected_handoff:
                raise AssertionError('SHARED_SAMPLE_INCOMPLETE: offline handoff disagrees with physical sampling')
    scenario = {
        'case_id': 'v7',
        'input': {'fixture': data, 'lost_reply': 'x', 'lookup_paused_during_first_Y_resume': True},
        'expected': expected, 'actual': actual,
        'downstream_expected': expected_handoff, 'downstream_actual': downstream,
        'code_paths': ['device/take.py', 'capture/resume.py', 'issuance/publish.py', 'history/reconcile.py'],
        'oracle': '固定样例手算 11+13=24 和 7+11=18，并对照 B 的固定物理位置、动作、回执及一次实取计数。',
    }
    print('POINTWISE_SCENARIO ' + json.dumps(scenario, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description='青砾材料试验站采样交班')
    parser.add_argument('command', choices=('demo',))
    parser.parse_args()
    demo()


if __name__ == '__main__':
    main()
