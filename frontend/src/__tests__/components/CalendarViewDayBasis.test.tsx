/**
 * A calendar cell and the list it opens count the same prints (upstream audit G04).
 *
 * The list of an opened day is `GET /archives?date_from=&date_to=`, which filters
 * the day a print was CREATED. The cell used to show the `ended` series (keyed on
 * `completed_at`), so a print that ran past midnight was in one day's number and
 * in the other day's list. Both now read the created-at day.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { CalendarView } from '../../components/CalendarView';
import type { AggregateBucket } from '../../api/client';
import { localDateKey } from '../../utils/date';

const metrics = (prints: number, completed: number, failed: number) => ({
  prints,
  completed,
  failed,
  grams: 0,
  cost: 0,
  energy_cost: 0,
  quantity: 0,
  seconds: 0,
});

describe('CalendarView — the day of a print', () => {
  const asked: URL[] = [];
  const today = localDateKey(new Date());

  beforeEach(() => {
    asked.length = 0;
    server.use(
      http.get('/api/v1/archives/', ({ request }) => {
        asked.push(new URL(request.url));
        return HttpResponse.json({ data: [], meta: { current_page: 1, per_page: 200, total: 0, last_page: 1 } });
      }),
    );
  });

  it('counts a day by the prints created on it, and opens that same day', async () => {
    // Three prints were created today; two prints that started yesterday ended today.
    const buckets: AggregateBucket[] = [{ at: today, started: metrics(3, 2, 1), ended: metrics(5, 4, 1) }];
    render(<CalendarView buckets={buckets} />);

    const cell = screen.getByTitle(`${today}: 3 (✓2 ✗1)`);
    expect(cell).toHaveTextContent('3');

    fireEvent.click(cell);
    await waitFor(() => expect(asked).toHaveLength(1));
    expect(asked[0].searchParams.get('date_from')).toBe(today);
    expect(asked[0].searchParams.get('date_to')).toBe(today);
  });
});
