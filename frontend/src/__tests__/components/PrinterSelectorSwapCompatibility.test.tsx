/**
 * Only a source KNOWN to carry its own plate-swap sequence narrows the printer list to swap-mode
 * printers (swap-source-lifecycle spec r4 §9). An unknown declaration filters nothing — it is no
 * declaration — and a False one never did.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { PrintModal } from '../../components/PrintModal';

const printers = [
  { id: 1, name: 'P1S-swap', model: 'P1S', ip_address: '192.168.1.101', enabled: true, is_active: true, swap_mode_enabled: true },
  { id: 2, name: 'P1S-plain', model: 'P1S', ip_address: '192.168.1.102', enabled: true, is_active: true, swap_mode_enabled: false },
];

beforeEach(() => {
  localStorage.clear();
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json(printers)),
    http.get('/api/v1/printers/:id/status', () =>
      HttpResponse.json({ connected: true, state: 'IDLE', ams: [], vt_tray: [] }),
    ),
    http.get('/api/v1/archives/:id/plates', () => HttpResponse.json({ is_multi_plate: false, plates: [] })),
    http.get('/api/v1/archives/:id/filament-requirements', () => HttpResponse.json({ filaments: [] })),
    http.get('/api/v1/print-option-preferences/:model', () => HttpResponse.json(null)),
  );
});

function mount(fact: { swap_compatible: boolean; swap_compatibility_known: boolean }) {
  server.use(
    http.get('/api/v1/archives/:id', () =>
      // A reprint reads the archive's SOURCE pair (spec r3 §7): here the run and the source agree.
      HttpResponse.json({
        id: 1,
        filename: 'a.3mf',
        sliced_for_model: null,
        ...fact,
        source_swap_compatible: fact.swap_compatible,
        source_swap_compatibility_known: fact.swap_compatibility_known,
      }),
    ),
  );
  render(<PrintModal mode="add-to-queue" archiveId={1} archiveName="Bracket" onClose={vi.fn()} onSuccess={vi.fn()} />);
}

describe('which printers a source may go to', () => {
  it('True narrows the list to swap-mode printers', async () => {
    mount({ swap_compatible: true, swap_compatibility_known: true });

    expect(await screen.findByText('P1S-swap')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('P1S-plain')).toBeNull());
  });

  it.each([
    ['unknown', { swap_compatible: false, swap_compatibility_known: false }],
    ['False', { swap_compatible: false, swap_compatibility_known: true }],
  ])('%s filters nothing', async (_label, fact) => {
    mount(fact);

    expect(await screen.findByText('P1S-swap')).toBeInTheDocument();
    expect(await screen.findByText('P1S-plain')).toBeInTheDocument();
  });
});
