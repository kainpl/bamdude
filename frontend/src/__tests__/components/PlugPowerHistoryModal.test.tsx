import { describe, expect, it, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { render } from '../utils';
import { PlugPowerHistoryModal } from '../../components/PlugPowerHistoryModal';
import { bucketLabelKey, chartPoints } from '../../utils/plugPowerChart';
import { api } from '../../api/client';
import type { PlugPowerHistory } from '../../api/client';

// The chart is mocked, as it is for every other recharts component here --
// jsdom gives ResponsiveContainer no size, so nothing would render. `Line`
// keeps its props visible because connectNulls is the whole reason the empty
// buckets are sent at all, and a silent regression there redraws the gaps as
// consumption.
vi.mock('recharts', () => ({
  LineChart: ({ children }: { children: React.ReactNode }) => <div data-testid="line-chart">{children}</div>,
  Line: (props: { connectNulls?: boolean }) => (
    <div data-testid="line" data-connect-nulls={String(props.connectNulls)} />
  ),
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Legend: () => null,
}));

const HISTORY: PlugPowerHistory = {
  points: [
    { recorded_at: '2026-08-03T10:00:00+00:00', power: 3 },
    { recorded_at: '2026-08-03T10:05:00+00:00', power: null },
    { recorded_at: '2026-08-03T10:10:00+00:00', power: 72 },
  ],
  bucket_seconds: 300,
  min_power: 3,
  avg_power: 66,
  max_power: 174,
};

describe('PlugPowerHistoryModal', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getSettings').mockResolvedValue({} as never);
    vi.spyOn(api, 'getPlugPowerHistory').mockResolvedValue(HISTORY);
  });

  it('shows the plug it is about', async () => {
    render(<PlugPowerHistoryModal isOpen onClose={() => {}} plugId={1} plugName="A1Mini-101 Plug" />);

    expect(await screen.findByText('A1Mini-101 Plug')).toBeInTheDocument();
  });

  it('keeps the peak the averaged line hides', async () => {
    render(<PlugPowerHistoryModal isOpen onClose={() => {}} plugId={1} plugName="P" />);

    expect(await screen.findByText('174 W')).toBeInTheDocument();
  });

  it('does not join the line across a gap', async () => {
    render(<PlugPowerHistoryModal isOpen onClose={() => {}} plugId={1} plugName="P" />);

    const line = await screen.findByTestId('line');
    expect(line).toHaveAttribute('data-connect-nulls', 'false');
  });

  it('asks again when the range changes', async () => {
    render(<PlugPowerHistoryModal isOpen onClose={() => {}} plugId={1} plugName="P" />);
    await screen.findByTestId('line-chart');

    await userEvent.click(screen.getByText('7d'));

    await waitFor(() => expect(api.getPlugPowerHistory).toHaveBeenCalledWith(1, 168));
  });

  it('an empty history reads as nothing recorded, not as a failure', async () => {
    vi.spyOn(api, 'getPlugPowerHistory').mockResolvedValue({
      points: [],
      bucket_seconds: 300,
      min_power: null,
      avg_power: null,
      max_power: null,
    });

    render(<PlugPowerHistoryModal isOpen onClose={() => {}} plugId={1} plugName="P" />);

    expect(await screen.findByText(/Nothing recorded yet/i)).toBeInTheDocument();
  });
});

describe('chartPoints', () => {
  it('keeps a null as a null', () => {
    // Turning it into 0 would draw the farm consuming nothing, which is a
    // different claim from not knowing.
    const points = chartPoints(HISTORY.points);

    expect(points.map((p) => p.power)).toEqual([3, null, 72]);
  });

  it('does not pad the series out to the edges of the window', () => {
    // HeaterHistoryModal duplicates its first and last point to reach the axis
    // ends. Here that would invent consumption across hours nobody recorded.
    const points = chartPoints(HISTORY.points);

    expect(points).toHaveLength(HISTORY.points.length);
    expect(points[0].time).toBe(new Date('2026-08-03T10:00:00+00:00').getTime());
  });
});

describe('bucketLabelKey', () => {
  it('names a one-minute bucket in the singular', () => {
    expect(bucketLabelKey(60)).toBe('smartPlugs.powerHistory.bucketMinute');
  });

  it('names anything longer with its minutes', () => {
    expect(bucketLabelKey(1800)).toBe('smartPlugs.powerHistory.bucketMinutes');
  });
});
