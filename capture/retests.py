"""Cross-shift retests: spot planning, actual re-taking, and settlement.

A plan is registered for already-issued orders.  Each order asks for a minimum
number of retest spots; each candidate retained slot declares which orders it
can support and a positive consumable cost.  One slot may support several
orders at once but contributes at most one spot per order.  A plan is the
lexicographically smallest covering subset under ``(total cost, slot count,
ascending slot ids)`` inside the budget; otherwise the request is refused.

Selected slots are durably locked against ordinary capture.  The retest still
uses the original device: an action is recorded, the device actually takes the
samples, and the receipt is adopted and confirmed with the same semantics as
ordinary capture.  Once every assigned spot has evidence, each order's retest
result is signed from that evidence against the plan's frozen per-order list.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass

from capture.actions import find_unsettled_actions
from capture.evidence import get_evidence
from capture.locks import lock_owner, lock_slot, release_plan_locks, retest_order_id
from capture.resume import resume
from issuance.measure import ResultLine, measure_total
from issuance.receipts import IssueReceipt, check_receipt_binding
from issuance.store import load_by_order
from orders.batches import require_text
from orders.manifests import get_manifest
from orders.slots import require_registered_slots


SCHEMA = '''
CREATE TABLE IF NOT EXISTS retest_plans (
    plan_id TEXT PRIMARY KEY NOT NULL,
    batch TEXT NOT NULL REFERENCES trial_batches(batch),
    budget TEXT NOT NULL,
    total_cost TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','signed'))
);
CREATE TABLE IF NOT EXISTS retest_plan_slots (
    plan_id TEXT NOT NULL REFERENCES retest_plans(plan_id),
    ordinal INTEGER NOT NULL,
    slot TEXT NOT NULL,
    cost TEXT NOT NULL,
    PRIMARY KEY (plan_id, slot)
);
CREATE TABLE IF NOT EXISTS retest_plan_support (
    plan_id TEXT NOT NULL REFERENCES retest_plans(plan_id),
    slot TEXT NOT NULL,
    order_id TEXT NOT NULL,
    PRIMARY KEY (plan_id, slot, order_id)
);
CREATE TABLE IF NOT EXISTS retest_plan_orders (
    plan_id TEXT NOT NULL REFERENCES retest_plans(plan_id),
    ordinal INTEGER NOT NULL,
    order_id TEXT NOT NULL,
    required_spots INTEGER NOT NULL,
    original_result_id TEXT NOT NULL,
    original_receipt_id TEXT NOT NULL,
    PRIMARY KEY (plan_id, order_id)
);
CREATE TABLE IF NOT EXISTS retest_plan_assignments (
    plan_id TEXT NOT NULL REFERENCES retest_plans(plan_id),
    order_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    slot TEXT NOT NULL,
    PRIMARY KEY (plan_id, order_id, slot)
);
CREATE TABLE IF NOT EXISTS retest_results (
    result_id TEXT PRIMARY KEY NOT NULL,
    plan_id TEXT NOT NULL REFERENCES retest_plans(plan_id),
    order_id TEXT UNIQUE NOT NULL REFERENCES manifests(order_id),
    batch TEXT NOT NULL,
    slots TEXT NOT NULL,
    total TEXT NOT NULL,
    shift TEXT NOT NULL,
    original_result_id TEXT NOT NULL,
    original_receipt_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS retest_lines (
    result_id TEXT NOT NULL REFERENCES retest_results(result_id),
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
CREATE TABLE IF NOT EXISTS retest_receipts (
    receipt_id TEXT PRIMARY KEY NOT NULL,
    result_id TEXT UNIQUE NOT NULL REFERENCES retest_results(result_id),
    order_id TEXT UNIQUE NOT NULL REFERENCES manifests(order_id)
);
CREATE INDEX IF NOT EXISTS retests_by_shift ON retest_results(shift, order_id);
'''


RETEST_RESULT_PREFIX = 'retest:'
RETEST_RECEIPT_PREFIX = 'retest-first:'


class RetestRefused(ValueError):
    """No plan inside the budget can cover every registered requirement."""

    def __init__(self, batch, budget, pending, reason):
        super().__init__(f'retest plan refused for batch {batch!r}: {reason}')
        self.batch = batch
        self.budget = budget
        self.pending = tuple(pending)
        self.reason = reason


@dataclass(frozen=True)
class RetestCandidate:
    slot: str
    orders: tuple[str, ...]
    cost: int


@dataclass(frozen=True)
class RetestRequirement:
    order_id: str
    spots: int


@dataclass(frozen=True)
class RetestPlan:
    plan_id: str
    batch: str
    budget: int
    total_cost: int
    slots: tuple[str, ...]
    requirements: tuple[RetestRequirement, ...]
    assignments: Mapping[str, tuple[str, ...]]
    original_results: Mapping[str, str]
    original_receipts: Mapping[str, str]
    status: str = 'planned'


@dataclass(frozen=True)
class RetestResume:
    plan_id: str
    adopted: tuple[str, ...]
    pending: tuple[str, ...]
    retry_actions: tuple[str, ...]


@dataclass(frozen=True)
class RetestResult:
    result_id: str
    plan_id: str
    order_id: str
    batch: str
    slots: tuple[str, ...]
    lines: tuple[ResultLine, ...]
    total: int
    shift: str
    original_result_id: str
    original_receipt_id: str
    receipt: IssueReceipt


@dataclass(frozen=True)
class RetestSettlement:
    plan_id: str
    results: tuple[RetestResult, ...]
    pending: tuple[str, ...]


@dataclass(frozen=True)
class RetestReconciliation:
    result_id: str
    shift: str
    sources: tuple[ResultLine, ...]
    total: int
    receipt: IssueReceipt
    original_result_id: str
    original_receipt_id: str


def retest_result_id(order_id):
    return f'{RETEST_RESULT_PREFIX}{require_text(order_id, "order id")}'


def retest_receipt_id(order_id):
    return f'{RETEST_RECEIPT_PREFIX}{require_text(order_id, "order id")}'


def _positive_int(value, field):
    if type(value) is not int or value <= 0:
        raise ValueError(f'{field} must be a positive integer')
    return value


def _nonnegative_int(value, field):
    if type(value) is not int or value < 0:
        raise ValueError(f'{field} must be a nonnegative integer')
    return value


def _requirement_entries(requirements):
    if isinstance(requirements, Mapping):
        entries = list(requirements.items())
    elif isinstance(requirements, (str, bytes, bytearray)):
        raise ValueError('requirements must map orders to positive spot counts')
    else:
        try:
            entries = list(requirements)
        except TypeError as error:
            raise ValueError('requirements must map orders to positive spot counts') from error
    normalized = []
    seen = set()
    for entry in entries:
        if isinstance(entry, RetestRequirement):
            order_id, spots = entry.order_id, entry.spots
        else:
            try:
                order_id, spots = entry
            except (TypeError, ValueError) as error:
                raise ValueError('each requirement is an (order, spots) pair') from error
        order_id = require_text(order_id, 'order id')
        if order_id in seen:
            raise ValueError(f'duplicate retest requirement for order {order_id!r}')
        seen.add(order_id)
        normalized.append(RetestRequirement(order_id, _positive_int(spots, 'required spots')))
    if not normalized:
        raise ValueError('a retest plan requires at least one order')
    return tuple(normalized)


def _candidate_entries(candidates):
    if isinstance(candidates, (str, bytes, bytearray, Mapping)):
        raise ValueError('candidates must be a sequence of slot registrations')
    try:
        entries = list(candidates)
    except TypeError as error:
        raise ValueError('candidates must be a sequence of slot registrations') from error
    if len(entries) > 24:
        raise ValueError('a retest plan accepts at most 24 candidate slots')
    normalized = []
    seen = set()
    for entry in entries:
        if isinstance(entry, RetestCandidate):
            slot, orders, cost = entry.slot, entry.orders, entry.cost
        elif isinstance(entry, Mapping):
            try:
                slot, orders, cost = entry['slot'], entry['orders'], entry['cost']
            except KeyError as error:
                raise ValueError('a candidate needs slot, orders and cost') from error
        elif isinstance(entry, (tuple, list)):
            if len(entry) != 3:
                raise ValueError('a candidate is a (slot, supported orders, cost) triple')
            slot, orders, cost = entry
        else:
            try:
                slot, orders, cost = entry.slot, entry.orders, entry.cost
            except AttributeError as error:
                raise ValueError('a candidate needs slot, orders and cost') from error
        slot = require_text(slot, 'candidate slot')
        if slot in seen:
            raise ValueError(f'duplicate candidate slot {slot!r}')
        seen.add(slot)
        if isinstance(orders, (str, bytes, bytearray, Mapping)):
            raise ValueError(f'candidate {slot!r} orders must be a collection')
        try:
            names = tuple(require_text(order, 'supported order') for order in orders)
        except TypeError as error:
            raise ValueError(f'candidate {slot!r} orders must be iterable') from error
        normalized.append(RetestCandidate(slot, tuple(sorted(set(names))), _positive_int(cost, 'cost')))
    return tuple(sorted(normalized, key=lambda candidate: candidate.slot))


def _fingerprint(batch, requirements, candidates, budget):
    return json.dumps(
        {
            'batch': batch,
            'budget': budget,
            'requirements': [[requirement.order_id, requirement.spots] for requirement in requirements],
            'candidates': [
                [candidate.slot, list(candidate.orders), candidate.cost] for candidate in candidates
            ],
        },
        ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    )


def _select(requirements, candidates, budget):
    """Return the lexicographically smallest covering subset of candidate slots.

    The comparison key is ``(total cost, slot count, ascending chosen indices)``.
    Candidates are ordered by ascending slot id, so the index tuple orders the
    same way as the slot-id tuple.
    """
    order_ids = [requirement.order_id for requirement in requirements]
    required = [requirement.spots for requirement in requirements]
    count = len(candidates)
    width = len(order_ids)
    support = [
        tuple(1 if order_id in candidate.orders else 0 for order_id in order_ids)
        for candidate in candidates
    ]
    suffix = [[0] * width for _ in range(count + 1)]
    for index in range(count - 1, -1, -1):
        for column in range(width):
            suffix[index][column] = suffix[index + 1][column] + support[index][column]

    def covers(chosen):
        covered = [0] * width
        for index in chosen:
            for column in range(width):
                covered[column] += support[index][column]
        return all(covered[column] >= required[column] for column in range(width))

    # Cheap initial upper bound: greedily add the cheapest useful slots.
    greedy = []
    remaining = list(required)
    for index in sorted(range(count), key=lambda item: (candidates[item].cost, item)):
        if all(value <= 0 for value in remaining):
            break
        if not any(support[index][column] and remaining[column] > 0 for column in range(width)):
            continue
        greedy.append(index)
        for column in range(width):
            if support[index][column]:
                remaining[column] -= 1
        remaining = [max(0, value) for value in remaining]
    if all(value <= 0 for value in remaining) and sum(candidates[i].cost for i in greedy) <= budget:
        greedy.sort()
        if covers(greedy):
            best = (
                (sum(candidates[i].cost for i in greedy), len(greedy), tuple(greedy)),
                tuple(greedy),
            )
        else:
            best = None
    else:
        best = None

    chosen = []
    covered = [0] * width
    total_cost = 0
    # A state records how many spots are already covered from each order at a
    # given candidate index.  Reaching the same state at a strictly higher cost
    # can never win, because the remaining choices are identical.
    reached = {}

    def search(index):
        nonlocal best, total_cost
        if all(covered[column] >= required[column] for column in range(width)):
            key = (total_cost, len(chosen), tuple(chosen))
            if best is None or key < best[0]:
                best = (key, tuple(chosen))
            return
        if index == count or total_cost > budget:
            return
        for column in range(width):
            if covered[column] + suffix[index][column] < required[column]:
                return
        if best is not None and total_cost >= best[0][0]:
            return
        state = (index, tuple(min(covered[column], required[column]) for column in range(width)))
        previous = reached.get(state)
        if previous is not None and previous < total_cost:
            return
        reached[state] = total_cost
        candidate = candidates[index]
        if total_cost + candidate.cost <= budget:
            chosen.append(index)
            total_cost += candidate.cost
            for column in range(width):
                covered[column] += support[index][column]
            search(index + 1)
            for column in range(width):
                covered[column] -= support[index][column]
            total_cost -= candidate.cost
            chosen.pop()
        search(index + 1)

    search(0)
    if best is None:
        return None
    return tuple(candidates[index] for index in best[1])


def _plan_row(station, plan_id):
    return station.db.execute(
        'SELECT * FROM retest_plans WHERE plan_id = ?', (plan_id,),
    ).fetchone()


def get_retest_plan(station, plan_id):
    plan_id = require_text(plan_id, 'plan id')
    row = _plan_row(station, plan_id)
    if row is None:
        raise KeyError(plan_id)
    requirements = []
    assignments = {}
    original_results = {}
    original_receipts = {}
    for record in station.db.execute(
            'SELECT * FROM retest_plan_orders WHERE plan_id = ? ORDER BY ordinal', (plan_id,)):
        requirement = RetestRequirement(record['order_id'], record['required_spots'])
        requirements.append(requirement)
        original_results[requirement.order_id] = record['original_result_id']
        original_receipts[requirement.order_id] = record['original_receipt_id']
        assignments[requirement.order_id] = tuple(
            line['slot'] for line in station.db.execute(
                '''SELECT slot FROM retest_plan_assignments
                   WHERE plan_id = ? AND order_id = ? ORDER BY ordinal''',
                (plan_id, requirement.order_id),
            )
        )
    slots = tuple(
        record['slot'] for record in station.db.execute(
            'SELECT slot FROM retest_plan_slots WHERE plan_id = ? ORDER BY ordinal', (plan_id,),
        )
    )
    return RetestPlan(
        plan_id, row['batch'], int(row['budget'], 0), int(row['total_cost'], 0), slots,
        tuple(requirements), assignments, original_results, original_receipts, row['status'],
    )


def _validate_orders(station, requirements):
    batch = None
    originals = {}
    receipts = {}
    for requirement in requirements:
        manifest = get_manifest(station, requirement.order_id)
        if batch is None:
            batch = manifest.batch
        elif manifest.batch != batch:
            raise ValueError('all retest orders must belong to one batch')
        result = load_by_order(station, requirement.order_id)
        if result is None:
            raise ValueError(f'order {requirement.order_id!r} has no issued original result')
        if result.batch != batch:
            raise ValueError(f'original result for {requirement.order_id!r} is in another batch')
        check_receipt_binding(result.receipt, requirement.order_id, result.result_id)
        originals[requirement.order_id] = result.result_id
        receipts[requirement.order_id] = result.receipt.receipt_id
    return batch, originals, receipts


def _validate_candidate_slots(station, batch, candidates):
    require_registered_slots(station, batch, [candidate.slot for candidate in candidates])
    candidates_by_slot = {candidate.slot: candidate for candidate in candidates}
    unsettled = find_unsettled_actions(station, batch, tuple(candidates_by_slot))
    touched = sorted({slot for intent in unsettled for slot in intent.slots if slot in candidates_by_slot})
    if touched:
        raise ValueError(f'retained slots have unsettled capture actions: {touched!r}')
    for candidate in candidates:
        slot = candidate.slot
        if get_evidence(station, batch, slot) is not None:
            raise ValueError(f'retained slot {slot!r} has already been taken')
        if lock_owner(station, batch, slot) is not None:
            raise ValueError(f'retained slot {slot!r} is locked by another retest plan')
        device = station.device
        if device is not None and not device.sample_available(batch, slot):
            raise ValueError(f'retained slot {slot!r} is not an untaken loaded sample')


def _assignments(requirements, chosen, support):
    """Pick exactly each order's required spots from the chosen slots.

    ``chosen`` is in ascending slot order, so the frozen per-order list is the
    first ``spots`` supporting slots in that order.  A slot appears at most
    once per order, and a shared slot may serve several orders at once.
    """
    assigned = {}
    for requirement in requirements:
        slots = tuple(
            candidate.slot for candidate in chosen
            if requirement.order_id in support[candidate.slot]
        )[: requirement.spots]
        assigned[requirement.order_id] = slots
    return assigned


def plan_retests(station, requirements, candidates, budget, plan_id=None):
    requirements = _requirement_entries(requirements)
    candidates = _candidate_entries(candidates)
    budget = _nonnegative_int(budget, 'budget')
    if plan_id is not None:
        plan_id = require_text(plan_id, 'plan id')
        if plan_id.startswith('@retest-plan:'):
            raise ValueError('plan id prefix @retest-plan: is reserved')

    batch, originals, receipts = _validate_orders(station, requirements)
    known_orders = {requirement.order_id for requirement in requirements}
    # Orders outside this plan are irrelevant to its coverage.
    effective = tuple(
        RetestCandidate(
            candidate.slot,
            tuple(order for order in candidate.orders if order in known_orders),
            candidate.cost,
        )
        for candidate in candidates
    )
    support = {candidate.slot: candidate.orders for candidate in effective}
    fingerprint = _fingerprint(batch, requirements, effective, budget)

    with station.write('plan'):
        if plan_id is None:
            row = station.db.execute(
                'SELECT plan_id FROM retest_plans WHERE batch = ? AND fingerprint = ?',
                (batch, fingerprint),
            ).fetchone()
            if row is not None:
                return get_retest_plan(station, row['plan_id'])
            sequence = station.db.execute(
                'SELECT COALESCE(MAX(rowid), 0) + 1 FROM retest_plans'
            ).fetchone()[0]
            plan_id = f'@retest-plan:{sequence}'
            while _plan_row(station, plan_id) is not None:
                sequence += 1
                plan_id = f'@retest-plan:{sequence}'
        existing = _plan_row(station, plan_id)
        if existing is not None:
            if existing['fingerprint'] != fingerprint:
                raise ValueError(f'retest plan {plan_id!r} has different parameters')
            return get_retest_plan(station, plan_id)

        for requirement in requirements:
            if load_retest_by_order(station, requirement.order_id) is not None:
                raise ValueError(f'order {requirement.order_id!r} has already been retested')
        _validate_candidate_slots(station, batch, effective)
        chosen = _select(requirements, effective, budget)
        if chosen is None:
            support_count = {requirement.order_id: 0 for requirement in requirements}
            for candidate in effective:
                for order_id in candidate.orders:
                    support_count[order_id] += 1
            starved = [
                requirement.order_id for requirement in requirements
                if support_count[requirement.order_id] < requirement.spots
            ]
            pending = starved or [requirement.order_id for requirement in requirements]
            reason = f'no covering plan within budget {budget}'
            if starved:
                reason += f'; not enough supporting slots: {starved!r}'
            raise RetestRefused(batch, budget, pending, reason)

        total_cost = sum(candidate.cost for candidate in chosen)
        station.db.execute(
            '''INSERT INTO retest_plans(plan_id, batch, budget, total_cost, fingerprint)
               VALUES (?, ?, ?, ?, ?)''',
            (plan_id, batch, hex(budget), hex(total_cost), fingerprint),
        )
        for ordinal, candidate in enumerate(chosen):
            station.db.execute(
                'INSERT INTO retest_plan_slots(plan_id, ordinal, slot, cost) VALUES (?, ?, ?, ?)',
                (plan_id, ordinal, candidate.slot, hex(candidate.cost)),
            )
            for order_id in candidate.orders:
                station.db.execute(
                    'INSERT INTO retest_plan_support(plan_id, slot, order_id) VALUES (?, ?, ?)',
                    (plan_id, candidate.slot, order_id),
                )
            if lock_owner(station, batch, candidate.slot) is not None:
                raise ValueError(f'retained slot {candidate.slot!r} was locked concurrently')
            lock_slot(station, plan_id, batch, candidate.slot)
        for ordinal, requirement in enumerate(requirements):
            station.db.execute(
                '''INSERT INTO retest_plan_orders(plan_id, ordinal, order_id, required_spots,
                                                  original_result_id, original_receipt_id)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (plan_id, ordinal, requirement.order_id, requirement.spots,
                 originals[requirement.order_id], receipts[requirement.order_id]),
            )
        assignments = _assignments(requirements, chosen, support)
        for requirement in requirements:
            if len(assignments[requirement.order_id]) != requirement.spots:
                raise ValueError(f'plan does not cover order {requirement.order_id!r}')
            for ordinal, slot in enumerate(assignments[requirement.order_id]):
                station.db.execute(
                    '''INSERT INTO retest_plan_assignments(plan_id, order_id, ordinal, slot)
                       VALUES (?, ?, ?, ?)''',
                    (plan_id, requirement.order_id, ordinal, slot),
                )
        hidden_id = retest_order_id(plan_id)
        hidden = station.db.execute(
            'SELECT batch, slots FROM manifests WHERE order_id = ?', (hidden_id,)
        ).fetchone()
        frozen = json.dumps([candidate.slot for candidate in chosen])
        if hidden is None:
            station.db.execute(
                'INSERT INTO manifests(order_id, batch, slots) VALUES (?, ?, ?)',
                (hidden_id, batch, frozen),
            )
        elif hidden['batch'] != batch or hidden['slots'] != frozen:
            raise ValueError(f'retest capture manifest for {plan_id!r} conflicts')

    return get_retest_plan(station, plan_id)


def resume_retest(station, plan_id, action_id=None):
    plan = get_retest_plan(station, plan_id)
    hidden_id = retest_order_id(plan.plan_id)
    report = resume(station, hidden_id, action_id)
    adopted = tuple(
        slot for slot in plan.slots if get_evidence(station, plan.batch, slot) is not None
    )
    pending = tuple(slot for slot in plan.slots if slot not in adopted)
    return RetestResume(plan.plan_id, adopted, pending, report.retry_actions)


def _assemble_retest(station, head):
    rows = station.db.execute(
        'SELECT * FROM retest_lines WHERE result_id = ? ORDER BY ordinal', (head['result_id'],),
    )
    lines = tuple(
        ResultLine(
            row['slot'], int(row['value'], 0), row['source_batch'], row['source_slot'],
            row['position'], row['action_id'], row['device_receipt_id'],
        )
        for row in rows
    )
    slots = tuple(json.loads(head['slots']))
    receipt = station.db.execute(
        'SELECT receipt_id, result_id, order_id FROM retest_receipts WHERE result_id = ?',
        (head['result_id'],),
    ).fetchone()
    if receipt is None:
        raise ValueError(f'missing retest receipt for {head["result_id"]!r}')
    return RetestResult(
        head['result_id'], head['plan_id'], head['order_id'], head['batch'], slots, lines,
        int(head['total'], 0), head['shift'], head['original_result_id'],
        head['original_receipt_id'],
        IssueReceipt(receipt['receipt_id'], receipt['result_id'], receipt['order_id']),
    )


def load_retest_by_order(station, order_id):
    row = station.db.execute(
        'SELECT * FROM retest_results WHERE order_id = ?', (require_text(order_id, 'order id'),),
    ).fetchone()
    return None if row is None else _assemble_retest(station, row)


def read_retest(station, result_id):
    result_id = require_text(result_id, 'result id')
    row = station.db.execute(
        'SELECT * FROM retest_results WHERE result_id = ?', (result_id,),
    ).fetchone()
    if row is None:
        raise KeyError(result_id)
    return _assemble_retest(station, row)


def list_retests_by_shift(station, shift):
    shift = require_text(shift, 'shift')
    rows = station.db.execute(
        'SELECT * FROM retest_results WHERE shift = ? ORDER BY order_id', (shift,),
    )
    return tuple(_assemble_retest(station, row) for row in rows)


def _settle_lines(station, batch, slots):
    lines = []
    pending = []
    for slot in slots:
        evidence = get_evidence(station, batch, slot)
        if evidence is None:
            pending.append(slot)
            continue
        lines.append(ResultLine(
            slot, evidence.value, evidence.key.batch, evidence.key.slot, evidence.position,
            evidence.action_id, evidence.receipt_id,
        ))
    return tuple(lines), tuple(pending)


def settle_retests(station, plan_id):
    plan = get_retest_plan(station, plan_id)
    missing = []
    for requirement in plan.requirements:
        _, pending = _settle_lines(station, plan.batch, plan.assignments[requirement.order_id])
        missing.extend(pending)
    if missing:
        return RetestSettlement(plan.plan_id, (), tuple(sorted(set(missing))))

    with station.write('settle'):
        results = []
        for requirement in plan.requirements:
            order_id = requirement.order_id
            lines, _ = _settle_lines(station, plan.batch, plan.assignments[order_id])
            existing = load_retest_by_order(station, order_id)
            if existing is not None:
                if existing.plan_id != plan.plan_id:
                    raise ValueError(f'order {order_id!r} was settled by another retest plan')
                results.append(existing)
                continue
            result_id = retest_result_id(order_id)
            receipt = IssueReceipt(retest_receipt_id(order_id), result_id, order_id)
            station.db.execute(
                '''INSERT INTO retest_results(result_id, plan_id, order_id, batch, slots, total,
                                              shift, original_result_id, original_receipt_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (result_id, plan.plan_id, order_id, plan.batch,
                 json.dumps(plan.assignments[order_id]), hex(measure_total(lines)),
                 station.shift, plan.original_results[order_id], plan.original_receipts[order_id]),
            )
            station.db.executemany(
                '''INSERT INTO retest_lines(result_id, ordinal, slot, value, source_batch,
                                            source_slot, position, action_id, device_receipt_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                [(result_id, ordinal, line.slot, hex(line.value), line.source_batch,
                  line.source_slot, line.position, line.action_id, line.device_receipt_id)
                 for ordinal, line in enumerate(lines)],
            )
            station.db.execute(
                'INSERT INTO retest_receipts(receipt_id, result_id, order_id) VALUES (?, ?, ?)',
                (receipt.receipt_id, result_id, order_id),
            )
            results.append(read_retest(station, result_id))
        station.db.execute(
            "UPDATE retest_plans SET status = 'signed' WHERE plan_id = ?", (plan.plan_id,),
        )
        release_plan_locks(station, plan.plan_id)
    return RetestSettlement(plan.plan_id, tuple(results), ())


def _check_retest_binding(result):
    expected = IssueReceipt(retest_receipt_id(result.order_id), retest_result_id(result.order_id),
                            result.order_id)
    if result.result_id != retest_result_id(result.order_id) or result.receipt != expected:
        raise ValueError(f'retest first receipt does not bind result {result.result_id!r}')


def reconcile_retest(station, shift, result_id):
    shift = require_text(shift, 'shift')
    result = read_retest(station, result_id)
    if result.shift != shift:
        raise ValueError(f'retest result {result_id!r} was issued in shift {result.shift!r}')
    _check_retest_binding(result)
    if (not result.slots or len(set(result.slots)) != len(result.slots)
            or tuple(line.slot for line in result.lines) != result.slots):
        raise ValueError(f'retest details do not match the frozen list for {result_id!r}')
    sources = []
    seen = set()
    for line in result.lines:
        if (line.source_batch, line.source_slot) != (result.batch, line.slot):
            raise ValueError(f'retest source points to a different slot for {line.slot!r}')
        if (line.source_batch, line.source_slot) in seen:
            raise ValueError(f'retest list repeats a source slot for {line.slot!r}')
        seen.add((line.source_batch, line.source_slot))
        require_text(line.position, 'device position')
        require_text(line.action_id, 'device action id')
        require_text(line.device_receipt_id, 'device receipt id')
        sources.append(line)
    total = measure_total(result.lines)
    if total != result.total:
        raise ValueError(f'frozen retest total differs from detail for {result_id!r}')
    if result.original_receipt_id != f'first:{result.order_id}':
        raise ValueError(f'original first receipt does not bind order {result.order_id!r}')
    if result.original_result_id != f'result:{result.order_id}':
        raise ValueError(f'original result does not bind order {result.order_id!r}')
    return RetestReconciliation(
        result.result_id, shift, tuple(sources), total, result.receipt,
        result.original_result_id, result.original_receipt_id,
    )
