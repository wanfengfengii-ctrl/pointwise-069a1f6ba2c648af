from device import source_receipt
from issuance.receipts import FrozenMismatch
from tests.support import StationCase


class HistoryTests(StationCase):
    def test_issue_and_audit_offline(self):
        x, y = self.issue_pair()
        self.assertEqual(source_receipt(self.device, 'x').status, 'confirmed')
        self.assertIsNone(source_receipt(self.device, 'x').receipt)
        self.assertEqual(self.device.taken_count(self.batch, 'B'), 1)
        self.device.close()
        with self.desk(offline=True, shift='晚班') as station:
            self.assertEqual(station.read(x.result_id), x)
            self.assertEqual(station.read(y.result_id), y)
            ax = station.reconcile(self.shift, x.result_id)
            ay = station.reconcile(self.shift, y.result_id)
            self.assertEqual((ax.total, ay.total), (18, 24))
            self.assertEqual([line.value for line in x.lines], [7, 11])
            self.assertEqual([line.value for line in y.lines], [11, 13])
            self.assertEqual([line.slot for line in ax.sources], ['A', 'B'])
            self.assertEqual([line.slot for line in ay.sources], ['B', 'C'])
            self.assertEqual(ax.sources[1], ay.sources[0])
            self.assertEqual((ay.sources[0].position, ay.sources[0].action_id, ay.sources[0].device_receipt_id),
                             ('光谱台/02', 'x', 'device:x'))
            self.assertEqual(station.export_sources(y.result_id), ay.sources)
            self.assertEqual((ax.receipt.receipt_id, ay.receipt.receipt_id), ('first:X', 'first:Y'))

    def test_unknown_result(self):
        with self.desk(offline=True) as station:
            with self.assertRaises(KeyError):
                station.read('result:unknown')

    def test_shift_listing(self):
        x, y = self.issue_pair()
        with self.desk(offline=True, shift='晚班') as station:
            self.assertEqual(station.list_shift_results(self.shift), (x, y))
            self.assertEqual(station.list_shift_results('晚班'), ())
            self.assertEqual(station.issue('Y'), y)
            self.assertEqual(station.list_shift_results('晚班'), ())

    def test_wrong_shift_rejected(self):
        _, y = self.issue_pair()
        with self.desk(offline=True, shift='晚班') as station:
            with self.assertRaisesRegex(ValueError, 'issued in shift'):
                station.reconcile('晚班', y.result_id)
            self.assertEqual(station.read(y.result_id), y)

    def test_bad_frozen_total_rejected(self):
        _, y = self.issue_pair()
        with self.desk(offline=True) as station:
            with station.write():
                station.db.execute('UPDATE issued_results SET total = ? WHERE result_id = ?', ('25', y.result_id))
        with self.desk(offline=True) as station:
            altered = station.read(y.result_id)
            self.assertEqual((altered.total, [line.value for line in altered.lines]), (25, [11, 13]))
            with self.assertRaisesRegex(FrozenMismatch, 'total differs'):
                station.reconcile(self.shift, y.result_id)

    def test_bad_frozen_source_rejected(self):
        _, y = self.issue_pair()
        with self.desk(offline=True) as station:
            with station.write():
                station.db.execute(
                    'UPDATE issued_lines SET source_slot = ? WHERE result_id = ? AND ordinal = 0',
                    ('C', y.result_id),
                )
        with self.desk(offline=True) as station:
            with self.assertRaisesRegex(FrozenMismatch, 'different slot'):
                station.reconcile(self.shift, y.result_id)

    def test_wrong_first_receipt_rejected(self):
        _, y = self.issue_pair()
        with self.desk(offline=True) as station:
            with station.write():
                station.db.execute(
                    'UPDATE issue_receipts SET receipt_id = ? WHERE result_id = ?', ('unknown-first', y.result_id),
                )
            with self.assertRaisesRegex(FrozenMismatch, 'first receipt'):
                station.reconcile(self.shift, y.result_id)
