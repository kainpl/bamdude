import { describe, it, expect } from 'vitest';
import { screen } from '@testing-library/react';
import { render } from '../../utils';
import { strayZeroTextNodes } from '../../domHelpers';
import { OrderFigures } from '../../../components/projects/OrderFigures';

const FIGURES = {
  ordered: 0,
  printed: 0,
  complete: 0,
  remaining: 0,
  total_time_seconds: 0,
  total_filament_grams: 0,
  total_cost: 0,
  defective: 0,
  from_stock_units: 0,
  bankable_surplus: 0,
  margin: null,
  progress: 0,
  other_prints_count: 0,
  all_printed: false,
  prints_in_progress: 0,
  prints_queued: 0,
};

describe('OrderFigures', () => {
  it('renders nothing numeric-stray for an empty order and hides the bar', () => {
    render(<OrderFigures figures={FIGURES} />);
    expect(screen.queryByTestId('order-progress')).not.toBeInTheDocument();
    // Scoped to the progress area (pre-flight ruling 1): the figure tiles legitimately print "0" as labelled numbers;
    // the rule the detector guards is that a HIDDEN bar leaves no bare 0 behind where it would have been.
    expect(strayZeroTextNodes(screen.getByTestId('order-progress-area'))).toHaveLength(0);
    expect(screen.queryByText(/other prints/i)).not.toBeInTheDocument();
  });

  it('shows the bar and the other-prints line once there is something to count', () => {
    render(
      <OrderFigures
        figures={{
          ordered: 10,
          printed: 4,
          complete: 3,
          remaining: 6,
          total_time_seconds: 5400,
          total_filament_grams: 123.45,
          total_cost: 12.5,
          defective: 1,
          from_stock_units: 0,
          bankable_surplus: 0,
          margin: -3,
          progress: 0.4,
          other_prints_count: 2,
          all_printed: false,
          prints_in_progress: 0,
          prints_queued: 0,
        }}
      />,
    );
    expect(screen.getByTestId('order-progress')).toBeInTheDocument();
    expect(screen.getByText('4 / 10')).toBeInTheDocument();
    expect(screen.getByText('1:30')).toBeInTheDocument();
    expect(screen.getByText('123.5')).toBeInTheDocument();
    expect(screen.getByText(/other prints/i)).toBeInTheDocument();
  });

  it('breaks actual production cost into filament and electricity', () => {
    render(
      <OrderFigures
        figures={{
          ...FIGURES,
          total_cost: 12.5,
          total_filament_cost: 10,
          total_energy_cost: 2.5,
        }}
      />,
    );

    expect(screen.getByTestId('order-cost-breakdown')).toHaveTextContent('Filament: $10.00 + Electricity: $2.50');
  });
  it('shows what is printing and queued right now', () => {
    render(
      <OrderFigures
        figures={{
          ordered: 10,
          printed: 4,
          complete: 3,
          remaining: 6,
          total_time_seconds: 5400,
          total_filament_grams: 123.45,
          total_cost: 12.5,
          defective: 1,
          from_stock_units: 0,
          bankable_surplus: 0,
          margin: -3,
          progress: 0.4,
          other_prints_count: 2,
          all_printed: false,
          prints_in_progress: 2,
          prints_queued: 3,
        }}
      />,
    );
    expect(screen.getByText('Printing').nextSibling).toHaveTextContent('2');
    expect(screen.getByText('Queued').nextSibling).toHaveTextContent('3');
  });
  it('shows what came off the shelf beside the printed count, and only when there is any', () => {
    // Pass 8, Decision 5. `ordered` and `printed` stay literal — the customer
    // asked for ten and the farm printed four — and this is the third number.
    // A permanent "0" tile on every order in the farm would be a column of
    // noise, so the tile exists only when the figure does.
    const figures = {
      ordered: 10,
      printed: 4,
      complete: 3,
      remaining: 3,
      total_time_seconds: 0,
      total_filament_grams: 0,
      total_cost: 0,
      defective: 0,
      from_stock_units: 3,
      bankable_surplus: 0,
      margin: null,
      progress: 0.7,
      other_prints_count: 0,
      all_printed: false,
      prints_in_progress: 0,
      prints_queued: 0,
    };
    const { rerender } = render(<OrderFigures figures={figures} />);
    expect(screen.getByText('From stock')).toBeInTheDocument();

    rerender(<OrderFigures figures={{ ...figures, from_stock_units: 0 }} />);
    expect(screen.queryByText('From stock')).not.toBeInTheDocument();
  });

  it('keeps machine hours and the concrete reason, but not a ready date, for an incomplete forecast', () => {
    render(
      <OrderFigures
        figures={FIGURES}
        forecast={{ project_id: 1, now_eta: '2026-09-07T10:00:00Z', now_seconds: 7200, after_eta: null, after_seconds: null, machine_seconds: 5400, unknown_prints: 2, unroutable_prints: 0, eta_complete: false, ahead_count: 0, assumptions: ['drying'], lines: [] }}
      />,
    );
    expect(screen.getByText('Ready ≈')).toBeInTheDocument();
    expect(screen.getByText('Incomplete estimate')).toBeInTheDocument();
    expect(screen.queryByText(/Sep 7/)).not.toBeInTheDocument();
    expect(screen.getByText('1:30')).toBeInTheDocument();
    expect(screen.getByText('2 prints without an estimate')).toBeInTheDocument();
    expect(screen.getByLabelText(/drying between prints/)).toBeInTheDocument();
  });

  it('marks an edited draft stale even when the server forecast itself was complete', () => {
    render(
      <OrderFigures
        figures={FIGURES}
        forecast={{ project_id: 1, now_eta: '2026-09-07T10:00:00Z', now_seconds: 7200, after_eta: null, after_seconds: null, machine_seconds: 5400, unknown_prints: 0, unroutable_prints: 0, eta_complete: true, ahead_count: 0, assumptions: [], lines: [] }}
        forecastStale
      />,
    );
    expect(screen.getByText('Refresh after plan changes')).toBeInTheDocument();
    expect(screen.getByText('See edited plan below')).toBeInTheDocument();
    expect(screen.queryByText(/Sep 7/)).not.toBeInTheDocument();
  });
});
