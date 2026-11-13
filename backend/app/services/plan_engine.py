"""What to print next for each line of an order — greedy covering (spec pass 3).

**Two greedy passes live in this codebase and they are not the same algorithm.**
``order_metrics.attribute`` deals finished archive part rows out across the lines
that count them: it distributes FACT, one part row at a time, and the question it
answers is "whose print was that". ``cover`` here picks plates to print for ONE
line until its outstanding parts are covered: it plans WORK, one whole plate at a
time, and the question it answers is "what next". They share the word "greedy"
and nothing else — keep the names ``attribute`` and ``cover`` apart in code,
tests and the vault, or a change to one reads as a change to both.

The engine proper (:func:`plan_lines` and everything above it) is pure: no
session or printer state. An optional monotonic deadline bounds calculator work;
ordinary planning does not read the clock. Only :func:`plan_for_orders` (and its
one-order wrapper :func:`plan_for_order`) and :func:`queued_yield_by_line` touch
the database. Routing is not dispatching — nothing here asks whether a printer
is ready, and nothing here may start to.

**Cost.** The app already has one filament price and it is farm-wide:
``services.filament_cost.default_rate_per_kg`` reads the ``default_filament_cost``
setting, a price per KILOGRAM, and is the same source the archive cost estimate
uses (``archive.py`` → ``cost_of(grams, await default_rate_per_kg(db))``).
:func:`plan_for_order` divides it by 1000 for the per-gram price the rows want.
An unset rate is ``0.0`` there and becomes ``price_per_gram=None`` here, so
``cost`` stays ``None`` end to end — never ``0.0``, which would claim the print
was free. No new setting is added by this pass.
"""

from __future__ import annotations

import heapq
import logging
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.product import ProductPlate
from backend.app.models.project_line import ProjectLine
from backend.app.services.filament_cost import default_rate_per_kg
from backend.app.services.forecast_operator_hours import OperatorHours
from backend.app.services.order_filing import line_for_plate
from backend.app.services.order_metrics import (
    LineFigures,
    OrderContext,
    attribute,
    batch_contexts,
    line_accepts_materials,
)
from backend.app.services.order_queue import awaiting_auto_row_conditions, queued_printer_row_conditions
from backend.app.services.product_composition import PlateRecipe, estimate_seconds, recipes_for_products
from backend.app.utils.model_compatibility import model_compatibility
from backend.app.utils.printer_models import normalize_model_name

if TYPE_CHECKING:
    from backend.app.services.farm_forecast import FarmSnapshot

logger = logging.getLogger(__name__)

# A defence, not a feature: a plate whose yield somehow never shrinks the
# outstanding map would otherwise spin forever inside a request.
MAX_ITERATIONS = 10_000

#: Simulated ``FleetCapacity.reserve`` calls the class lookahead may spend in
#: one ``cover`` call, outside the closed form (an applicable operator calendar
#: or fractional times). A class it cannot fully compare chooses on the
#: established next-print key; the deadline is separate and still ends a cover.
#: Measured 2026-10-21 (vault plan-recipe-choice-lookahead-spec): a calendar
#: reserve costs about 7 µs, and plate-fit runs ``cover`` for every quantity it
#: checks — 5000 took its search past its time limit, 1000 keeps it within
#: 1.5× of before.
LOOKAHEAD_SIM_BUDGET = 1_000

#: Integers up to here add exactly in a float, so the closed form equals the
#: float arithmetic ``reserve`` does.
_EXACT_FLOAT_INT = 2**53

#: Below this many prints past the idle prefix, the closed form pops the merged
#: progressions instead of searching them.
_HEAP_MERGE_LIMIT = 64

Candidate = tuple[ProductPlate, LibraryFile, PlateRecipe]


@lru_cache(maxsize=256)
def _model_key(model: str | None) -> str | None:
    """The same normalized model key the auto queue and forecast route on."""
    normalised = normalize_model_name(model) if model else None
    return normalised.strip().lower() if normalised else None


@dataclass
class FleetMachine:
    """One *currently usable* lane in the initial-plan heuristic.

    This is intentionally smaller than the forecast's machine state.  It is a
    snapshot for choosing recipes, not a second dispatcher: its clock starts
    after work already held by the printer and only advances in memory while a
    single order's lines are being covered.
    """

    printer_id: int
    model: str | None
    free_at: float = 0.0
    prep_seconds: float = 0.0
    gap_seconds: float = 0.0
    requires_operator: bool = True

    @property
    def key(self) -> str | None:
        return _model_key(self.model)


@dataclass
class FleetCapacity:
    """Mutable per-order capacity ledger supplied by the farm-snapshot adapter.

    Empty capacity is meaningful: it deliberately preserves the legacy plan
    when a recipe is the only way to make a part.  The forecast can then report
    the actual reason (no such active/eligible model) instead of the planner
    silently turning that demand into an unsatisfied part.
    """

    machines: list[FleetMachine] = field(default_factory=list)
    allow_compatible: bool = False
    operator_hours: OperatorHours | None = None
    auto_eject: bool = False

    def copy(self) -> FleetCapacity:
        return FleetCapacity(
            [
                FleetMachine(m.printer_id, m.model, m.free_at, m.prep_seconds, m.gap_seconds, m.requires_operator)
                for m in self.machines
            ],
            allow_compatible=self.allow_compatible,
            operator_hours=self.operator_hours,
            auto_eject=self.auto_eject,
        )

    def eligible(self, key: str) -> list[FleetMachine]:
        """The lanes a recipe of this model may be reserved on: its own model, or a compatible one."""
        return [
            m
            for m in self.machines
            if m.key == key or (self.allow_compatible and model_compatibility(key, m.key) == "compatible")
        ]

    def preview(self, recipe: PlateRecipe) -> tuple[float, FleetMachine] | None:
        seconds = estimate_seconds(recipe)
        key = _model_key(recipe.printer_model)
        if key is None or seconds is None or seconds <= 0:
            return None
        choices = self.eligible(key)
        if not choices:
            return None
        machine = min(
            choices,
            key=lambda m: (
                0 if m.key == key and m.free_at <= 0 else 1 if m.key != key and m.free_at <= 0 else 2,
                m.free_at + m.prep_seconds + seconds,
                m.printer_id,
            ),
        )
        return machine.free_at + machine.prep_seconds + seconds, machine

    def reserve(self, recipe: PlateRecipe) -> float | None:
        """Place one selected print on its earliest lane, only in this ledger."""
        preview = self.preview(recipe)
        if preview is None:
            return None
        finish, machine = preview
        gap = 0.0 if self.auto_eject else machine.gap_seconds
        machine.free_at = (
            self.operator_hours.service_finish(finish, gap)
            if self.operator_hours and machine.requires_operator and not self.auto_eject
            else finish + gap
        )
        return finish


class _DeadlineExpired(Exception):
    """A cooperative deadline passed inside the lookahead; the cover ends incomplete."""


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise _DeadlineExpired


def _exact_int(value: float) -> int | None:
    """``value`` as an int when it is a finite, non-negative whole number of seconds."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return None
    return int(number)


def _lane_timings(capacity: FleetCapacity) -> dict[int, tuple[int, int, int] | None]:
    """Each lane's ``(free, prep, gap)`` as exact ints, ``None`` where one is not."""
    timings: dict[int, tuple[int, int, int] | None] = {}
    for m in capacity.machines:
        free, prep = _exact_int(m.free_at), _exact_int(m.prep_seconds)
        gap = 0 if capacity.auto_eject else _exact_int(m.gap_seconds)
        timings[m.printer_id] = None if free is None or prep is None or gap is None else (free, prep, gap)
    return timings


def _closed_form_makespan(
    capacity: FleetCapacity,
    recipe: PlateRecipe,
    k: int,
    deadline: float | None,
    lanes_by_key: dict[str, list[FleetMachine]] | None = None,
    timings: dict[int, tuple[int, int, int] | None] | None = None,
) -> float | None:
    """What ``k`` successive ``reserve`` calls finish by, without making them.

    Exact inside a proven domain only — no operator calendar applies, and every
    relevant time is a whole number of seconds that a float adds exactly — and
    ``None`` everywhere else (a fractional ``free_at`` is real: work the
    calendar moved keeps its origin's microseconds; rounding it changed which
    plate won). Mirrors ``preview``: the idle lanes are taken first, one print
    each, exact model before compatible; after that every lane is busy and each
    print goes where it finishes soonest, so the rest finish at the k-th
    smallest value of the lanes' arithmetic progressions.
    """
    seconds = estimate_seconds(recipe)
    key = _model_key(recipe.printer_model)
    if key is None or seconds is None or seconds <= 0 or k <= 0:
        return None
    if lanes_by_key is None:
        lanes = capacity.eligible(key)
    else:
        lanes = lanes_by_key.get(key)
        if lanes is None:
            lanes = lanes_by_key[key] = capacity.eligible(key)
    if not lanes:
        return None
    if capacity.operator_hours is not None and not capacity.auto_eject and any(m.requires_operator for m in lanes):
        return None
    secs = _exact_int(seconds)
    if secs is None or secs <= 0:
        return None
    if timings is None:
        timings = _lane_timings(capacity)
    timing: dict[int, tuple[int, int, int]] = {}
    for m in lanes:
        lane = timings[m.printer_id]
        if lane is None:
            return None
        timing[m.printer_id] = lane

    if len(lanes) == 1:
        # One lane: k prints back to back, the last service not counted.
        free, prep, gap = timing[lanes[0].printer_id]
        end = free + k * (prep + secs) + (k - 1) * gap
        return float(end) if end <= _EXACT_FLOAT_INT else None

    idle = sorted(
        (m for m in lanes if timing[m.printer_id][0] == 0),
        key=lambda m: (0 if m.key == key else 1, timing[m.printer_id][1] + secs, m.printer_id),
    )
    prefix = idle[:k]
    prefix_end = max((timing[m.printer_id][1] + secs for m in prefix), default=0)
    rest = k - len(prefix)
    if rest <= 0:
        return float(prefix_end)

    taken = {m.printer_id for m in prefix}
    progressions: list[tuple[int, int]] = []
    for m in lanes:
        free, prep, gap = timing[m.printer_id]
        start = prep + secs + gap if m.printer_id in taken else free
        progressions.append((start + prep + secs, prep + secs + gap))
    low = min(first for first, _ in progressions)
    high = max(first for first, _ in progressions) + (rest - 1) * max(step for _, step in progressions)
    if high > _EXACT_FLOAT_INT:
        return None
    if rest <= _HEAP_MERGE_LIMIT:
        # Few prints left: pop them off the merged progressions directly — the
        # same k-th value the search finds, at a fraction of its sums.
        heap = list(progressions)
        heapq.heapify(heap)
        last = 0
        for _ in range(rest):
            last, step = heapq.heappop(heap)
            heapq.heappush(heap, (last + step, step))
        return float(max(prefix_end, last))

    def finished_by(t: int) -> int:
        return sum((t - first) // step + 1 for first, step in progressions if t >= first)

    while low < high:
        _check_deadline(deadline)
        mid = (low + high) // 2
        if finished_by(mid) >= rest:
            high = mid
        else:
            low = mid + 1
    return float(max(prefix_end, low))


def _simulated_makespan(
    capacity: FleetCapacity,
    recipe: PlateRecipe,
    k: int,
    budget: list[int] | None,
    deadline: float | None,
    finishes: list[float] | None = None,
) -> float | None:
    """``k`` real reserves on a copy; ``None`` when the budget cannot pay for all of them.

    A candidate is never scored on part of its prints, so the budget is
    charged up front and only when it covers the whole run. ``finishes``, when
    given, receives the run's makespan after each print — what any shorter run
    from the same ledger would finish by.
    """
    if k <= 0:
        return None
    if budget is not None:
        if budget[0] < k:
            return None
        budget[0] -= k
    ledger = capacity.copy()
    finish = None
    for _ in range(k):
        _check_deadline(deadline)
        end = ledger.reserve(recipe)
        if end is None:
            return None
        finish = end if finish is None else max(finish, end)
        if finishes is not None:
            finishes.append(finish)
    return finish


def _run_state(
    capacity: FleetCapacity, recipe: PlateRecipe, lanes_by_key: dict[str, list[FleetMachine]]
) -> tuple | None:
    """Everything a simulated run of ``recipe`` depends on that moves inside one ``cover``.

    ``reserve`` only ever reads the recipe's model and time and its eligible
    lanes, and only ``free_at`` changes; prep, gap, the calendar and auto-eject
    are fixed for the call. The same state therefore gives the same run.
    """
    key = _model_key(recipe.printer_model)
    if key is None:
        return None
    lanes = lanes_by_key.get(key)
    if lanes is None:
        lanes = lanes_by_key[key] = capacity.eligible(key)
    return key, estimate_seconds(recipe), tuple((m.printer_id, m.free_at) for m in lanes)


def _class_choice(
    live: list[tuple],
    remaining: dict[int, int],
    yields: dict[int, dict[int, int]],
    capacity: FleetCapacity,
    budget: list[int],
    stats: dict[str, int],
    deadline: float | None,
    lanes_by_key: dict[str, list[FleetMachine]],
    runs: dict[tuple, list[float]],
) -> tuple:
    """The step's pick: each coverage class sends its best cover, the established key picks among them.

    A class is the plates whose output covers the SAME still-needed parts, so
    every one of them closes the same work — the whole remainder of those parts
    — and their finishes compare directly (vault plan-recipe-choice-lookahead-spec
    r3); its representative is the plate whose prints alone finish that work
    soonest. Recomputed on every step from the current ledger: a stale winner is
    no winner (a cache that kept one lost lane parallelism and picked 4+4+3 over
    4+3+3). Between classes the established next-print key decides, unchanged.

    A representative's key is never below its class's best member key, so
    classes are taken in that order and the walk stops as soon as no remaining
    class could beat the best representative found. With every class it
    reaches fully compared, that is the pick a walk over every class would
    make. ⚠️ Under a budget that cannot compare them all, the ORDER is a
    policy, not an equivalence (owner, 2026-10-22; Codex C3-I1): the budget
    goes to the likely winners first, and a class that gets an exact
    comparison here may fall back under another order — r3's order by the
    parts covered picks a different plate on the same line
    (``test_a_limited_budget_goes_to_classes_by_their_best_key``). The order is
    fixed by the keys (which end in the plate id), so the budget falls the
    same way whatever order the candidates came in. A class that could not be
    fully compared chooses on the established key among ALL its members; a
    member scored on part of its prints never wins.

    ``runs`` keeps every simulated run of this ``cover`` by the state it started
    from (``_run_state``): a member whose lanes the step did not move asks for a
    prefix of a run already made, and gets it without spending the budget. The
    runs a class still needs are paid for together, before any is made — a class
    the budget cannot cover in full spends nothing.
    """
    classes: dict[tuple[int, ...], list[tuple]] = {}
    for entry in live:
        plate = entry[1]
        support = tuple(sorted(p for p, n in yields[plate.id].items() if n > 0 and remaining.get(p, 0) > 0))
        classes.setdefault(support, []).append(entry)
    ordered = sorted(
        ((min(members, key=lambda e: e[0]), support, members) for support, members in classes.items()),
        key=lambda c: c[0][0],
    )
    best: tuple | None = None
    # The ledger does not move inside one step: read each lane's times once.
    timings: dict[int, tuple[int, int, int] | None] | None = None
    for least, support, members in ordered:
        if best is not None and least[0] > best[0]:
            break
        if len(members) == 1:
            representative = least
        else:
            if timings is None:
                timings = _lane_timings(capacity)
            ordered_members = sorted(members, key=lambda e: e[1].id)
            finish_by_plate: dict[int, float] = {}
            # Runs still to simulate, one per distinct state (members sharing it
            # read prefixes of the longest one asked), in plate-id order.
            to_run: dict[tuple, list[tuple[tuple, int]]] = {}
            comparable = True
            for entry in ordered_members:
                _check_deadline(deadline)
                plate, recipe = entry[1], entry[3]
                k = max(math.ceil(remaining[p] / yields[plate.id][p]) for p in support)
                stats["evaluations"] += 1
                finish = _closed_form_makespan(capacity, recipe, k, deadline, lanes_by_key, timings)
                if finish is None:
                    state = _run_state(capacity, recipe, lanes_by_key)
                    if state is None:
                        comparable = False
                        break
                    known = runs.get(state)
                    if known is None or len(known) < k:
                        to_run.setdefault(state, []).append((entry, k))
                        continue
                    finish = known[k - 1]
                    stats["reused"] += 1
                finish_by_plate[plate.id] = finish
            # The whole class is paid for before any of it runs: a class that
            # cannot be compared in full chooses on the established key anyway,
            # and a run spent on one of its members would buy nothing.
            if comparable and sum(max(k for _e, k in asked) for asked in to_run.values()) > budget[0]:
                comparable = False
            if comparable:
                for state, asked in to_run.items():
                    longest = max(k for _e, k in asked)
                    run: list[float] = []
                    if _simulated_makespan(capacity, asked[0][0][3], longest, budget, deadline, run) is None:
                        comparable = False
                        break
                    stats["simulated"] += longest
                    runs[state] = run
                    for entry, k in asked:
                        finish_by_plate[entry[1].id] = run[k - 1]
            if not comparable:
                stats["fallback_classes"] += 1
                representative = least
            else:
                representative = min(ordered_members, key=lambda e: (finish_by_plate[e[1].id], *e[0][1:]))
        if best is None or representative[0] < best[0]:
            best = representative
    return best


def _lookahead_makespan(
    capacity: FleetCapacity,
    recipe: PlateRecipe,
    k: int,
    *,
    budget: list[int] | None = None,
    deadline: float | None = None,
) -> float | None:
    """When ``k`` prints of ``recipe`` would all be finished on this ledger — the
    MAX of their finishes, never the last ``reserve``'s return (an idle exact lane
    with a long prep can finish after a later one). The ledger is not touched.
    ``None``: not computable within the budget. A passed deadline raises
    ``_DeadlineExpired`` inside ``cover`` and returns ``None`` here. The
    closed form or the simulation, whichever applies, behind one door — the
    tests hold both to repeated ``reserve`` through it; ``cover`` calls the two
    directly.
    """
    try:
        closed = _closed_form_makespan(capacity, recipe, k, deadline)
        if closed is not None:
            return closed
        return _simulated_makespan(capacity, recipe, k, budget, deadline)
    except _DeadlineExpired:
        return None


@dataclass
class PlanAlternative:
    """Another plate of the same line that makes exactly the same counted parts.

    The same part is routinely sliced once per printer model — two files, one
    yield — and the greedy picks whichever scored best and hangs every print on
    it, so the other file was invisible in the plan block (user report,
    2026-09-04). This is that other file, riding out on the row it duplicates.

    ⚠️ **"The same" is the COUNTED yield**, exactly as :func:`line_yield` reads
    it: a plate carrying an extra part this line does not count still makes the
    same thing for this line, and a plate making half as many is a different
    plate that belongs in the "+ plate" menu instead.

    The figures are PER PRINT, like a row's — the block re-does its arithmetic
    against them the moment the operator switches files, without asking the
    server again. The count does NOT move with the switch: the yields are
    identical by construction, so the same number of prints covers the same
    work.
    """

    plate_id: int  # ProductPlate.id
    library_file_id: int
    plate_index: int  # 0 = the whole file
    filename: str
    printer_model: str | None = None
    print_time_seconds: int | None = None
    filament_used_grams: float | None = None
    cost: float | None = None
    time_unknown: bool = False
    source_token: str | None = None


@dataclass
class PlanRow:
    """One plate, printed ``count`` times.

    ``print_time_seconds`` / ``filament_used_grams`` / ``cost`` are PER PRINT —
    the count is the multiplier, so the frontend can re-do the arithmetic while
    the operator edits the count without asking the server again.
    """

    plate_id: int  # ProductPlate.id
    library_file_id: int
    plate_index: int  # 0 = the whole file
    filename: str
    count: int = 0
    useful: dict[int, int] = field(default_factory=dict)  # part_id → outstanding covered, summed over the prints
    print_time_seconds: int | None = None
    filament_used_grams: float | None = None
    cost: float | None = None
    # Sliced but timeless: the plate ranked on its useful count alone (time 1),
    # so the operator can see why it out-ranked a plate with a real estimate.
    time_unknown: bool = False
    # The printer model this plate's file was sliced for, in the spelling the
    # auto-queue routes on. ``None`` = the file names none.
    printer_model: str | None = None
    # The line's OTHER candidate plates with the identical counted yield, this
    # one excluded, sorted by ``(printer_model or "", plate_id)``. Empty is the
    # ordinary case; see :class:`PlanAlternative`.
    alternatives: list[PlanAlternative] = field(default_factory=list)
    # An exact draft may pin this run to a machine; recommendation rows do not.
    target_printer_id: int | None = None
    entry_id: str | None = None
    line_token: str | None = None
    source_token: str | None = None
    manual: bool = False
    protected_count: int = 0
    issue: str | None = None


@dataclass
class LinePlan:
    """``outstanding_before`` and ``surplus_after`` list only non-zero entries —
    an empty ``outstanding_before`` is exactly "this line needs nothing"."""

    line_id: int
    product_id: int
    material: str | None
    outstanding_before: dict[int, int] = field(default_factory=dict)
    rows: list[PlanRow] = field(default_factory=list)
    surplus_after: dict[int, int] = field(default_factory=dict)
    unsatisfiable: list[int] = field(default_factory=list)
    #: Why each ``unsatisfiable`` part has no plan (WS-13 E1 OR5a) — exactly one of
    #: ``material_mismatch`` (a sliced plate makes it, in another material),
    #: ``needs_slicing`` (only an unsliced plate makes it) or ``no_plate``.
    unsatisfiable_reasons: dict[int, str] = field(default_factory=dict)
    candidates: list[int] = field(default_factory=list)  # ProductPlate ids eligible for this line
    not_sliced: list[int] = field(default_factory=list)


@dataclass
class PlanTotals:
    prints: int = 0
    print_time_seconds: int | None = None  # None as soon as ONE row has no estimate
    filament_used_grams: float = 0.0
    cost: float | None = None  # None with no farm rate, and also when no row could be costed


@dataclass
class OrderPlan:
    """The rows stay bare ids; the two name maps ride BESIDE them.

    Every id the plan can mention is already named in the ``OrderContext`` the
    plan was built from, so carrying the maps out costs nothing and spares the
    route two SELECTs to look up rows it just had in memory. They are lookup
    tables, not part of a row's identity — nothing downstream may key off a
    name, and a missing entry is a "?" placeholder, never an error.
    """

    lines: list[LinePlan] = field(default_factory=list)
    totals: PlanTotals = field(default_factory=PlanTotals)
    part_names: dict[int, str] = field(default_factory=dict)  # ProductPart.id → name
    product_names: dict[int, str] = field(default_factory=dict)  # Product.id → name
    # ``MAX_ITERATIONS`` stopped the covering of at least one line, so the rows
    # below are a PREFIX of the plan and the totals are a prefix's totals. It
    # rides on the plan because a silent guard reads as a finished answer — see
    # :func:`cover`.
    truncated: bool = False
    auto_eject: bool = False
    mode: str = "recommendation"
    working_revision: int | None = None
    inputs_token: str | None = None
    unplanned_parts: dict[int, int] = field(default_factory=dict)


def line_yield(recipe: PlateRecipe, counted_part_ids: set[int]) -> dict[int, int]:
    """The plate's yield toward THIS line: only parts the line's product counts.

    A plate is shared (two products' lids on one bed) and a product may zero a
    part it does not want counted (``qty_per_unit = 0``). Neither of those is
    this line's output, so neither enters its useful count, its waste or its
    surplus — the pass-1 shared-plate rule, applied to planning.
    """
    return {pid: n for pid, n in recipe.yield_by_part.items() if pid in counted_part_ids and n > 0}


def plate_recipe_index(
    recipes_by_product: dict[int, list[Candidate]],
) -> tuple[dict[tuple[int, int], dict[int, PlateRecipe]], dict[int, dict[int, PlateRecipe]]]:
    """``(by_plate, by_file)`` — the two indexes that answer "whose plate is this".

    Keyed by PLATE, not by product: one caller asks "this product's recipe for
    this plate" and another asks "whose plate is this", and one pair of maps
    answers both. ``by_plate`` is the exact index, ``by_file`` the whole-file
    (``plate_index = 0``) plate that claims every plate of its file; exact wins
    wherever both exist for the same product — see :func:`recipes_for_row`.

    ⚠️ **Shared on purpose.** :func:`queued_yield_by_line` and
    ``queue_rebalance.load_line_catalog`` resolve the same question about the
    same rows, and this exact resolution was already wrong once (a single-plate
    file gets a 0-row from the product side and the slicer's index 1 from the
    row side, so an exact tuple lookup alone misses nearly every single-plate
    print there is). Two copies of it is one copy too many.
    """
    by_plate: dict[tuple[int, int], dict[int, PlateRecipe]] = {}
    by_file: dict[int, dict[int, PlateRecipe]] = {}
    for product_id, rows in recipes_by_product.items():
        for plate, _file, recipe in rows:
            by_plate.setdefault((plate.library_file_id, plate.plate_index), {})[product_id] = recipe
            if plate.plate_index == 0:
                by_file.setdefault(plate.library_file_id, {})[product_id] = recipe
    return by_plate, by_file


def recipes_for_row(
    by_plate: dict[tuple[int, int], dict[int, PlateRecipe]],
    by_file: dict[int, dict[int, PlateRecipe]],
    library_file_id: int,
    plate_index: int,
) -> dict[int, PlateRecipe]:
    """``product_id → recipe`` for one row's ``(file, plate)``, exact plate winning.

    The whole-file plate is laid down first and the exact one overrides it, so a
    product that links both gets its plate-specific recipe and a product that
    links only the file still answers. The one rule; see
    :func:`plate_recipe_index` for why it lives here rather than in each caller.
    """
    return {**by_file.get(library_file_id, {}), **by_plate.get((library_file_id, plate_index), {})}


def counted_parts_by_line(figures_by_project: dict[int, dict[int, LineFigures]]) -> dict[int, set[int]]:
    """``line_id → the part ids that line COUNTS``, from one ``attribute`` pass.

    What :func:`line_yield` filters on, and what every reader of a plate's yield
    toward a line needs first. Shared with ``queue_rebalance`` for the reason
    :func:`plate_recipe_index` is: the figures are the authority on which parts
    a line counts, and a second derivation of that set would eventually
    disagree with the plan on screen.
    """
    return {
        line_id: {pf.part_id for pf in figs.parts if pf.per > 0}
        for figures in figures_by_project.values()
        for line_id, figs in figures.items()
    }


def _pick_key(useful: int, waste: int, secs: int | None, plate_id: int) -> tuple:
    """Spec decision 4, as a sort key (lower is better).

    Highest ``useful / print_time_seconds`` first — a sliced plate with no
    estimate divides by 1, i.e. it competes on its useful count alone. Ties go
    to lower waste, then to the shorter print (an unknown time sorts last, so a
    timeless plate never wins a tie it did not earn on score), then to the lower
    plate id so the same order comes back on every read.

    ``secs`` arrives already normalised by :func:`estimate_seconds`, so ``None`` is the
    only spelling of "no estimate" this key ever sees.
    """
    return (-(useful / (secs or 1)), waste, (secs is None, secs or 0), plate_id)


def rank_key(useful: int, secs: int | None, plate_id: int) -> tuple:
    """The plan's own order of the plates that make ONE part (WS-13 E1 PS1):
    :func:`_pick_key` with no waste — most of the part per hour of print first, then
    the shorter print, then the plate id. A part's sources are listed in the order the
    planner would reach for them, and the first is the one it recommends."""
    return _pick_key(useful, 0, secs, plate_id)


def _figures(recipe: PlateRecipe, price_per_gram: float | None) -> tuple[int | None, float | None, float | None]:
    """``(seconds, grams, cost)`` for ONE print of this plate.

    Shared by :func:`_row_for` and :func:`_alternative_for` because a row and
    the alternative that replaces it on screen are priced by the same rule — the
    block swaps one for the other and the operator must not see the cost of a
    plate change its arithmetic along with its file.
    """
    grams = recipe.filament_used_grams
    secs = estimate_seconds(recipe)
    return secs, grams, (round(grams * price_per_gram, 2) if grams and price_per_gram else None)


def _row_for(plate: ProductPlate, file: LibraryFile, recipe: PlateRecipe, price_per_gram: float | None) -> PlanRow:
    secs, grams, cost = _figures(recipe, price_per_gram)
    return PlanRow(
        plate_id=plate.id,
        library_file_id=plate.library_file_id,
        plate_index=plate.plate_index,
        filename=file.filename,
        print_time_seconds=secs,
        filament_used_grams=grams,
        cost=cost,
        time_unknown=secs is None,
        printer_model=recipe.printer_model,
    )


def _alternative_for(
    plate: ProductPlate, file: LibraryFile, recipe: PlateRecipe, price_per_gram: float | None
) -> PlanAlternative:
    secs, grams, cost = _figures(recipe, price_per_gram)
    return PlanAlternative(
        plate_id=plate.id,
        library_file_id=plate.library_file_id,
        plate_index=plate.plate_index,
        filename=file.filename,
        printer_model=recipe.printer_model,
        print_time_seconds=secs,
        filament_used_grams=grams,
        cost=cost,
        time_unknown=secs is None,
    )


def _attach_alternatives(
    rows: list[PlanRow], candidates: list[Candidate], counted: set[int], price_per_gram: float | None
) -> None:
    """Hang each row's interchangeable plates on it, in place.

    Runs AFTER :func:`cover` and changes nothing it decided: the pick, the
    counts and the surplus are the picked plate's, and swapping a file is the
    block's what-if, not a second plan. It costs one pass over the candidates
    the line already had — no query, no recipe re-read.

    The grouping key is the plate's yield of COUNTED parts as a frozen set of
    ``(part_id, n)`` pairs, i.e. :func:`line_yield` frozen. Two plates share a
    key exactly when one print of either covers the same work for this line;
    everything else about them — the file, the printer model, the time, the
    weight — is free to differ, and that is the point.
    """
    if not rows:
        return
    # Keep exact model rows; already-selected files are not duplicate choices.
    selected = {row.plate_id for row in rows}
    by_key: dict[frozenset[tuple[int, int]], list[Candidate]] = {}
    key_by_plate: dict[int, frozenset[tuple[int, int]]] = {}
    for plate, file, recipe in candidates:
        key = frozenset(line_yield(recipe, counted).items())
        key_by_plate[plate.id] = key
        by_key.setdefault(key, []).append((plate, file, recipe))
    for row in rows:
        key = key_by_plate.get(row.plate_id)
        if key is None:
            continue
        row.alternatives = sorted(
            (
                _alternative_for(plate, file, recipe, price_per_gram)
                for plate, file, recipe in by_key[key]
                if plate.id not in selected
            ),
            key=lambda alt: (alt.printer_model or "", alt.plate_id),
        )


def cover(
    outstanding: dict[int, int],
    candidates: list[Candidate],
    counted: set[int],
    price_per_gram: float | None,
    capacity: FleetCapacity | None = None,
    *,
    compact_equivalent: bool = True,
    deadline: float | None = None,
) -> tuple[list[PlanRow], dict[int, int], bool]:
    """Greedy covering (spec decision 4): plates for one line, until nothing helps.

    Returns the rows in pick order — one row per distinct plate, its ``count``
    and its ``useful`` aggregated over the prints — the surplus each counted
    part ends the plan with, and whether the iteration guard or optional deadline stopped
    it. ``outstanding`` may carry zeros (a part already covered): they never
    attract a pick but they do bound the surplus, because a plate that yields
    them still over-produces.

    ⚠️ The third value is why the guard is not silent. A stopped plan looks
    exactly like a finished one — rows, totals, an empty ``unsatisfiable`` —
    and the operator would print it believing it covers the order. Reading it
    as "the loop ended by exhaustion" also flags the boundary case where the
    LAST iteration happened to finish the job; telling those two apart costs a
    scan that buys nothing, and at ten thousand prints "this is more than one
    plan shows" is not a lie.

    A deadline is cooperative, including scoring and reservation replay. Its
    incomplete result must not be accepted as an exact cover. Calculator callers
    own a capacity copy; an interrupted pass may have reserved partial work there.
    """
    if deadline is not None and time.monotonic() >= deadline:
        return [], {}, True
    remaining = {pid: n for pid, n in outstanding.items() if n > 0}
    yields = {plate.id: line_yield(recipe, counted) for plate, _file, recipe in candidates}
    # Beside ``yields`` and for the same reason: the tie-break reads it on every
    # candidate of every iteration, and it is a pure function of the recipe. Left
    # inside the loop it was recomputed ``MAX_ITERATIONS × len(candidates)``
    # times for an answer that cannot have changed.
    seconds = {plate.id: estimate_seconds(recipe) for plate, _file, recipe in candidates}
    # If the order has at least one candidate that a current lane can actually
    # schedule, absent/parked-model recipes must not win merely because they
    # make more parts per machine-hour.  If there is no usable lane for ANY
    # recipe, retain the old covering result so the sole recipe remains visible
    # and the forecast can explain why it cannot run yet.
    fleet_can_choose = capacity is not None and any(
        capacity.preview(recipe) is not None for _p, _f, recipe in candidates
    )
    rows: dict[int, PlanRow] = {}
    order: list[int] = []
    initial_capacity = capacity.copy() if capacity is not None else None
    reservations: list[int] = []
    # One budget for both passes below: the guard means "more prints than one plan
    # shows", whichever pass would have printed them.
    budget = MAX_ITERATIONS

    # The class lookahead's own allowance of simulated reserves for this whole
    # call (``LOOKAHEAD_SIM_BUDGET``), and what it did — internal diagnostics.
    lookahead_budget = [LOOKAHEAD_SIM_BUDGET]
    lookahead_stats = {"evaluations": 0, "simulated": 0, "reused": 0, "fallback_classes": 0}
    # Eligibility of a model never changes inside one call; the ledger's times do.
    lanes_by_key: dict[str, list[FleetMachine]] = {}
    lookahead_runs: dict[tuple, list[float]] = {}

    def greedy(scheduled: bool) -> bool:
        """Pick prints until nothing helps; True when the budget ran out first."""
        nonlocal budget
        while budget > 0:
            if deadline is not None and time.monotonic() >= deadline:
                return True
            budget -= 1
            best: tuple[tuple, ProductPlate, LibraryFile, PlateRecipe, dict[int, int]] | None = None
            live: list[tuple[tuple, ProductPlate, LibraryFile, PlateRecipe, dict[int, int]]] = []
            for plate, file, recipe in candidates:
                if deadline is not None and time.monotonic() >= deadline:
                    return True
                gain = {pid: min(n, remaining.get(pid, 0)) for pid, n in yields[plate.id].items()}
                useful = sum(gain.values())
                if useful <= 0:
                    continue
                waste = sum(max(0, n - remaining.get(pid, 0)) for pid, n in yields[plate.id].items())
                schedule = capacity.preview(recipe) if scheduled and capacity is not None else None
                if scheduled and schedule is None:
                    continue
                # Calendar finish is the objective when there is real capacity.
                # The established useful/waste/time/id ordering remains the stable
                # tie-break, and remains the whole rule where a farm cannot inform
                # the decision at all.
                key = (
                    (schedule[0], *_pick_key(useful, waste, seconds[plate.id], plate.id))
                    if schedule is not None
                    else _pick_key(useful, waste, seconds[plate.id], plate.id)
                )
                live.append((key, plate, file, recipe, gain))
            if scheduled and capacity is not None and len(live) > 1:
                # Inside a class of plates making the same still-needed parts,
                # the next print's finish is myopic — a small plate always ends
                # first, so a single printer was planned 2×5 instead of 1×10.
                # Each class sends its best COVER of the class's remaining work;
                # between classes the established key decides, unchanged.
                try:
                    best = _class_choice(
                        live,
                        remaining,
                        yields,
                        capacity,
                        lookahead_budget,
                        lookahead_stats,
                        deadline,
                        lanes_by_key,
                        lookahead_runs,
                    )
                except _DeadlineExpired:
                    return True
            else:
                for entry in live:
                    if best is None or entry[0] < best[0]:
                        best = entry
            if deadline is not None and time.monotonic() >= deadline:
                return True
            if best is None:
                return False  # nothing left that covers anything
            _key, plate, file, recipe, gain = best
            if scheduled and capacity is not None:
                # ``schedule`` above came from this unchanged ledger; reserve the
                # same earliest machine immediately so later prints — including a
                # later line of this order — see the load this choice created.
                capacity.reserve(recipe)
                reservations.append(plate.id)
            row = rows.get(plate.id)
            if row is None:
                row = rows[plate.id] = _row_for(plate, file, recipe, price_per_gram)
                order.append(plate.id)
            row.count += 1
            for pid, n in gain.items():
                if not n:
                    continue
                row.useful[pid] = row.useful.get(pid, 0) + n
                remaining[pid] = max(0, remaining[pid] - n)
        return True

    truncated = greedy(fleet_can_choose)
    if lookahead_stats["evaluations"]:
        logger.debug(
            "plan cover lookahead: %d class evaluation(s), %d simulated reserve(s), %d reused run(s), "
            "%d class(es) on the next-print key",
            lookahead_stats["evaluations"],
            lookahead_stats["simulated"],
            lookahead_stats["reused"],
            lookahead_stats["fallback_classes"],
        )
    if fleet_can_choose and not truncated and any(remaining.values()):
        # ⚠️ What the scheduled pass leaves is made ONLY by recipes no lane takes
        # (an absent or parked model, a plate with no estimate) — the per-part form
        # of the rule above. Skipping them hid the part: no row, not unsatisfiable,
        # not truncated, and the forecast read «complete» over it (WS-13 E1 T3). The
        # rows planned here are what the forecast counts as unroutable / unknown.
        truncated = greedy(False)
    planned = [rows[plate_id] for plate_id in order]
    if deadline is not None and time.monotonic() >= deadline:
        return [], {}, True
    if not truncated and not any(remaining.values()):
        # Keep the existing greedy choice; a solver dependency would change the
        # planning objective. Only remove whole runs proven redundant for EVERY
        # counted part, including a rare part on an otherwise surplus plate.
        made = {pid: sum(row.count * yields[row.plate_id].get(pid, 0) for row in planned) for pid in outstanding}
        removed: dict[int, int] = {}
        for row in sorted(planned, key=lambda r: (-(seconds[r.plate_id] or 0), r.plate_id)):
            if deadline is not None and time.monotonic() >= deadline:
                return [], {}, True
            output = yields[row.plate_id]
            removable = min(
                ((made.get(pid, 0) - outstanding.get(pid, 0)) // n for pid, n in output.items() if n > 0),
                default=0,
            )
            removable = max(0, min(row.count, removable))
            if not removable:
                continue
            row.count -= removable
            removed[row.plate_id] = removable
            for pid, n in output.items():
                made[pid] = made.get(pid, 0) - removable * n
        if removed:
            planned = [row for row in planned if row.count]
            left = dict(outstanding)
            for row in planned:
                row.useful = {}
                for pid, n in yields[row.plate_id].items():
                    gain = min(left.get(pid, 0), row.count * n)
                    if gain > 0:
                        row.useful[pid] = gain
                        left[pid] -= gain
            if capacity is not None and initial_capacity is not None:
                # Later order lines must not inherit reservations for removed
                # runs. Replay only retained picks through the SAME lane ledger.
                capacity.machines = initial_capacity.copy().machines
                retained = []
                for plate_id in reversed(reservations):
                    if deadline is not None and time.monotonic() >= deadline:
                        return [], {}, True
                    if removed.get(plate_id, 0):
                        removed[plate_id] -= 1
                    else:
                        retained.append(plate_id)
                recipes = {plate.id: recipe for plate, _file, recipe in candidates}
                for plate_id in reversed(retained):
                    if deadline is not None and time.monotonic() >= deadline:
                        return [], {}, True
                    capacity.reserve(recipes[plate_id])
    if fleet_can_choose and compact_equivalent:
        # Scheduling can deliberately reserve equal-yield variants on different
        # model lanes.  The plan surface already represents that as one row
        # plus alternatives, and the forecast carries the proposed split.  Do
        # not turn a single plate choice into two confusing operator rows just
        # because the private capacity ledger used both variants while scoring.
        merged: dict[frozenset[tuple[int, int]], PlanRow] = {}
        compact: list[PlanRow] = []
        for row in planned:
            if deadline is not None and time.monotonic() >= deadline:
                return [], {}, True
            key = frozenset(yields[row.plate_id].items())
            first = merged.get(key)
            if first is None:
                merged[key] = row
                compact.append(row)
                continue
            first.count += row.count
            for part_id, count in row.useful.items():
                first.useful[part_id] = first.useful.get(part_id, 0) + count
        planned = compact
    surplus = {}
    for pid, want in outstanding.items():
        if deadline is not None and time.monotonic() >= deadline:
            return [], {}, True
        made = sum(row.count * yields[row.plate_id].get(pid, 0) for row in planned)
        if made > want:
            surplus[pid] = made - want
    return planned, surplus, truncated


def _totals(lines: list[LinePlan], price_per_gram: float | None) -> PlanTotals:
    """Time is ``None`` the moment one row has no estimate — a partial sum would
    read as a promise. Grams sum what is known: a row with no figure contributes
    nothing rather than voiding the column.

    ``cost`` is ``None`` unless a rate exists AND at least one row could be
    costed. A farm that has entered a rate but plans only plates with no weight
    would otherwise read 0.00, i.e. "this plan is free" — the same lie
    ``filament_cost.cost_of`` refuses to tell, and the same reason time voids
    itself rather than reporting the half it knows.
    """
    totals = PlanTotals()
    seconds = 0
    unknown = False
    grams = 0.0
    cost = 0.0
    costed = False
    for line in lines:
        for row in line.rows:
            if row.count <= 0:
                continue
            totals.prints += row.count
            if row.print_time_seconds is None:
                unknown = True
            else:
                seconds += row.count * row.print_time_seconds
            if row.filament_used_grams is not None:
                grams += row.count * row.filament_used_grams
            if row.cost is not None:
                cost += row.count * row.cost
                costed = True
    totals.print_time_seconds = None if unknown else seconds
    totals.filament_used_grams = round(grams, 2)
    totals.cost = round(cost, 2) if price_per_gram is not None and costed else None
    return totals


def plan_totals(lines: list[LinePlan], price_per_gram: float | None) -> PlanTotals:
    """The public name of :func:`_totals` — the product estimate (WS-13 E1 ES1) sums
    its rows by the plan's own rules, not a copy of them."""
    return _totals(lines, price_per_gram)


def unplanned_units(figs: LineFigures, queued: Mapping[int, int]) -> int:
    """Whole units of the line nobody has printed, is printing or has queued — the part
    «take from stock» may cover (spec workshop-order-issue, rule 17). The plan's own
    ``outstanding`` per counted part (see :func:`plan_lines`), in units: the most any
    part still lacks, rounded up. ``queued`` is the line's ``part_id → parts queued``."""
    units = 0
    for pf in figs.parts:
        if pf.per <= 0:
            continue
        lacking = max(0, pf.remaining - pf.in_progress - queued.get(pf.part_id, 0))
        units = max(units, -(-lacking // pf.per))
    return units


def plan_lines(
    ctx: OrderContext,
    figures: dict[int, LineFigures],
    recipes_by_product: dict[int, list[Candidate]],
    queued: dict[int, dict[int, int]],
    price_per_gram: float | None,
    capacity: FleetCapacity | None = None,
    *,
    deadline: float | None = None,
) -> OrderPlan:
    """The plan for every line of the order. Pure — see the module docstring.

    ``outstanding = max(0, remaining − in_progress − queued)`` per counted part
    (spec decision 2): what the archives still owe, minus what is on a printer
    right now, minus what somebody has already queued for this line. Unsliced
    plates are reported under ``not_sliced`` and never planned; a part with
    outstanding work that no candidate plate yields at all is ``unsatisfiable``
    — note that a part the iteration guard simply ran out of prints for is NOT,
    because the guard is a defence and not a verdict about the plate. That the
    guard tripped at all is said once, for the whole order, in
    :attr:`OrderPlan.truncated`.

    ⚠️ The ``max(0, …)`` floor is not decoration: more queued than remaining is
    an ordinary state (somebody queued a plate that over-produces), and a
    negative outstanding would flow straight into the surplus arithmetic as a
    phantom.

    The names ride out with the plan (see :class:`OrderPlan`) — they come from
    the context this function was handed, so no caller need re-read them.
    """
    from backend.app.services.order_resources import constrained, permits, recipe_permitted

    resources = getattr(ctx.project, "printer_resources", None)
    if capacity is not None:
        capacity.machines = [m for m in capacity.machines if permits(resources, m.printer_id, m.model)]
        capacity.auto_eject = bool(ctx.project.auto_eject_enabled)
    plans: list[LinePlan] = []
    truncated = False
    for line in ctx.lines:
        if deadline is not None and time.monotonic() >= deadline:
            truncated = True
            break
        figs = figures.get(line.id)
        # A `per == 0` row is surplus of a part the configuration dropped — never planned.
        parts = [pf for pf in figs.parts if pf.per > 0] if figs is not None else []
        counted = {pf.part_id for pf in parts}
        already = queued.get(line.id) or {}
        outstanding = {pf.part_id: max(0, pf.remaining - pf.in_progress - already.get(pf.part_id, 0)) for pf in parts}
        recipes = recipes_by_product.get(line.product_id) or []
        # An unsliced plate has no filaments to match a material against, so it
        # is listed whole — the operator slices it and it becomes a candidate.
        not_sliced = [plate.id for plate, _file, recipe in recipes if not recipe.sliced]
        candidates = [
            row
            for row in recipes
            if row[2].sliced
            and line_accepts_materials(line, row[2].materials)
            and recipe_permitted(
                resources, row[2].printer_model, allow_compatible=bool(capacity and capacity.allow_compatible)
            )
            and (not constrained(resources) or capacity is None or capacity.eligible(_model_key(row[2].printer_model)))
        ]
        rows, surplus, line_truncated = cover(
            outstanding, candidates, counted, price_per_gram, capacity, compact_equivalent=False, deadline=deadline
        )
        if deadline is not None and time.monotonic() >= deadline:
            truncated = True
            break
        _attach_alternatives(rows, candidates, counted, price_per_gram)
        truncated = truncated or line_truncated
        yielded: set[int] = set()
        for _plate, _file, recipe in candidates:
            yielded |= set(line_yield(recipe, counted))
        unsatisfiable = sorted(pid for pid, n in outstanding.items() if n > 0 and pid not in yielded)
        # WS-13 E1 OR5a: an unsliced plate next to a covering one is no reason — only
        # a part that NO candidate yields gets one, from the first rule that fits.
        made_sliced: set[int] = set()
        made_unsliced: set[int] = set()
        for _plate, _file, recipe in recipes:
            (made_sliced if recipe.sliced else made_unsliced).update(line_yield(recipe, counted))
        reasons = {
            pid: "material_mismatch" if pid in made_sliced else "needs_slicing" if pid in made_unsliced else "no_plate"
            for pid in unsatisfiable
        }
        if constrained(resources):
            reasons.update({pid: "printer_resources" for pid in unsatisfiable if pid in made_sliced})
        plans.append(
            LinePlan(
                line_id=line.id,
                product_id=line.product_id,
                material=line.material,
                outstanding_before={pid: n for pid, n in sorted(outstanding.items()) if n > 0},
                rows=rows,
                surplus_after=surplus,
                unsatisfiable=unsatisfiable,
                unsatisfiable_reasons=reasons,
                candidates=[plate.id for plate, _file, _recipe in candidates],
                not_sliced=not_sliced,
            )
        )
    return OrderPlan(
        lines=plans,
        totals=_totals(plans, price_per_gram),
        part_names={part.id: part.name for parts in ctx.parts_by_product.values() for part in parts},
        product_names={pid: product.name for pid, product in ctx.products_by_id.items()},
        truncated=truncated,
        auto_eject=bool(ctx.project.auto_eject_enabled),
    )


async def queued_yield_by_line(
    db: AsyncSession,
    recipes_by_product: dict[int, list[Candidate]],
    lines: list[ProjectLine],
    counted_by_line: dict[int, set[int]],
    *,
    exclude_auto_ids: tuple[int, ...] = (),
    exclude_printer_ids: tuple[int, ...] = (),
) -> dict[int, dict[int, int]]:
    """How much each line already has coming: ``line_id → part_id → parts queued``.

    ⚠️ **Counted parts only, exactly as :func:`line_yield` counts them.** A
    queued plate is shared like any other (two products' lids on one bed, a
    part the product zeroes with ``qty_per_unit = 0``), and the parts of it
    this line does not count are not this line's incoming work. The map went
    out keyed on the RAW ``yield_by_part``, so it carried entries no figure of
    this line ever mentions — harmless in the subtraction below (an uncounted
    id matches no ``PartFigures``) and wrong for everybody else, because the
    map is public. ``counted_by_line`` is the line's ``PartFigures`` part ids,
    from the same ``attribute`` pass the plan is built on; a line missing from
    it counts nothing.

    "Still waiting" is the vault's rule, and it differs per table:
    ``print_queue.status == 'pending'``, and ``auto_queue_items.status ==
    'pending' AND assigned_to_item_id IS NULL`` — an auto row that has already
    been handed to a printer item is counted once, through that item.

    A row's plate is resolved the way attribution resolves an archive's: the
    exact ``(library_file_id, plate_id or 0)`` plate of the line's product
    first, then that product's whole-file (``plate_index = 0``) plate, which
    claims every plate of its file. ⚠️ ``plate_id`` on a queue row is the plate
    INDEX, not a ``ProductPlate.id``.

    ⚠️ A row whose file belongs to no plate of the line's product counts
    NOTHING — the file was unlinked, or the row points somewhere else entirely,
    and there is no yield to invent. Same for a row queued from an archive
    rather than a library file (``library_file_id IS NULL``): every writer the
    plan itself uses stamps the file, so the plan's own work always round-trips.

    ⚠️ **Two branches, and only the second one names the order.** The LINE
    branch filters on ``project_line_id`` alone, because a queue row may carry a
    line without an order id and must still count — naming the order THERE would
    drop real work. The IMPLICIT branch is its opposite and exists for the rows
    the other one cannot see: ``project_id == this order AND project_line_id IS
    NULL`` (spec pass 7, Decision 4b — the legacy, Telegram and hand-written
    rows the writers' own filing does not reach). It resolves the line itself,
    plate → product → material, through the same ``order_filing.line_for_plate``
    the writers call, and a row that resolves to no line of its order counts
    NOWHERE: an ambiguous product is not a licence to guess. The two branches
    are disjoint by construction (``project_line_id IN lines`` vs ``IS NULL``)
    and add into the same buckets, so a row is counted once or not at all.

    ⚠️ Both reads select THREE COLUMNS, never the entity. Loading an
    ``AutoQueueItem`` drags its ``target_location`` in on ``lazy="selectin"``,
    and a ``printer_locations`` SELECT inside the plan path is a lie about what
    this code does: a reader grepping the planner for "printer" must find
    nothing, because routing is not dispatching. The columns are also all the
    yield lookup needs — the implicit branch's material test reads
    ``PlateRecipe.materials``, which the recipes already carry, so naming the
    order costs no query and no file read.
    """
    out: dict[int, dict[int, int]] = {line.id: {} for line in lines}
    if not out:
        return out
    product_by_line = {line.id: line.product_id for line in lines}
    lines_by_project: dict[int, list[ProjectLine]] = {}
    for line in lines:
        lines_by_project.setdefault(line.project_id, []).append(line)
    by_plate, by_file = plate_recipe_index(recipes_by_product)

    def _recipes_for(library_file_id: int, plate_index: int) -> dict[int, PlateRecipe]:
        return recipes_for_row(by_plate, by_file, library_file_id, plate_index)

    def _count(line_id: int, recipe: PlateRecipe) -> None:
        bucket = out[line_id]
        for part_id, n in line_yield(recipe, counted_by_line.get(line_id) or set()).items():
            bucket[part_id] = bucket.get(part_id, 0) + n

    line_ids = list(out)
    waiting = list(
        (
            await db.execute(
                select(PrintQueueItem.project_line_id, PrintQueueItem.library_file_id, PrintQueueItem.plate_id).where(
                    PrintQueueItem.project_line_id.in_(line_ids),
                    *queued_printer_row_conditions(),
                    PrintQueueItem.id.notin_(exclude_printer_ids),
                )
            )
        ).all()
    ) + list(
        (
            await db.execute(
                select(AutoQueueItem.project_line_id, AutoQueueItem.library_file_id, AutoQueueItem.plate_id).where(
                    AutoQueueItem.project_line_id.in_(line_ids),
                    *awaiting_auto_row_conditions(),
                    AutoQueueItem.id.notin_(exclude_auto_ids),
                )
            )
        ).all()
    )
    for line_id, library_file_id, plate_id in waiting:
        if library_file_id is None:
            continue
        product_id = product_by_line.get(line_id)
        if product_id is None:
            continue
        recipe = _recipes_for(library_file_id, plate_id or 0).get(product_id)
        if recipe is None:
            continue
        _count(line_id, recipe)

    project_ids = list(lines_by_project)
    unfiled = list(
        (
            await db.execute(
                select(PrintQueueItem.project_id, PrintQueueItem.library_file_id, PrintQueueItem.plate_id).where(
                    PrintQueueItem.project_id.in_(project_ids),
                    PrintQueueItem.project_line_id.is_(None),
                    *queued_printer_row_conditions(),
                    PrintQueueItem.id.notin_(exclude_printer_ids),
                )
            )
        ).all()
    ) + list(
        (
            await db.execute(
                select(AutoQueueItem.project_id, AutoQueueItem.library_file_id, AutoQueueItem.plate_id).where(
                    AutoQueueItem.project_id.in_(project_ids),
                    AutoQueueItem.project_line_id.is_(None),
                    *awaiting_auto_row_conditions(),
                    AutoQueueItem.id.notin_(exclude_auto_ids),
                )
            )
        ).all()
    )
    for project_id, library_file_id, plate_id in unfiled:
        if library_file_id is None:
            continue
        order_lines = lines_by_project.get(project_id) or []
        hits: dict[int, PlateRecipe] = {}
        # ⚠️ **The material test here can be WIDER than the writers'.** They ask
        # ``plate_materials(meta, the row's own plate index)``; this asks
        # ``PlateRecipe.materials``, which is built from the PRODUCT PLATE's
        # index — and for a whole-file plate (index 0) on a multi-plate 3MF that
        # is the union of every plate's filaments. So a PETG-only line can accept
        # a row here whose actual plate prints PLA, where ``resolve_line_id``
        # would have left it unfiled. The divergence is deliberate and one-way:
        # this branch exists for rows nobody filed, and a whole-file plate is the
        # product saying "this file makes my parts" without saying which plate
        # does — narrowing it per row would drop the legacy rows this branch was
        # added to count. A row the writers DID file never reaches here at all;
        # the two branches are disjoint.
        for product_id, recipe in _recipes_for(library_file_id, plate_id or 0).items():
            line = line_for_plate(order_lines, product_id, recipe.materials)
            if line is not None:
                hits[line.id] = recipe
        # Two products of the same order each claiming a line of their own is as
        # unanswerable as two lines of one product; both end here, counting
        # nowhere.
        # Decision 7 in the engine's own terms: the engine holds recipes, not
        # parts, and "this line counts a part of the plate" is a non-empty
        # counted yield. Same narrowing the writers apply; same fallback.
        counting = {lid: r for lid, r in hits.items() if line_yield(r, counted_by_line.get(lid) or set())}
        if counting:
            hits = counting
        if len(hits) != 1:
            continue
        ((line_id, recipe),) = hits.items()
        _count(line_id, recipe)
    return out


async def _current_fleet_capacity(
    db: AsyncSession,
    *,
    exclude_auto_ids: tuple[int, ...] = (),
    exclude_printer_ids: tuple[int, ...] = (),
    include_paused_queues: bool = False,
) -> FleetCapacity:
    """Read the forecast-owned farm snapshot through its narrow planning adapter.

    The pure covering functions above accept only :class:`FleetCapacity`; they
    neither query a printer nor know what online means.  Keeping the loader at
    this database boundary makes the initial-plan result shared by the order
    page, order-from-files wizard and every other ``plan_for_orders`` caller.
    It does *not* call ``forecast_projects`` (which itself asks us for plans),
    so there is no forecast/planner recursion.
    """
    # Runtime import avoids the module-import cycle: farm_forecast imports the
    # public plan shapes for its advisory simulation, while this wrapper merely
    # asks its snapshot loader for a capacity-only view.
    from backend.app.services.farm_forecast import capacity_from_snapshot, load_snapshot

    return capacity_from_snapshot(
        await load_snapshot(
            db, datetime.utcnow(), exclude_auto_ids=exclude_auto_ids, exclude_printer_ids=exclude_printer_ids
        ),
        include_paused_queues=include_paused_queues,
    )


async def plan_for_orders(
    db: AsyncSession,
    project_ids: list[int],
    *,
    capacity: FleetCapacity | None = None,
    snapshot: FarmSnapshot | None = None,
    working: bool = True,
) -> dict[int, OrderPlan]:
    """The plan of every order in ``project_ids``, in ONE round of queries.

    ``project_id → OrderPlan``, with no entry for an id that names no order —
    the same "there is nothing here" :func:`plan_for_order` says with ``None``.

    ⚠️ **This is the batch, and the one-order function is a wrapper over it.**
    The candidates endpoint asks for the plan of every order that could hold a
    plate, which on a working farm is every open order carrying that product; in
    a loop that was ~14 statements EACH, including a full archive-and-parts read
    per order. ``order_metrics.batch_contexts`` is the loader the orders list
    already uses for exactly this shape, and it returns the contexts
    :func:`load_order_context` would have returned one at a time (pinned by its
    own parity test), so the arithmetic below is untouched.

    Two things are shared across the orders and both are safe to share:

    * ``recipes_for_products`` over the UNION of every order's products — a
      recipe is a property of a plate and a file, not of the order asking, and
      ``plan_lines`` indexes it by ``line.product_id``. A superset costs nothing.
    * ``queued_yield_by_line`` over the UNION of the lines. Its LINE branch
      filters on line ids, and its IMPLICIT branch groups the lines it was given
      by ``project_id`` and asks each order about its own rows — so both branches
      stay keyed per order however many orders are in the call. A product of
      SOMEBODY ELSE's order in the shared recipe map yields no line here either:
      ``line_for_plate`` only ever looks at the lines of the row's own order.

    Computed on every read, never cached and never stored: a second call after
    enqueuing sees the new queue rows and plans that much less.  A snapshot of
    usable farm capacity chooses among viable recipes; it never dispatches and
    every order in this batch receives its own copy of that snapshot.
    """
    if not project_ids:
        return {}
    contexts = await batch_contexts(db, project_ids)
    if not contexts:
        return {}
    figures_by_project = {ctx.project.id: attribute(ctx)[0] for ctx in contexts}
    # ⚠️ ONE load for every product of every order. Per ORDER this was already a
    # single call (it used to be one per product — a SELECT per line of a page
    # that recomputes its plan on every read); per BATCH it is one for all.
    products_by_id = {pid: product for ctx in contexts for pid, product in ctx.products_by_id.items()}
    recipes_by_product = await recipes_for_products(db, products_by_id.values())
    counted_by_line = counted_parts_by_line(figures_by_project)
    all_lines = [line for ctx in contexts for line in ctx.lines]
    queued = await queued_yield_by_line(db, recipes_by_product, all_lines, counted_by_line)
    rate_per_kg = await default_rate_per_kg(db)
    price_per_gram = rate_per_kg / 1000.0 if rate_per_kg > 0 else None
    # A batch is often a comparison of independent orders (the candidates
    # dialog is one caller), so every order starts from the same farm snapshot.
    # Within an order ``plan_lines`` shares its copy across every line.
    from backend.app.services.farm_forecast import capacity_from_snapshot, load_snapshot
    from backend.app.services.order_resources import includes_paused_queues

    fleets = {}
    if capacity is None:
        if snapshot is None:
            snapshot = await load_snapshot(db, datetime.utcnow())
        modes = {includes_paused_queues(ctx.project.printer_resources) for ctx in contexts}
        fleets = {mode: capacity_from_snapshot(snapshot, include_paused_queues=mode) for mode in modes}
    plans = {
        ctx.project.id: plan_lines(
            ctx,
            figures_by_project[ctx.project.id],
            recipes_by_product,
            queued,
            price_per_gram,
            (
                capacity if capacity is not None else fleets[includes_paused_queues(ctx.project.printer_resources)]
            ).copy(),
        )
        for ctx in contexts
    }
    if working:
        from backend.app.services.order_working_plan import effective_plans

        await effective_plans(db, plans, contexts, recipes_by_product, queued, price_per_gram)
    return plans


async def plan_for_order(
    db: AsyncSession, project_id: int, *, capacity: FleetCapacity | None = None
) -> OrderPlan | None:
    """One order's plan — :func:`plan_for_orders` for one. ``None`` = no order."""
    return (await plan_for_orders(db, [project_id], capacity=capacity)).get(project_id)


__all__ = [
    "MAX_ITERATIONS",
    "Candidate",
    "FleetCapacity",
    "FleetMachine",
    "LinePlan",
    "OrderPlan",
    "PlanAlternative",
    "PlanRow",
    "PlanTotals",
    "counted_parts_by_line",
    "cover",
    "line_yield",
    "plan_for_order",
    "plan_for_orders",
    "plan_lines",
    "plate_recipe_index",
    "queued_yield_by_line",
    "recipes_for_row",
    "unplanned_units",
]
