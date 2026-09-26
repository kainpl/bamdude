/**
 * The auto-queue list shows the order the distributor will place jobs in
 * (upstream f5cdc866, #3043).
 *
 * With Shortest-Job-First on, `AutoQueueScheduler._fetch_pending` orders by
 * `target_model, been_jumped DESC, print_time_seconds ASC NULLS LAST, position`
 * — the panel kept sorting by position alone, so the one list that says what
 * goes next answered for an order nobody was using.
 */
import { describe, it, expect } from 'vitest';
import { compareAutoQueueOrder } from '../../utils/autoQueueOrder';

type Row = { id: number; position: number; target_model: string | null; been_jumped: boolean; print_time_seconds: number | null };

const row = (id: number, over: Partial<Row> = {}): Row => ({
  id, position: id, target_model: 'P1S', been_jumped: false, print_time_seconds: 3600, ...over,
});

const order = (rows: Row[], sjf: boolean) => [...rows].sort((a, b) => compareAutoQueueOrder(a, b, sjf)).map((r) => r.id);

describe('compareAutoQueueOrder', () => {
  it('is position alone with SJF off', () => {
    expect(order([row(2, { print_time_seconds: 60 }), row(1, { print_time_seconds: 9000 })], false)).toEqual([1, 2]);
  });

  it('puts the shortest print first with SJF on', () => {
    expect(order([row(1, { print_time_seconds: 9000 }), row(2, { print_time_seconds: 60 })], true)).toEqual([2, 1]);
  });

  it('puts an unknown duration last, not first', () => {
    expect(order([row(1, { print_time_seconds: null }), row(2, { print_time_seconds: 9000 })], true)).toEqual([2, 1]);
  });

  it('puts a jumped job ahead of every shorter one', () => {
    expect(
      order([row(1, { print_time_seconds: 60 }), row(2, { print_time_seconds: 9000, been_jumped: true })], true),
    ).toEqual([2, 1]);
  });

  it('breaks a tie by position', () => {
    expect(order([row(3, { position: 5 }), row(4, { position: 2 })], true)).toEqual([4, 3]);
  });

  it('keeps each model together, as the distributor reads them', () => {
    const rows = [
      row(1, { target_model: 'X1C', print_time_seconds: 60 }),
      row(2, { target_model: 'P1S', print_time_seconds: 9000 }),
      row(3, { target_model: 'X1C', print_time_seconds: 120 }),
    ];
    expect(order(rows, true)).toEqual([2, 1, 3]);
  });
});
