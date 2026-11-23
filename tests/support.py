import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from device import open_device_store
from station import SAMPLE, Station, initialize


class StationCase(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix='spectral-test-')
        self.addCleanup(directory.cleanup)
        self.station_path = Path(directory.name) / 'station.sqlite'
        self.device_path = Path(directory.name) / 'device.sqlite'
        self.data = json.loads(SAMPLE.read_text(encoding='utf-8'))
        self.batch = self.data['batch']
        self.shift = self.data['shift']
        self.device = open_device_store(self.device_path)
        self.addCleanup(self.device.close)

    def desk(self, *, offline=False, shift=None):
        return Station(self.station_path, None if offline else self.device, shift=shift or self.shift)

    def leave_x(self):
        with self.desk() as caller:
            initialize(caller, self.data)
            self.device.drop_replies.add(('take', 'x'))
            outcome = caller.start_capture('X', 'x')
            self.assertTrue(outcome.uncertain)
            self.assertIsNone(outcome.receipt)

    def issue_pair(self):
        self.leave_x()
        with self.desk() as station:
            self.assertEqual(station.resume('Y').pending, ())
            y = station.issue('Y')
            self.assertEqual(station.resume('X').pending, ())
            x = station.issue('X')
        return x, y

    def publication_counts(self, station, order_id):
        return tuple(
            station.db.execute(statement, (order_id,)).fetchone()[0]
            for statement in (
                'SELECT COUNT(*) FROM issued_results WHERE order_id = ?',
                'SELECT COUNT(*) FROM issue_receipts WHERE order_id = ?',
            )
        )
