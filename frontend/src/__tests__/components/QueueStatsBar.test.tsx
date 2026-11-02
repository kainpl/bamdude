import { describe, expect, it } from 'vitest';
import { screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { QueueStatsBar } from '../../components/Queue/QueueStatsBar';

describe('QueueStatsBar forecast duration', () => {
  it.each([
    [3540, '59m'],
    [3570, '1h'],
    [3599, '1h'],
    [7199, '2h'],
    [5400, '1h 30m'],
  ])('normalizes rounded minutes for %i seconds as %s', async (seconds, expected) => {
    server.use(http.get('/api/v1/queue/forecast', () => HttpResponse.json({
      free_at: '2026-11-02T22:00:00Z', free_seconds: seconds, unknown_prints: 0,
    })));
    render(<QueueStatsBar queues={[]} unassignedCount={0} />);
    expect(await screen.findByText(expected)).toBeInTheDocument();
  });
});
