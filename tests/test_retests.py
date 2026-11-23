from capture.evidence import get_evidence
from capture.locks import SampleLocked, is_locked
from capture.retests import RetestRefused
from device import source_receipt
from station import CommitRejected, Station
from tests.support import StationCase


RETEST_BATCH = '复验批'
LOAD = (
    ('P', 5, '光谱台/21'),
    ('Q', 6, '光谱台/22'),
    ('X', 70, '光谱台/31'),
    ('Y', 80, '光谱台/32'),
    ('Z', 90, '光谱台/33'),
    ('W', 40, '光谱台/34'),
)
SHARED_CANDIDATES = (
    ('X', ('甲',), 2),
    ('Y', ('乙',), 3),
    ('Z', ('甲', '乙'), 4),
)


class RetestCase(StationCase):
    """Two already-issued orders with three untaken retained slots."""

    def setUp(self):
        super().setUp()
        with self.desk() as station:
            station.register_batch(RETEST_BATCH)
            station.register_slots(RETEST_BATCH, tuple(slot for slot, _, _ in LOAD))
            for slot, value, position in LOAD:
                station.load_sample(RETEST_BATCH, slot, value, position)
            station.order('甲', RETEST_BATCH, ('P',))
            station.order('乙', RETEST_BATCH, ('Q',))
            self.assertEqual(station.resume('甲').pending, ())
            self.original_jia = station.issue('甲')
            self.assertEqual(station.resume('乙').pending, ())
            self.original_yi = station.issue('乙')

    def plan(self, station, budget=4, requirements=(('甲', 1), ('乙', 1)),
             candidates=SHARED_CANDIDATES):
        return station.plan_retests(requirements, candidates, budget)

    def assert_originals_intact(self, station):
        self.assertEqual(station.result_for('甲'), self.original_jia)
        self.assertEqual(station.result_for('乙'), self.original_yi)
        self.assertEqual(station.read(self.original_jia.result_id), self.original_jia)
        self.assertEqual(station.read(self.original_yi.result_id), self.original_yi)


class RetestPlanTests(RetestCase):
    def test_local_greedy_would_miss_the_shared_slot(self):
        with self.desk() as station:
            plan = self.plan(station)
            self.assertEqual(plan.slots, ('Z',))
            self.assertEqual(plan.total_cost, 4)
            self.assertEqual(plan.assignments, {'甲': ('Z',), '乙': ('Z',)})
            self.assertEqual(plan.original_results,
                             {'甲': self.original_jia.result_id, '乙': self.original_yi.result_id})
            self.assertEqual(plan.original_receipts, {'甲': 'first:甲', '乙': 'first:乙'})
            # Cheapest-first greedy takes X (2) then must add another slot for 乙
            # (3), total 5 > 4; the shared Z (4) is the only budget-feasible cover.
            self.assertGreater(2 + 3, 4)
        with self.desk(offline=True) as station:
            self.assertEqual(station.retest_plan(plan.plan_id), plan)

    def test_locks_only_selected_slots(self):
        with self.desk() as station:
            self.plan(station)
            locked = tuple(slot for slot in ('X', 'Y', 'Z')
                           if is_locked(station, RETEST_BATCH, slot))
            self.assertEqual(locked, ('Z',))

    def test_slot_contributes_at_most_one_spot_per_order(self):
        with self.desk() as station:
            with self.assertRaises(RetestRefused):
                self.plan(station, requirements=(('甲', 2),),
                          candidates=(('Z', ('甲',), 4),), budget=10)

    def test_refusal_when_no_cover_within_budget(self):
        with self.desk() as station:
            with self.assertRaises(RetestRefused) as caught:
                self.plan(station, budget=3)
            self.assertEqual(set(caught.exception.pending), {'甲', '乙'})
            self.assertEqual(station.db.execute('SELECT COUNT(*) FROM retest_plans').fetchone()[0], 0)
            self.assertEqual(station.db.execute('SELECT COUNT(*) FROM retest_locks').fetchone()[0], 0)
            for slot in ('X', 'Y', 'Z'):
                self.assertFalse(is_locked(station, RETEST_BATCH, slot))

    def test_cost_prefers_lower_total(self):
        with self.desk() as station:
            plan = self.plan(
                station, requirements=(('甲', 1),),
                candidates=(('X', ('甲',), 2), ('Y', ('甲',), 3)), budget=5,
            )
            self.assertEqual(plan.slots, ('X',))

    def test_tie_prefers_fewer_slots(self):
        with self.desk() as station:
            plan = self.plan(
                station, requirements=(('甲', 1),),
                candidates=(('X', ('甲',), 2), ('Y', ('甲',), 2), ('Z', ('甲',), 1)), budget=5,
            )
            self.assertEqual(plan.slots, ('Z',))

    def test_tie_prefers_ascending_slot_ids(self):
        with self.desk() as station:
            plan = self.plan(
                station, requirements=(('甲', 1), ('乙', 1)),
                candidates=(('Y', ('甲', '乙'), 4), ('X', ('甲', '乙'), 4)), budget=4,
            )
            self.assertEqual(plan.slots, ('X',))

    def test_idempotent_and_rejects_changed_parameters(self):
        with self.desk() as station:
            first = self.plan(station, requirements=(('甲', 1),),
                              candidates=(('X', ('甲',), 2), ('Y', ('甲',), 2)), budget=4)
            again = self.plan(station, requirements=(('甲', 1),),
                              candidates=(('Y', ('甲',), 2), ('X', ('甲',), 2)), budget=4)
            self.assertEqual(again, first)
            with self.assertRaises(ValueError):
                station.plan_retests((('甲', 1),), (('X', ('甲',), 2),), 3, plan_id=first.plan_id)

    def test_requires_issued_original_and_untaken_slot(self):
        with self.desk() as station:
            station.order('未签', RETEST_BATCH, ('W',))
            with self.assertRaises(ValueError):
                self.plan(station, requirements=(('未签', 1),),
                          candidates=(('W', ('未签',), 1),), budget=1)
            with self.assertRaises(ValueError):
                self.plan(station, requirements=(('甲', 1),),
                          candidates=(('P', ('甲',), 1),), budget=1)

    def test_twenty_four_slots_is_the_hard_cap(self):
        with self.desk() as station:
            slots = tuple(f'{RETEST_BATCH}-{index:02d}' for index in range(24))
            station.register_slots(RETEST_BATCH, slots + ('原据',))
            for slot in slots + ('原据',):
                station.load_sample(RETEST_BATCH, slot, 1, f'光谱台/{slot}')
            station.order('大单', RETEST_BATCH, ('原据',))
            station.resume('大单')
            station.issue('大单')
            candidates = tuple((slot, ('大单',), 1) for slot in slots)
            plan = station.plan_retests((('大单', 24),), candidates, 24)
            self.assertEqual(plan.slots, slots)
            self.assertEqual(plan.total_cost, 24)
            with self.assertRaises(ValueError):
                station.plan_retests((('大单', 1),), candidates + ((slots[0], ('大单',), 1),), 1)

    def test_plan_survives_reopen_and_offline_settlement(self):
        with self.desk() as station:
            plan = self.plan(station)
            station.resume_retest(plan.plan_id, 'rt-off')
        with self.desk(offline=True) as station:
            report = station.resume_retest(plan.plan_id, 'rt-off')
            self.assertEqual((report.adopted, report.pending), (('Z',), ()))
            settlement = station.settle_retests(plan.plan_id)
            self.assertEqual(settlement.pending, ())
            self.assertEqual([result.total for result in settlement.results], [90, 90])

    def test_parameter_validation(self):
        with self.desk() as station:
            for requirements in (
                (), {'甲': 0}, {'甲': -1}, {'甲': True}, {'甲': 1.0},
                (('甲', 1), ('甲', 1)),
            ):
                with self.subTest(requirements=requirements), self.assertRaises(ValueError):
                    station.plan_retests(requirements, (('X', ('甲',), 1),), 5)
            for candidates in (
                (), (('X', ('甲',), 0),), (('X', ('甲',), -2),),
                (('X', ('甲',), 1), ('X', ('甲',), 1)),
                tuple((f'S{index}', ('甲',), 1) for index in range(25)),
            ):
                with self.subTest(candidates=candidates), self.assertRaises(ValueError):
                    station.plan_retests((('甲', 1),), candidates, 5)
            with self.assertRaises(ValueError):
                station.plan_retests((('甲', 1),), (('X', ('甲',), 1),), -1)
            with self.assertRaises(ValueError):
                station.plan_retests((('甲', 1),), (('X', ('甲',), 1),), 5,
                                     plan_id='@retest-plan:1')
            self.assertEqual(station.db.execute('SELECT COUNT(*) FROM retest_plans').fetchone()[0], 0)

    def test_plan_rollback_leaves_no_lock(self):
        with self.desk() as station:
            station.reject_next_commit('plan')
            with self.assertRaisesRegex(CommitRejected, 'plan'):
                self.plan(station)
            self.assertEqual(station.db.execute('SELECT COUNT(*) FROM retest_plans').fetchone()[0], 0)
            self.assertEqual(station.db.execute('SELECT COUNT(*) FROM retest_locks').fetchone()[0], 0)
            self.assertFalse(is_locked(station, RETEST_BATCH, 'Z'))
            plan = self.plan(station)
            self.assertEqual(plan.slots, ('Z',))


class RetestCompetitionTests(RetestCase):
    def test_locked_slot_is_skipped_by_ordinary_capture(self):
        with self.desk() as station:
            self.plan(station)
            station.order('丙', RETEST_BATCH, ('X', 'Z'))
            self.assertEqual(station.resume('丙').pending, ('Z',))
            self.assertEqual(get_evidence(station, RETEST_BATCH, 'X').value, 70)
            self.assertIsNone(get_evidence(station, RETEST_BATCH, 'Z'))
            self.assertEqual(self.device.taken_count(RETEST_BATCH, 'Z'), 0)
            self.assertTrue(is_locked(station, RETEST_BATCH, 'Z'))
            with self.assertRaises(SampleLocked):
                station.start_capture('丙')

    def test_lock_blocks_a_second_plan(self):
        with self.desk() as station:
            station.register_slots(RETEST_BATCH, ('W',))
            station.load_sample(RETEST_BATCH, 'W', 40, '光谱台/34')
            station.order('丁', RETEST_BATCH, ('W',))
            station.resume('丁')
            station.issue('丁')
            self.plan(station)
            with self.assertRaises(ValueError):
                self.plan(station, requirements=(('丁', 1),),
                          candidates=(('Z', ('丁',), 4),), budget=4)

    def test_uncertain_retake_keeps_the_lock(self):
        with self.desk() as station:
            plan = self.plan(station)
            self.device.blocked.add(('take', 'rt-1'))
            report = station.resume_retest(plan.plan_id, 'rt-1')
            self.assertEqual(report.pending, ('Z',))
            self.assertIn('rt-1', report.retry_actions)
            self.assertTrue(is_locked(station, RETEST_BATCH, 'Z'))
        with self.desk() as station:
            self.assertTrue(is_locked(station, RETEST_BATCH, 'Z'))
            self.device.blocked.remove(('take', 'rt-1'))
            report = station.resume_retest(plan.plan_id, 'rt-1')
            self.assertEqual((report.adopted, report.pending, report.retry_actions), (('Z',), (), ()))
            self.assertEqual(self.device.taken_count(RETEST_BATCH, 'Z'), 1)


class RetestSettlementTests(RetestCase):
    def test_shared_slot_taken_once_for_both_orders(self):
        with self.desk() as station:
            plan = self.plan(station)
            report = station.resume_retest(plan.plan_id)
            self.assertEqual((report.adopted, report.pending, report.retry_actions), (('Z',), (), ()))
            settlement = station.settle_retests(plan.plan_id)
            self.assertEqual(settlement.pending, ())
            self.assertEqual(self.device.taken_count(RETEST_BATCH, 'Z'), 1)
            self.assertFalse(is_locked(station, RETEST_BATCH, 'Z'))
            self.assert_originals_intact(station)
            self.assertEqual(station.settle_retests(plan.plan_id), settlement)
            self.assertEqual(station.retest_for('甲'), settlement.results[0] if
                             settlement.results[0].order_id == '甲' else settlement.results[1])
        with self.desk(offline=True) as station:
            self.assertEqual(len(settlement.results), 2)
            by_order = {result.order_id: result for result in settlement.results}
            self.assertEqual(set(by_order), {'甲', '乙'})
            for order_id, original in (('甲', self.original_jia), ('乙', self.original_yi)):
                result = by_order[order_id]
                self.assertEqual(result.slots, ('Z',))
                self.assertEqual([line.slot for line in result.lines], ['Z'])
                self.assertEqual([line.value for line in result.lines], [90])
                self.assertEqual(result.total, 90)
                self.assertEqual(result.receipt.receipt_id, f'retest-first:{order_id}')
                self.assertEqual(result.original_result_id, original.result_id)
                self.assertEqual(result.original_receipt_id, original.receipt.receipt_id)
                self.assertEqual(station.read_retest(result.result_id), result)
                audit = station.reconcile_retest(self.shift, result.result_id)
                self.assertEqual(audit.total, 90)
                self.assertEqual(audit.sources[0].source_slot, 'Z')
                self.assertEqual(audit.sources[0].position, '光谱台/33')
            self.assertEqual(station.retest_for('乙'), by_order['乙'])
            self.assertEqual(
                sorted(station.list_shift_retests(self.shift), key=lambda item: item.order_id),
                sorted(settlement.results, key=lambda item: item.order_id),
            )
            # Both orders share one physical original source.
            self.assertEqual(by_order['甲'].lines[0], by_order['乙'].lines[0])

    def test_settle_waits_and_keeps_lock_until_evidence(self):
        with self.desk() as station:
            plan = self.plan(station)
            settlement = station.settle_retests(plan.plan_id)
            self.assertEqual(settlement, settlement.__class__(plan.plan_id, (), ('Z',)))
            self.assertTrue(is_locked(station, RETEST_BATCH, 'Z'))
            self.assertIsNone(station.retest_for('甲'))
            self.device.blocked.add(('take', 'rt-late'))
            station.resume_retest(plan.plan_id, 'rt-late')
            self.assertEqual(station.settle_retests(plan.plan_id).pending, ('Z',))
            self.device.blocked.remove(('take', 'rt-late'))
            station.resume_retest(plan.plan_id, 'rt-late')
            signed = station.settle_retests(plan.plan_id)
            self.assertEqual(signed.pending, ())
            self.assertEqual(len(signed.results), 2)

    def test_shared_slot_lost_reply_takes_once(self):
        with self.desk() as station:
            plan = self.plan(station)
            self.device.drop_replies.add(('take', 'rt-lost'))
            first = station.resume_retest(plan.plan_id, 'rt-lost')
            self.assertEqual(first.pending, ('Z',))
            self.assertIn('rt-lost', first.retry_actions)
            self.assertIsNone(get_evidence(station, RETEST_BATCH, 'Z'))
            self.assertTrue(is_locked(station, RETEST_BATCH, 'Z'))
        with self.desk() as station:
            second = station.resume_retest(plan.plan_id, 'rt-lost')
            self.assertEqual((second.adopted, second.pending, second.retry_actions), (('Z',), (), ()))
            settlement = station.settle_retests(plan.plan_id)
            self.assertEqual(settlement.pending, ())
            self.assertEqual(self.device.taken_count(RETEST_BATCH, 'Z'), 1)
            self.assertEqual(source_receipt(self.device, 'rt-lost').status, 'confirmed')
            lines = {result.order_id: result.lines[0] for result in settlement.results}
            self.assertEqual(lines['甲'], lines['乙'])
            self.assertEqual((lines['甲'].action_id, lines['甲'].device_receipt_id),
                             ('rt-lost', 'device:rt-lost'))
            self.assertEqual([result.total for result in settlement.results], [90, 90])

    def test_confirm_retry_after_adoption(self):
        with self.desk() as station:
            plan = self.plan(station)
            self.device.blocked.add(('confirm', 'rt-2'))
            report = station.resume_retest(plan.plan_id, 'rt-2')
            self.assertEqual((report.adopted, report.pending), (('Z',), ()))
            self.assertIn('rt-2', report.retry_actions)
            self.assertTrue(is_locked(station, RETEST_BATCH, 'Z'))
            self.device.blocked.remove(('confirm', 'rt-2'))
            report = station.resume_retest(plan.plan_id, 'rt-2')
            self.assertEqual(report.retry_actions, ())
            self.assertEqual(source_receipt(self.device, 'rt-2').status, 'confirmed')
            self.assertEqual(station.settle_retests(plan.plan_id).pending, ())

    def test_settlement_rollback_keeps_locks_and_retry(self):
        with self.desk() as station:
            plan = self.plan(station)
            station.resume_retest(plan.plan_id, 'rt-3')
            station.reject_next_commit('settle')
            with self.assertRaisesRegex(CommitRejected, 'settle'):
                station.settle_retests(plan.plan_id)
            self.assertIsNone(station.retest_for('甲'))
            self.assertIsNone(station.retest_for('乙'))
            self.assertTrue(is_locked(station, RETEST_BATCH, 'Z'))
            self.assertEqual(station.db.execute('SELECT COUNT(*) FROM retest_results').fetchone()[0], 0)
        with self.desk(offline=True) as station:
            self.assertTrue(is_locked(station, RETEST_BATCH, 'Z'))
            signed = station.settle_retests(plan.plan_id)
            self.assertEqual((signed.pending, len(signed.results)), ((), 2))
            self.assertFalse(is_locked(station, RETEST_BATCH, 'Z'))
            self.assertEqual([result.total for result in signed.results], [90, 90])
