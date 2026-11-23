from capture.actions import get_intent
from capture.evidence import get_committed_receipt, get_evidence
from device import DeviceUnavailable, ReceiptConfirmed, open_device_store, source_receipt, take
from station import CommitRejected, Station, initialize
from tests.support import StationCase


class CollectTests(StationCase):
    def test_reinitialize_retains_taken_slot(self):
        with self.desk() as station:
            initialize(station, self.data)
            station.order('single', self.batch, ('A',))
            self.assertEqual(station.resume('single').adopted, ('A',))
            original = get_evidence(station, self.batch, 'A')
        with self.desk() as station:
            initialize(station, self.data)
            self.assertEqual(take(self.device, 'new-take', self.batch, ['A']).readings, ())
            self.assertEqual(get_evidence(station, self.batch, 'A'), original)
            self.assertEqual(self.device.taken_count(self.batch, 'A'), 1)

    def test_reopen_preserves_station(self):
        with self.desk() as station:
            initialize(station, self.data)
            station.register_batch('extra')
            station.register_slots('extra', ('Z',))
            extra = station.order('extra-order', 'extra', ('Z',))
            station.order('single', self.batch, ('A',))
            station.resume('single')
            original = get_evidence(station, self.batch, 'A')
        with open_device_store(self.device_path) as reopened:
            with Station(self.station_path, reopened, shift=self.shift) as station:
                initialize(station, self.data)
                self.assertEqual(station.manifest('extra-order'), extra)
                self.assertEqual(station.register_batch('extra').batch, 'extra')
                self.assertEqual(get_evidence(station, self.batch, 'A'), original)
                self.assertEqual(take(reopened, 'reopen-probe', self.batch, ('A',)).readings, ())
                self.assertEqual(reopened.taken_count(self.batch, 'A'), 1)

    def test_conflicting_load_preserves_original(self):
        with self.desk() as station:
            initialize(station, self.data)
            station.order('single', self.batch, ('A',))
            station.resume('single')
            with self.assertRaises(ValueError):
                station.load_sample(self.batch, 'A', 70, '光谱台/01')
            with self.assertRaises(ValueError):
                station.load_sample(self.batch, 'A', 7, '光谱台/99')
            with self.assertRaises(ValueError):
                station.load_sample(self.batch, 'unknown', 7, '光谱台/01')
            stored = get_evidence(station, self.batch, 'A')
            self.assertEqual((stored.value, stored.position), (7, '光谱台/01'))
            self.assertEqual(self.device.taken_count(self.batch, 'A'), 1)

    def test_lost_reply_and_shared_slot(self):
        self.leave_x()
        self.device.blocked.add(('lookup', 'x'))
        with self.desk() as station:
            first_y = station.resume('Y')
            self.assertEqual(first_y.adopted, ('C',))
            self.assertEqual(first_y.pending, ('B',))
            self.assertIn('x', first_y.retry_actions)
            self.assertIsNone(get_evidence(station, self.batch, 'B'))
            self.assertEqual(get_evidence(station, self.batch, 'C').value, 13)
        self.device.blocked.remove(('lookup', 'x'))
        receipt = source_receipt(self.device, 'x').receipt
        self.assertEqual(tuple(sample.value for sample in receipt.readings), (7, 11))
        with self.desk() as station:
            y = station.resume('Y')
            self.assertEqual((y.adopted, y.pending), (('B', 'C'), ()), 'SHARED_SAMPLE_INCOMPLETE')
            b = get_evidence(station, self.batch, 'B')
            self.assertEqual((b.value, b.action_id, b.receipt_id, b.position), (11, 'x', 'device:x', '光谱台/02'))
        self.assertEqual(self.device.taken_count(self.batch, 'B'), 1)
        self.assertEqual(source_receipt(self.device, 'x').status, 'confirmed')

    def test_action_replay(self):
        with self.desk() as station:
            initialize(station, self.data)
            original = station.start_capture('X', 'x').receipt
            self.assertEqual(take(self.device, 'x', self.batch, ('B', 'A')), original)
            with self.assertRaises(ValueError):
                take(self.device, 'x', self.batch, ('B',))
            with self.assertRaises(ValueError):
                source_receipt(self.device, 'x', confirm='device:some-other-action')
            self.assertEqual(source_receipt(self.device, 'x').receipt, original)
            station.resume('X')
        with self.assertRaises(ReceiptConfirmed) as caught:
            take(self.device, 'x', self.batch, ('A', 'B'))
        self.assertEqual(caught.exception.receipt_id, 'device:x')
        self.assertEqual(source_receipt(self.device, 'x', confirm='device:x').status, 'confirmed')
        self.assertEqual(self.device.taken_count(self.batch, 'B'), 1)

    def test_logged_action_not_executed(self):
        with self.desk() as station:
            initialize(station, self.data)
            self.device.blocked.add(('take', 'x'))
            self.assertTrue(station.start_capture('X', 'x').uncertain)
            self.assertEqual(get_intent(station, 'x').slots, ('A', 'B'))
        state = source_receipt(self.device, 'x')
        self.assertEqual(state.status, 'not_executed')
        self.assertIsNone(state.receipt)
        self.assertEqual(self.device.taken_count(self.batch, 'B'), 0)
        self.device.blocked.remove(('take', 'x'))
        with self.desk() as station:
            self.assertEqual(station.resume('Y').pending, ())
            self.assertEqual(get_evidence(station, self.batch, 'B').action_id, 'x')
            self.assertEqual(get_committed_receipt(station, 'x'), 'device:x')
        self.assertEqual(self.device.taken_count(self.batch, 'B'), 1)

    def test_lookup_unavailable_is_retryable(self):
        self.leave_x()
        self.device.blocked.add(('lookup', 'x'))
        with self.assertRaises(DeviceUnavailable):
            source_receipt(self.device, 'x')
        with self.desk() as station:
            report = station.resume('Y')
            self.assertEqual((report.adopted, report.pending), (('C',), ('B',)))
            self.assertIn('x', report.retry_actions)
        self.device.blocked.remove(('lookup', 'x'))
        with self.desk() as station:
            self.assertEqual(station.resume('Y').pending, ())
            self.assertEqual(get_evidence(station, self.batch, 'B').action_id, 'x')

    def test_batch_isolation(self):
        x, y = self.issue_pair()
        with self.desk() as station:
            station.register_batch('second')
            station.register_slots('second', ('B',))
            station.load_sample('second', 'B', 37, '光谱台/12')
            station.order('Z', 'second', ('B',))
            self.assertEqual(station.resume('Z').pending, ())
            z = station.issue('Z')
            first = get_evidence(station, self.batch, 'B')
            second = get_evidence(station, 'second', 'B')
            self.assertEqual((first.value, first.position), (11, '光谱台/02'))
            self.assertEqual((second.value, second.position), (37, '光谱台/12'))
            self.assertEqual(x.lines[1], y.lines[0])
            self.assertNotEqual(y.lines[0].device_receipt_id, z.lines[0].device_receipt_id)
            self.assertEqual(z.total, 37)

    def test_confirm_after_local_evidence(self):
        with self.desk() as station:
            initialize(station, self.data)
            station.order('single', self.batch, ('A',))
            dispatch = station.start_capture('single', 'a')
            station.reject_next_commit('adopt')
            with self.assertRaisesRegex(CommitRejected, 'adopt'):
                station.resume('single')
            self.assertIsNone(get_evidence(station, self.batch, 'A'))
            self.assertIsNone(get_committed_receipt(station, 'a'))
        with open_device_store(self.device_path) as reopened:
            before = source_receipt(reopened, 'a')
            self.assertEqual(before.receipt, dispatch.receipt)
            with Station(self.station_path, reopened) as station:
                self.assertEqual(station.resume('single').pending, ())
                original = get_evidence(station, self.batch, 'A')
                self.assertEqual((original.value, original.receipt_id), (7, 'device:a'))
            self.assertEqual(source_receipt(reopened, 'a').status, 'confirmed')
            self.assertEqual(reopened.taken_count(self.batch, 'A'), 1)

    def test_release_retry_after_unavailability_and_reply_loss(self):
        with self.desk() as station:
            initialize(station, self.data)
            station.start_capture('X', 'x')
            self.device.blocked.add(('confirm', 'x'))
            self.assertEqual(station.resume('X').retry_actions, ('x',))
            self.assertEqual(get_intent(station, 'x').status, 'adopted')
        with open_device_store(self.device_path) as reopened:
            self.assertEqual(source_receipt(reopened, 'x').status, 'unconfirmed')
            reopened.drop_replies.add(('confirm', 'x'))
            with Station(self.station_path, reopened) as station:
                self.assertEqual(get_evidence(station, self.batch, 'B').value, 11)
                self.assertEqual(station.resume('X').retry_actions, ('x',))
                self.assertEqual(get_intent(station, 'x').status, 'adopted')
                self.assertEqual(source_receipt(reopened, 'x').status, 'confirmed')
                self.assertEqual(station.resume('X').retry_actions, ())
                self.assertEqual(get_intent(station, 'x').status, 'released')
            self.assertEqual(reopened.taken_count(self.batch, 'B'), 1)

    def test_dispatch_rollback_does_not_take(self):
        with self.desk() as station:
            initialize(station, self.data)
            station.reject_next_commit('dispatch')
            with self.assertRaisesRegex(CommitRejected, 'dispatch'):
                station.start_capture('X', 'x')
            with self.assertRaises(KeyError):
                get_intent(station, 'x')
            self.assertEqual(source_receipt(self.device, 'x').status, 'not_executed')
            self.assertEqual(self.device.taken_count(self.batch, 'A'), 0)
