import { describe, expect, it } from 'vitest';
import type { LinePlan, PlanPartCount } from '../../../api/client';
import { chosenPlate, parseCount, projectPlan, rowDistribution } from '../../../components/projects/planMath';

/**
 * Two plates against 120 bodies and 10 caps — the parent spec's worked case,
 * shrunk. `useful` is what the SERVER covered with the row it planned; the
 * per-print yields come separately (the product's plate recipes), because
 * `useful` is aggregated over the row's prints and clipped at each pick, so it
 * cannot be divided back into a per-print figure.
 */
const line: LinePlan = {
  line_id: 10,
  product_id: 7,
  product_name: 'Flask',
  material: 'PETG',
  outstanding_before: [
    { part_id: 1, name: 'Body', count: 120 },
    { part_id: 2, name: 'Cap', count: 10 },
  ],
  rows: [
    {
      plate_id: 100,
      library_file_id: 5,
      plate_index: 1,
      filename: 'big.3mf',
      count: 1,
      useful: [{ part_id: 1, name: 'Body', count: 100 }],
      print_time_seconds: 36000,
      filament_used_grams: 500,
      cost: 10,
      time_unknown: false,
      printer_model: 'X1C',
      // The same 100 bodies, sliced for the other machine: half the speed and a
      // little more filament, and the yield identical by construction.
      alternatives: [
        {
          plate_id: 400,
          library_file_id: 8,
          plate_index: 1,
          filename: 'big-p1s.3mf',
          printer_model: 'P1S',
          print_time_seconds: 72000,
          filament_used_grams: 550,
          cost: 11,
          time_unknown: false,
        },
      ],
    },
    {
      plate_id: 200,
      library_file_id: 6,
      plate_index: 0,
      filename: 'small.3mf',
      count: 2,
      useful: [
        { part_id: 1, name: 'Body', count: 20 },
        { part_id: 2, name: 'Cap', count: 10 },
      ],
      print_time_seconds: 3600,
      filament_used_grams: 50,
      cost: 1,
      time_unknown: false,
      printer_model: null,
      alternatives: [],
    },
  ],
  surplus_after: [],
  unsatisfiable: [],
  candidates: [100, 200],
  not_sliced: [],
};

const yields: Record<number, PlanPartCount[]> = {
  100: [{ part_id: 1, name: 'Body', count: 100 }],
  200: [
    { part_id: 1, name: 'Body', count: 10 },
    { part_id: 2, name: 'Cap', count: 5 },
  ],
};

describe('projectPlan', () => {
  it('reproduces the server plan when nothing is edited', () => {
    const p = projectPlan(line, {}, yields);

    expect(p.prints).toBe(3);
    expect(p.seconds).toBe(36000 + 2 * 3600);
    expect(p.grams).toBeCloseTo(600);
    expect(p.cost).toBeCloseTo(12);
    // 1 × 100 + 2 × 10 = 120 bodies and 2 × 5 = 10 caps, both exactly the need.
    expect(p.surplusAfter).toEqual([]);
  });

  it('moves surplus and totals when a count is raised', () => {
    const p = projectPlan(line, { 200: 3 }, yields);

    expect(p.prints).toBe(4);
    expect(p.seconds).toBe(36000 + 3 * 3600);
    expect(p.grams).toBeCloseTo(650);
    expect(p.cost).toBeCloseTo(13);
    expect(p.surplusAfter).toEqual([
      { part_id: 1, name: 'Body', count: 10 },
      { part_id: 2, name: 'Cap', count: 5 },
    ]);
  });

  it('lets a row at zero contribute nothing at all', () => {
    const p = projectPlan(line, { 100: 0 }, yields);

    expect(p.prints).toBe(2);
    expect(p.seconds).toBe(2 * 3600);
    expect(p.grams).toBeCloseTo(100);
    expect(p.cost).toBeCloseTo(2);
    // 20 bodies against a need of 120 — short, not surplus.
    expect(p.surplusAfter).toEqual([]);
  });

  it('floors a negative count at zero rather than subtracting work', () => {
    expect(projectPlan(line, { 100: -5, 200: 0 }, yields).prints).toBe(0);
  });

  it('voids the time as soon as one counted row has no estimate', () => {
    const timeless: LinePlan = {
      ...line,
      rows: [{ ...line.rows[0], print_time_seconds: null, time_unknown: true }, line.rows[1]],
    };

    expect(projectPlan(timeless, {}, yields).seconds).toBeNull();
    // …and only while that row is actually counted.
    expect(projectPlan(timeless, { 100: 0 }, yields).seconds).toBe(2 * 3600);
  });

  it('keeps the grams column when one row has no weight, and voids cost only when no row is costed', () => {
    const partial: LinePlan = {
      ...line,
      rows: [{ ...line.rows[0], filament_used_grams: null, cost: null }, line.rows[1]],
    };

    expect(projectPlan(partial, {}, yields).grams).toBeCloseTo(100);
    expect(projectPlan(partial, {}, yields).cost).toBeCloseTo(2);
    expect(projectPlan(partial, { 200: 0 }, yields).cost).toBeNull();
  });

  it('counts a part the line no longer needs as pure surplus', () => {
    // `outstanding_before` carries non-zero entries only, so a counted part
    // that is already covered is absent from it — everything the plan makes of
    // it is surplus, exactly as the engine's own loop has it.
    const covered: LinePlan = { ...line, outstanding_before: [{ part_id: 1, name: 'Body', count: 120 }] };

    expect(projectPlan(covered, {}, yields).surplusAfter).toEqual([
      { part_id: 2, name: 'Cap', count: 10 },
    ]);
  });

  it('refuses to guess a surplus while a plate yield is unknown', () => {
    const p = projectPlan(line, {}, { 100: yields[100] });

    expect(p.surplusAfter).toBeNull();
    // The totals never depended on the yields, so they still answer.
    expect(p.prints).toBe(3);
    expect(p.seconds).toBe(36000 + 2 * 3600);
  });

  it('answers for a plan with no rows at all', () => {
    const empty: LinePlan = { ...line, rows: [] };
    const p = projectPlan(empty, {}, {});

    expect(p).toEqual({ surplusAfter: [], prints: 0, seconds: 0, grams: 0, cost: null });
  });
});

describe('the file a row is set to print', () => {
  it('is the row’s own plate until an alternative is chosen', () => {
    expect(chosenPlate(line.rows[0]).filename).toBe('big.3mf');
    expect(chosenPlate(line.rows[0], line.rows[0].plate_id).printer_model).toBe('X1C');
  });

  it('is the alternative once it is', () => {
    const chosen = chosenPlate(line.rows[0], 400);

    expect(chosen).toEqual({
      plate_id: 400,
      library_file_id: 8,
      plate_index: 1,
      filename: 'big-p1s.3mf',
      printer_model: 'P1S',
      print_time_seconds: 72000,
      filament_used_grams: 550,
      cost: 11,
      time_unknown: false,
    });
  });

  it('falls back to the row when the chosen plate is no longer offered', () => {
    // ⚠️ A choice made against a plan that has since moved: the reseed drops
    // it, but a render between the two must not go blank or send a plate this
    // row cannot print.
    expect(chosenPlate(line.rows[0], 999).filename).toBe('big.3mf');
  });

  it('projects the chosen alternative’s per-print figures, not the row’s', () => {
    const p = projectPlan(line, {}, yields, { 100: 400 });

    // The yield is identical by construction, so the counts and the surplus do
    // not move — only what the machine spends making them.
    expect(p.prints).toBe(3);
    expect(p.surplusAfter).toEqual([]);
    expect(p.seconds).toBe(72000 + 2 * 3600);
    expect(p.grams).toBeCloseTo(650);
    expect(p.cost).toBeCloseTo(13);
  });
});

describe('rowDistribution', () => {
  it('is the whole count on the chosen file when nothing is split', () => {
    expect(rowDistribution(line.rows[0], 4, undefined, undefined)).toEqual([{ plate_id: 100, count: 4 }]);
    expect(rowDistribution(line.rows[0], 4, 400, undefined)).toEqual([{ plate_id: 400, count: 4 }]);
  });

  it('drops a file the operator emptied, and keeps the row’s order', () => {
    expect(rowDistribution(line.rows[0], 5, undefined, { 100: 3, 400: 2 })).toEqual([
      { plate_id: 100, count: 3 },
      { plate_id: 400, count: 2 },
    ]);
    expect(rowDistribution(line.rows[0], 5, undefined, { 100: 0, 400: 5 })).toEqual([{ plate_id: 400, count: 5 }]);
  });

  it('sends nothing for a row at zero', () => {
    expect(rowDistribution(line.rows[0], 0, undefined, undefined)).toEqual([]);
  });
});

describe('parseCount', () => {
  it('takes a whole number and floors it at zero', () => {
    expect(parseCount('7', 1)).toBe(7);
    expect(parseCount('0', 1)).toBe(0);
    expect(parseCount('2.9', 1)).toBe(2);
    // A minus sign is a count nobody can print — the floor is the same one the
    // engine applies to a line's outstanding work.
    expect(parseCount('-4', 3)).toBe(0);
  });

  it('refuses a number that is not finite, keeping what was there', () => {
    // ⚠️ `Number('1e999')` is `Infinity`, and `Number(x) || 0` waves it
    // through: the row then rendered `Infinityh NaMm` and the arithmetic below
    // it went with it. A count the browser cannot represent is not an edit.
    expect(parseCount('1e999', 4)).toBe(4);
    expect(parseCount('-1e999', 4)).toBe(4);
    expect(parseCount('abc', 4)).toBe(4);
  });

  it('reads an emptied box as zero, not as a refusal', () => {
    // Clearing the box to type a new number must not snap the old one back —
    // the operator would be unable to type at all.
    expect(parseCount('', 4)).toBe(0);
    expect(parseCount('   ', 4)).toBe(0);
  });
});
