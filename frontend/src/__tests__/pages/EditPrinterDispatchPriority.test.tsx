import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { EditPrinterModal } from '../../pages/PrintersPage';
import type { Printer } from '../../api/client';

// C01 (upstream #2915): a printer's place in the AutoQueue printer order, sent as upstream's form sends it.
const printer = {
  id: 78, name: 'Synthetic B', model: 'P1S', nozzle_count: 1, is_active: true, tags: [], tag_ids: [],
  serial_number: 'SYNTHETIC78', ip_address: '192.0.2.78', dispatch_priority: null,
} as unknown as Printer;

function capturePatches() {
  const patches: Record<string, unknown>[] = [];
  server.use(
    http.get('/api/v1/macros/swap-profiles', () => HttpResponse.json([])),
    http.post('/api/v1/printers/diagnostic', () => HttpResponse.json({ checks: [] })),
    http.get('/api/v1/inventory/spools/auto-stock-groups', () => HttpResponse.json([])),
    http.patch('/api/v1/printers/78', async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      patches.push(body);
      return HttpResponse.json({ ...printer, ...body });
    }),
  );
  return patches;
}

async function saveWith(typed: string | null, start: Printer = printer) {
  const patches = capturePatches();
  render(<EditPrinterModal printer={start} onClose={() => {}} />);
  const field = await screen.findByLabelText('Priority for AutoQueue jobs');
  await userEvent.clear(field);
  if (typed !== null) await userEvent.type(field, typed);
  await userEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
  await waitFor(() => expect(patches).toHaveLength(1));
  return patches[0].dispatch_priority;
}

describe('Edit Printer — priority for AutoQueue jobs', () => {
  it('shows a saved priority', async () => {
    capturePatches();
    render(<EditPrinterModal printer={{ ...printer, dispatch_priority: 4 } as Printer} onClose={() => {}} />);
    expect(await screen.findByLabelText('Priority for AutoQueue jobs')).toHaveValue(4);
  });

  it('empty sends null', async () => {
    expect(await saveWith(null, { ...printer, dispatch_priority: 4 } as Printer)).toBeNull();
  });

  it('0 stays 0', async () => {
    expect(await saveWith('0')).toBe(0);
  });

  it('2.5 is sent as 3', async () => {
    expect(await saveWith('2.5')).toBe(3);
  });

  it('a negative number is sent as 0', async () => {
    expect(await saveWith('-1')).toBe(0);
  });
});
