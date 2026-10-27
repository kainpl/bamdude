/**
 * The order's summary (WS-13 E3 §E): eight tiles in the mockup's order, the
 * coverage bar, and one line of the other figures. Every number is the
 * server's, shown as sent (design decision 8) — and not one of the figures the
 * old twelve tiles carried may be lost on the way (E07).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, within } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { ProjectFigures } from '../../../api/client';
import { strayZeroTextNodes } from '../../domHelpers';
import { OrderFigures } from '../../../components/projects/OrderFigures';
import type { ForecastView } from '../../../components/projects/orderForecastView';
import { makeFigures, makeForecast } from '../../fixtures/orderDetail';

const loading: ForecastView = { kind: 'loading' };
const withData = (over = {}): ForecastView => ({ kind: 'data', forecast: makeForecast(over), refreshFailed: false });

function mount(figures: ProjectFigures, forecast: ForecastView = loading) {
  return render(<OrderFigures figures={figures} forecast={forecast} />);
}

const tile = (name: string) => screen.getByTestId(`order-tile-${name}`);

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, 'getSettings').mockResolvedValue({ currency: 'USD', date_format: 'system', time_format: '24h' } as never);
});

describe('OrderFigures · eight tiles', () => {
  it('draws exactly eight tiles in the mockup’s order', () => {
    mount(makeFigures());
    const tiles = screen.getAllByTestId(/^order-tile-/);
    expect(tiles.map((el) => el.dataset.testid)).toEqual([
      'order-tile-ordered',
      'order-tile-printed',
      'order-tile-from-stock',
      'order-tile-remaining',
      'order-tile-printing',
      'order-tile-ready',
      'order-tile-cost',
      'order-tile-defects',
    ]);
    expect(within(tile('ordered')).getByText('Ordered')).toBeInTheDocument();
    expect(within(tile('printing')).getByText('Printing / queued')).toBeInTheDocument();
  });

  it('keeps «From stock» even at zero', () => {
    mount(makeFigures({ from_stock_units: 0 }));
    expect(tile('from-stock')).toHaveTextContent('From stock0');
  });

  it('shows printing and queued in one tile', () => {
    mount(makeFigures({ prints_in_progress: 2, prints_queued: 3 }));
    expect(tile('printing')).toHaveTextContent('2 / 3');
  });

  it('greens a zero left to cover and warns of defects', () => {
    mount(makeFigures({ remaining: 0, defective: 2 }));
    expect(tile('remaining').querySelector('[data-tone]')).toHaveAttribute('data-tone', 'ok');
    expect(tile('defects').querySelector('[data-tone]')).toHaveAttribute('data-tone', 'warn');
  });

  it('shows the cost with purchases', () => {
    mount(makeFigures({ cost_with_procurement: 14.5 }));
    expect(tile('cost')).toHaveTextContent('$14.50');
  });

  it('writes a known lower bound when a purchase price is unknown, and a dash when nothing is known (Z8, R07)', () => {
    const { unmount } = mount(
      makeFigures({ total_cost: 8, procurement_known_cost: 4, procurement_partial: true, procurement_cost: null, cost_with_procurement: null }),
    );
    expect(tile('cost')).toHaveTextContent('≥ $12.00');
    expect(tile('cost').querySelector('[title]')).toHaveAttribute('title', expect.stringMatching(/purchased parts/i));
    unmount();

    mount(makeFigures({ total_cost: 0, procurement_known_cost: 0, procurement_partial: true, procurement_cost: null, cost_with_procurement: null }));
    expect(tile('cost')).toHaveTextContent('—');
  });
});

describe('OrderFigures · «Ready ≈»', () => {
  const ready = () => tile('ready');

  it('has no date for a closed order', () => {
    mount(makeFigures(), { kind: 'closed' });
    expect(ready()).toHaveTextContent('Ready ≈—');
  });

  it('says «all covered» by COVERAGE, whatever the stage — a manual QC with work left shows a date (E01)', () => {
    const { unmount } = mount(makeFigures({ remaining: 0 }), withData());
    expect(within(ready()).getByText('all covered')).toBeInTheDocument();
    unmount();

    mount(makeFigures({ remaining: 3 }), withData({ now_eta: '2026-09-26T09:37:00Z' }));
    // The day in the system locale's short form (the jsdom locale decides the month's name).
    expect(ready()).toHaveTextContent(/26/);
    expect(within(ready()).queryByText('all covered')).toBeNull();
  });

  it('says the plan was edited instead of the previous plan’s date (R03)', () => {
    mount(makeFigures(), { kind: 'draft' });
    expect(ready()).toHaveTextContent('plan edited');
    expect(ready()).not.toHaveTextContent(/26/);
  });

  it('waits and fails without inventing a date', () => {
    const { unmount } = mount(makeFigures(), loading);
    expect(ready()).toHaveTextContent('…');
    unmount();
    mount(makeFigures(), { kind: 'error' });
    expect(ready()).toHaveTextContent('Ready ≈—');
  });

  it('reds a date past the deadline and marks an incomplete estimate by more than colour', () => {
    mount(
      makeFigures(),
      withData({ now_eta: '2026-09-26T09:37:00Z', late: true, incomplete_reasons: [{ code: 'no_plate', count: 1 }] }),
    );
    expect(ready().querySelector('[data-tone]')).toHaveAttribute('data-tone', 'late');
    expect(ready()).toHaveTextContent('later than the deadline');
    expect(within(ready()).getByRole('img', { name: 'Incomplete estimate' })).toBeInTheDocument();
  });

  it('writes a dash when the forecast has no date', () => {
    mount(makeFigures(), withData({ now_eta: null }));
    expect(ready()).toHaveTextContent('Ready ≈—');
  });
});

describe('OrderFigures · coverage', () => {
  it('hides the bar for an order with nothing ordered, leaving no stray zero', () => {
    mount(makeFigures({ ordered: 0, printed: 0, covered_units: 0, remaining: 0, progress: 0, from_stock_units: 0 }));
    expect(screen.queryByTestId('order-progress')).not.toBeInTheDocument();
    expect(strayZeroTextNodes(screen.getByTestId('order-progress-area'))).toHaveLength(0);
  });

  it('names where the coverage comes from and shows the server’s percentage', () => {
    const { unmount } = mount(makeFigures({ ordered: 10, printed: 4, covered_units: 7, from_stock_units: 3, progress: 0.7 }));
    const bar = screen.getByTestId('order-progress');
    expect(bar).toHaveTextContent('Covered: 4 printed · 3 from stock');
    expect(bar).toHaveTextContent('70%');
    unmount();

    mount(makeFigures({ ordered: 10, printed: 4, covered_units: 4, from_stock_units: 0, progress: 0.4 }));
    expect(screen.getByTestId('order-progress')).toHaveTextContent('Covered: 4 printed');
    expect(screen.getByTestId('order-progress')).not.toHaveTextContent('from stock');
  });

  it('fills by the server coverage, not by raw prints, on a mixed order', () => {
    mount(makeFigures({ ordered: 4, printed: 4, covered_units: 1, remaining: 3, progress: 0.25, from_stock_units: 0 }));
    expect(screen.getByTestId('order-progress')).toHaveTextContent('25%');
    expect(screen.getByTestId('order-progress-fill')).toHaveStyle({ width: '25%' });
  });
});

describe('OrderFigures · the line of other figures (E05, E07)', () => {
  const stats = () => screen.getByTestId('order-stats');

  it('carries time and filament always, and the rest when there is any', () => {
    const { unmount } = mount(
      makeFigures({ total_time_seconds: 5400, total_filament_grams: 420, complete: 3, other_prints_count: 2, held_units: 5, issued_units: 4, ordered: 10 }),
    );
    expect(stats()).toHaveTextContent('Print time 1:30');
    expect(stats()).toHaveTextContent('filament 420g');
    expect(stats()).toHaveTextContent('can assemble 3');
    expect(stats()).toHaveTextContent('other prints: 2');
    expect(stats()).toHaveTextContent('held for the order 5 · issued 4 of 10');
    unmount();

    mount(makeFigures({ complete: 0, other_prints_count: 0, held_units: 0, issued_units: 0 }));
    expect(stats()).not.toHaveTextContent(/can assemble|other prints|issued/);
  });

  it('breaks the cost into its parts', () => {
    mount(makeFigures({ total_cost: 12.5, total_filament_cost: 10, total_energy_cost: 2.5, procurement_cost: 0 }));
    expect(screen.getByTestId('order-cost-breakdown')).toHaveTextContent('cost: filament $10.00 + electricity $2.50');
  });

  it('marks estimated electricity and counts the prints with no reliable figure, never as zero', () => {
    mount(
      makeFigures({ total_cost: 2.5, total_filament_cost: 0, total_energy_cost: 2.5, procurement_cost: 0, energy_estimated: 1, energy_unavailable: 2 }),
    );
    // The mark never wraps away from its amount onto a line of its own.
    expect(screen.getByTestId('order-cost-breakdown').textContent).toContain('electricity ≈ $2.50');
    expect(screen.getByTestId('order-energy-unknown')).toHaveTextContent('2 prints without a reliable energy figure');
  });

  it('keeps the purchases of an order that printed nothing (R07)', () => {
    const { unmount } = mount(
      makeFigures({ total_cost: 0, total_filament_cost: 0, total_energy_cost: 0, procurement_cost: 5, procurement_known_cost: 5 }),
    );
    expect(screen.getByTestId('order-cost-breakdown')).toHaveTextContent('cost: purchases $5.00');
    unmount();

    mount(
      makeFigures({ total_cost: 0, total_filament_cost: 0, total_energy_cost: 0, procurement_cost: null, procurement_known_cost: 0, procurement_partial: true, cost_with_procurement: null }),
    );
    expect(screen.getByTestId('order-cost-breakdown')).toHaveTextContent('cost: purchases: price unknown');
  });

  it('writes a known lower bound for partly priced purchases', () => {
    mount(makeFigures({ total_cost: 8, total_filament_cost: 6, total_energy_cost: 2, procurement_cost: null, procurement_known_cost: 4, procurement_partial: true, cost_with_procurement: null }));
    expect(screen.getByTestId('order-cost-breakdown')).toHaveTextContent('cost: filament $6.00 + electricity $2.00 + purchases ≥ $4.00');
  });

  it('says nothing about cost when there is none at all', () => {
    mount(makeFigures({ total_cost: 0, total_filament_cost: 0, total_energy_cost: 0, procurement_cost: 0, procurement_known_cost: 0 }));
    expect(screen.queryByTestId('order-cost-breakdown')).toBeNull();
  });
});
