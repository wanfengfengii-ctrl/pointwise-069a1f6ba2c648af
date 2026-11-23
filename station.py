import argparse
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from capture.actions import SCHEMA as ACTION_SCHEMA
from capture.dispatch import dispatch_take
from capture.evidence import SCHEMA as EVIDENCE_SCHEMA
from capture.evidence import get_evidence
from capture.locks import SCHEMA as LOCK_SCHEMA
from capture.locks import SampleLocked, is_locked
from capture.resume import resume
from capture.retests import SCHEMA as RETEST_SCHEMA
from capture.retests import (
    RetestCandidate,
    RetestPlan,
    RetestReconciliation,
    RetestRefused,
    RetestRequirement,
    RetestResult,
    RetestResume,
    RetestSettlement,
    get_retest_plan,
    list_retests_by_shift,
    load_retest_by_order,
    plan_retests,
    read_retest,
    reconcile_retest,
    resume_retest,
    settle_retests,
)
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
            schemas = (
                BATCH_SCHEMA, SLOT_SCHEMA, MANIFEST_SCHEMA, ACTION_SCHEMA, EVIDENCE_SCHEMA,
                RESULT_SCHEMA, LOCK_SCHEMA, RETEST_SCHEMA,
            )
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
        if stage not in ('dispatch', 'adopt', 'release', 'issue', 'plan', 'settle'):
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

    def resume(self, order_id, action_id=None):
        return resume(self, order_id, action_id)

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

    def plan_retests(self, requirements, candidates, budget, plan_id=None):
        return plan_retests(self, requirements, candidates, budget, plan_id)

    def retest_plan(self, plan_id):
        return get_retest_plan(self, plan_id)

    def resume_retest(self, plan_id, action_id=None):
        return resume_retest(self, plan_id, action_id)

    def settle_retests(self, plan_id):
        return settle_retests(self, plan_id)

    def retest_for(self, order_id):
        return load_retest_by_order(self, order_id)

    def read_retest(self, result_id):
        return read_retest(self, result_id)

    def list_shift_retests(self, shift):
        return list_retests_by_shift(self, shift)

    def reconcile_retest(self, shift, result_id):
        return reconcile_retest(self, shift, result_id)


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


def _retest_demo(directory, data):
    batch = '复验批'
    samples = (
        ('P', 5, '光谱台/21'), ('Q', 6, '光谱台/22'),
        ('X', 70, '光谱台/31'), ('Y', 80, '光谱台/32'), ('Z', 90, '光谱台/33'),
    )
    shared = (('X', ('甲',), 2), ('Y', ('乙',), 3), ('Z', ('甲', '乙'), 4))
    requirements = (('甲', 1), ('乙', 1))
    station_path = Path(directory) / 'retest.sqlite'
    with open_device_store(Path(directory) / 'retest-device.sqlite') as device:
        with Station(station_path, device, shift=data['shift']) as desk:
            desk.register_batch(batch)
            desk.register_slots(batch, tuple(slot for slot, _, _ in samples))
            for slot, value, position in samples:
                desk.load_sample(batch, slot, value, position)
            originals = {}
            for order_id, slot in (('甲', 'P'), ('乙', 'Q')):
                desk.order(order_id, batch, (slot,))
                desk.resume(order_id)
                originals[order_id] = desk.issue(order_id)
            try:
                desk.plan_retests(requirements, shared[:2], 4)
            except RetestRefused as refused:
                greedy_refused = set(refused.pending) == {'甲', '乙'}
            else:
                raise AssertionError('a cheapest-first greedy plan should be refused')
            plan = desk.plan_retests(requirements, shared, 4)
            if plan.slots != ('Z',) or plan.total_cost != 4:
                raise AssertionError('retest plan did not choose the shared Z slot')
            desk.order('丙', batch, ('X', 'Z'))
            competition = desk.resume('丙')
            z_evidence_after_competition = get_evidence(desk, batch, 'Z')
            if competition.pending != ('Z',) or z_evidence_after_competition is not None:
                raise AssertionError('ordinary capture must not take a locked retained slot')
            device.drop_replies.add(('take', 'rt-z'))
            first = desk.resume_retest(plan.plan_id, 'rt-z')
            lock_after_lost_reply = is_locked(desk, batch, 'Z')
            if not lock_after_lost_reply:
                raise AssertionError('an uncertain retest take must keep the retained sample locked')
            second = desk.resume_retest(plan.plan_id, 'rt-z')
            lock_before_settlement = is_locked(desk, batch, 'Z')
            if second.pending or not lock_before_settlement:
                raise AssertionError('retest evidence did not settle under the same lock')
            settlement = desk.settle_retests(plan.plan_id)
            lock_after_settlement = is_locked(desk, batch, 'Z')
            takes = device.taken_count(batch, 'Z')
            confirmed = source_receipt(device, 'rt-z').status
        with Station(station_path, shift='晚班') as audit:
            by_order = {result.order_id: result for result in settlement.results}
            audits = tuple(
                audit.reconcile_retest(data['shift'], by_order[order_id].result_id)
                for order_id in ('甲', '乙')
            )
            z_source = audits[0].sources[0]
            retest_downstream = {
                'totals': [entry.total for entry in audits],
                'same_source': audits[0].sources[0] == audits[1].sources[0],
                'source': [z_source.source_slot, z_source.position, z_source.action_id,
                           z_source.device_receipt_id],
                'z_takes': takes, 'device_receipt': confirmed,
                'locks_released': not lock_after_settlement,
                'originals_intact': [
                    audit.result_for(order_id) == originals[order_id] for order_id in ('甲', '乙')
                ],
            }
            for order_id in ('甲', '乙'):
                original = originals[order_id]
                if by_order[order_id].original_result_id != original.result_id:
                    raise AssertionError(f'retest did not link the original result of {order_id!r}')
                if by_order[order_id].original_receipt_id != original.receipt.receipt_id:
                    raise AssertionError(f'retest did not keep the original first receipt of {order_id!r}')
            if retest_downstream != {
                'totals': [90, 90], 'same_source': True,
                'source': ['Z', '光谱台/33', 'rt-z', 'device:rt-z'],
                'z_takes': 1, 'device_receipt': 'confirmed',
                'locks_released': True, 'originals_intact': [True, True],
            }:
                raise AssertionError('retest handoff disagrees with physical sampling')
    return {
        'case_id': 'v8',
        'input': {
            'requirements': {'甲': 1, '乙': 1},
            'candidates': [[slot, list(orders), cost] for slot, orders, cost in shared],
            'budget': 4, 'lost_reply_action': 'rt-z',
        },
        'expected': {'slots': ['Z'], 'total_cost': 4},
        'actual': {'slots': list(plan.slots), 'total_cost': plan.total_cost},
        'greedy_refused': greedy_refused,
        'competition': {
            'pending': list(competition.pending),
            'z_evidence_after_resume': z_evidence_after_competition,
        },
        'retest': {
            'lost_reply_pending': list(first.pending),
            'retry_actions': list(first.retry_actions),
            'lock_after_lost_reply': lock_after_lost_reply,
            'resumed_pending': list(second.pending),
            'lock_before_settlement': lock_before_settlement,
            'results': {order_id: _result_details(by_order[order_id]) for order_id in ('甲', '乙')},
        },
        'downstream_expected': {
            'totals': [90, 90], 'same_source': True,
            'source': ['Z', '光谱台/33', 'rt-z', 'device:rt-z'],
            'z_takes': 1, 'device_receipt': 'confirmed',
            'locks_released': True, 'originals_intact': [True, True],
        },
        'downstream_actual': retest_downstream,
        'code_paths': ['capture/retests.py', 'capture/locks.py', 'capture/resume.py',
                       'device/take.py', 'issuance/measure.py'],
        'oracle': '两单各需一槽：甲单 X 成本 2、乙单 Y 成本 3、共享 Z 成本 4，额度 4 只取 Z；'
                  'Z 实取一次、回执确认一次、两单复验成果同源，留样锁在销账后释放。',
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
        retest_scenario = _retest_demo(directory, data)
    scenario = {
        'case_id': 'v7',
        'input': {'fixture': data, 'lost_reply': 'x', 'lookup_paused_during_first_Y_resume': True},
        'expected': expected, 'actual': actual,
        'downstream_expected': expected_handoff, 'downstream_actual': downstream,
        'code_paths': ['device/take.py', 'capture/resume.py', 'issuance/publish.py', 'history/reconcile.py'],
        'oracle': '固定样例手算 11+13=24 和 7+11=18，并对照 B 的固定物理位置、动作、回执及一次实取计数。',
    }
    print('POINTWISE_SCENARIO ' + json.dumps(scenario, ensure_ascii=False))
    print('POINTWISE_SCENARIO ' + json.dumps(retest_scenario, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description='青砾材料试验站采样交班')
    parser.add_argument('command', choices=('demo',))
    parser.parse_args()
    demo()


if __name__ == '__main__':
    main()
