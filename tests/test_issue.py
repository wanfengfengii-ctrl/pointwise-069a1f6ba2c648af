import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from capture.evidence import get_evidence
from issuance import IssuePending, IssuedResult
from station import CommitRejected, initialize
from tests.support import StationCase


class IssueTests(StationCase):
    def test_manifest_order_and_zero(self):
        self.data['samples'][0]['value'] = 0
        with self.desk() as station:
            initialize(station, self.data)
            station.order('reverse', self.batch, ('B', 'A'))
            receipt = station.start_capture('reverse', 'reverse-action').receipt
            self.assertEqual(tuple(line.key.slot for line in receipt.readings), ('A', 'B'))
            self.assertEqual(station.resume('reverse').pending, ())
            result = station.issue('reverse')
        expected = {'slots': ['B', 'A'], 'values': [11, 0], 'total': 11}
        actual = {'slots': [line.slot for line in result.lines],
                  'values': [line.value for line in result.lines], 'total': result.total}
        self.assertEqual(actual, expected)
        with self.desk(offline=True) as station:
            audit = station.reconcile(self.shift, result.result_id)
            downstream = {'slots': [line.slot for line in audit.sources], 'total': audit.total,
                          'zero_source': audit.sources[1].position, 'first_receipt': audit.receipt.receipt_id}
        expected_downstream = {'slots': ['B', 'A'], 'total': 11, 'zero_source': '光谱台/01',
                               'first_receipt': 'first:reverse'}
        self.assertEqual(downstream, expected_downstream)
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'v2', 'input': {'readings': {'A': 0, 'B': 11}, 'manifest': ['B', 'A']},
            'expected': expected, 'actual': actual,
            'downstream_expected': expected_downstream, 'downstream_actual': downstream,
            'code_paths': ['capture/evidence.py', 'issuance/eligibility.py', 'issuance/measure.py',
                           'history/reconcile.py'],
            'oracle': '按手写清单 B、A 取 11、0，手算 11+0=11，复核 A 的固定设备位置。',
        }, ensure_ascii=False))

    def test_exact_total(self):
        self.data['samples'][0]['value'] = 9223372036854775807
        self.data['samples'][1]['value'] = 1
        with self.desk() as station:
            initialize(station, self.data)
            station.order('large', self.batch, ('B', 'A'))
            station.resume('large')
            result = station.issue('large')
            self.assertEqual([line.value for line in result.lines], [1, 9223372036854775807])
            self.assertEqual(result.total, 9223372036854775808)
        with self.desk(offline=True) as station:
            self.assertEqual(station.read(result.result_id), result)
            self.assertEqual(station.reconcile(self.shift, result.result_id).total, 9223372036854775808)

    def test_empty_take_is_pending(self):
        self.leave_x()
        self.device.blocked.add(('lookup', 'x'))
        with self.desk() as station:
            station.resume('Y')
            self.assertEqual(station.issue('Y'), IssuePending('Y', ('B',)), 'SHARED_SAMPLE_INCOMPLETE')
            self.assertEqual(self.publication_counts(station, 'Y'), (0, 0))
            self.assertEqual(get_evidence(station, self.batch, 'C').value, 13)
            with self.assertRaises(KeyError):
                station.read('result:Y')
        self.device.blocked.remove(('lookup', 'x'))
        with self.desk() as station:
            station.resume('Y')
            result = station.issue('Y')
            self.assertIsInstance(result, IssuedResult, 'SHARED_SAMPLE_INCOMPLETE')
            self.assertEqual([line.value for line in result.lines], [11, 13], 'SHARED_SAMPLE_INCOMPLETE')
            self.assertEqual(result.total, 24)
            self.assertEqual(result.lines[0].action_id, 'x')

    def test_retry_publish_after_rollback(self):
        self.leave_x()
        with self.desk() as station:
            self.assertEqual(station.resume('Y').pending, ())
            evidence = tuple(get_evidence(station, self.batch, slot) for slot in ('B', 'C'))
            station.reject_next_commit('issue')
            with self.assertRaisesRegex(CommitRejected, 'issue'):
                station.issue('Y')
            self.assertIsNone(station.result_for('Y'))
            self.assertEqual(self.publication_counts(station, 'Y'), (0, 0))
        with self.desk(offline=True) as station:
            self.assertEqual(self.publication_counts(station, 'Y'), (0, 0))
            self.assertEqual(tuple(get_evidence(station, self.batch, slot) for slot in ('B', 'C')), evidence)
            result = station.issue('Y')
            self.assertEqual(result.total, 24)
            self.assertEqual(result.receipt.receipt_id, 'first:Y')
            self.assertEqual(station.issue('Y'), result)
            self.assertEqual(self.publication_counts(station, 'Y'), (1, 1))

    def test_order_id_rejects_different_slots(self):
        x, _ = self.issue_pair()
        with self.desk(offline=True) as station:
            with self.assertRaises(ValueError):
                station.order('X', self.batch, ('A', 'C'))
            station.register_batch('other')
            station.register_slots('other', ('A', 'B'))
            with self.assertRaises(ValueError):
                station.order('X', 'other', ('A', 'B'))
            self.assertEqual(station.manifest('X').slots, ('A', 'B'))
            self.assertEqual(station.read(x.result_id), x)

    def test_same_request_reuses_manifest_and_receipt(self):
        x, _ = self.issue_pair()
        with self.desk(offline=True, shift='晚班') as station:
            manifest = station.order('X', self.batch, ('A', 'B'))
            self.assertEqual(manifest, station.manifest('X'))
            self.assertEqual(station.issue('X'), x)
            self.assertEqual(x.shift, self.shift)
            self.assertEqual(x.receipt.receipt_id, 'first:X')

    def test_reordered_conflicts_with_frozen_order(self):
        x, _ = self.issue_pair()
        with self.desk(offline=True) as station:
            with self.assertRaises(ValueError):
                station.order('X', self.batch, ('B', 'A'))
            self.assertEqual(station.issue('X'), x)
            self.assertEqual([line.slot for line in station.read(x.result_id).lines], ['A', 'B'])

    def test_invalid_new_manifests_are_not_saved(self):
        with self.desk() as station:
            initialize(station, self.data)
            for batch, slots in ((self.batch, ()), (self.batch, ('A', 'A')), (self.batch, ('Z',)),
                                 ('unknown', ('A',)), (self.batch, 'AB')):
                with self.subTest(batch=batch, slots=slots), self.assertRaises(ValueError):
                    station.order('invalid', batch, slots)
            with self.assertRaises(KeyError):
                station.manifest('invalid')

    def test_parallel_publish_reuses_one_receipt(self):
        self.leave_x()
        with self.desk() as station:
            self.assertEqual(station.resume('Y').pending, ())
        barrier = Barrier(2, timeout=10)

        def publish():
            with self.desk(offline=True) as station:
                barrier.wait()
                return station.issue('Y')

        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(publish)
            second = workers.submit(publish)
            results = (first.result(timeout=15), second.result(timeout=15))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0].total, 24)
        with self.desk(offline=True) as station:
            self.assertEqual(self.publication_counts(station, 'Y'), (1, 1))
