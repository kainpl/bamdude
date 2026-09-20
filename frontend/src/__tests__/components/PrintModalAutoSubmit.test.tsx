/**
 * The self-submit a grouped run drives PrintModal with.
 *
 * ⚠️ Every failure this pins is SILENT. The dialog renders nothing while it is
 * deciding, so a gate that answers too early does not look broken — it either
 * queues nothing and shows a dialog for every member (the feature "does
 * nothing"), or queues a plate against whatever happens to be loaded. Neither
 * throws, and neither shows up in a screenshot.
 */

import { describe, it, expect, vi, beforeEach, type Mock } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PrintModal } from '../../components/PrintModal';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const printers = [
  { id: 1, name: 'X1 Carbon', model: 'X1C', ip_address: '192.168.1.100', enabled: true, is_active: true },
];

const statusWithPetg = {
  connected: true,
  state: 'IDLE',
  ams: [
    {
      id: 0,
      tray: [
        { id: 0, tray_type: 'PETG', tray_color: 'FF0000FF', remain: 90 },
        { id: 1, tray_type: '', tray_color: '', remain: -1 },
        { id: 2, tray_type: '', tray_color: '', remain: -1 },
        { id: 3, tray_type: '', tray_color: '', remain: -1 },
      ],
    },
  ],
  vt_tray: [],
};

describe('PrintModal self-submit', () => {
  let queuePosts: number;
  // Typed: a bare `vi.fn()` is `Mock<Procedure | Constructable>` and the
  // modal's props want `() => void`.
  let onClose: Mock<() => void>;
  let onSuccess: Mock<() => void>;

  beforeEach(() => {
    vi.clearAllMocks();
    queuePosts = 0;
    onClose = vi.fn<() => void>();
    onSuccess = vi.fn<() => void>();
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(printers)),
      http.get('/api/v1/archives/:id/plates', () =>
        HttpResponse.json({ is_multi_plate: false, plates: [{ index: 1, name: 'Plate 1' }] })
      ),
      http.get('/api/v1/archives/:id/filament-requirements', () =>
        HttpResponse.json({ filaments: [{ slot_id: 1, type: 'PETG', color: '#FF0000', used_grams: 10 }] })
      ),
      http.post('/api/v1/queue/', () => {
        queuePosts += 1;
        return HttpResponse.json({ id: 1, status: 'pending' });
      })
    );
  });

  const mount = () =>
    render(
      <PrintModal
        mode="add-to-queue"
        archiveId={1}
        archiveName="Bracket"
        initialSelectedPrinterIds={[1]}
        autoSubmitWhenUnambiguous
        onClose={onClose}
        onSuccess={onSuccess}
      />
    );

  it('queues silently when the loaded filament covers the plate', async () => {
    server.use(http.get('/api/v1/printers/:id/status', () => HttpResponse.json(statusWithPetg)));

    mount();

    await waitFor(() => expect(queuePosts).toBe(1));
    await waitFor(() => expect(onSuccess).toHaveBeenCalled());
    expect(onClose).toHaveBeenCalled();
  });

  it('⚠️ waits for the printer status instead of reading its absence as a refusal', async () => {
    // `loadedFilaments` is derived from the status query and is `[]` while it
    // loads, which verdicts EVERY plate with a filament requirement as
    // `filament_type` — a run whose modals mount before the status arrives
    // would show a dialog for every single member and look like a feature that
    // was never wired up. `canSubmit` does not gate on printer status, so this
    // is the condition that has to.
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get('/api/v1/printers/:id/status', async () => {
        await held;
        return HttpResponse.json(statusWithPetg);
      })
    );

    mount();

    // The requirements have landed and a printer is picked, so `canSubmit` is
    // already true here — only the status is missing. Nothing on screen, and
    // above all no refusal.
    await waitFor(() =>
      expect(screen.queryByText(/PETG|Bracket/)).toBeNull()
    );
    expect(screen.queryByRole('heading')).toBeNull();
    expect(queuePosts).toBe(0);

    release();

    await waitFor(() => expect(queuePosts).toBe(1));
  });

  it('renders itself instead of queueing a type that is not loaded', async () => {
    server.use(
      http.get('/api/v1/archives/:id/filament-requirements', () =>
        HttpResponse.json({ filaments: [{ slot_id: 1, type: 'ABS', color: '#FF0000', used_grams: 10 }] })
      ),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(statusWithPetg))
    );

    mount();

    await waitFor(() => expect(screen.getByRole('heading')).toBeInTheDocument());
    expect(queuePosts).toBe(0);
    expect(onSuccess).not.toHaveBeenCalled();
  });

  it('leaves a dialog with none of the new props exactly as it was', async () => {
    server.use(http.get('/api/v1/printers/:id/status', () => HttpResponse.json(statusWithPetg)));

    render(
      <PrintModal
        mode="add-to-queue"
        archiveId={1}
        archiveName="Bracket"
        onClose={onClose}
        onSuccess={onSuccess}
      />
    );

    await waitFor(() => expect(screen.getByRole('heading')).toBeInTheDocument());
    expect(queuePosts).toBe(0);
  });
});

describe('the group toggle', () => {
  const onApplyToRestChange = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(printers)),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(statusWithPetg)),
      http.get('/api/v1/archives/:id/plates', () =>
        HttpResponse.json({ is_multi_plate: false, plates: [{ index: 1, name: 'Plate 1' }] })
      ),
      http.get('/api/v1/archives/:id/filament-requirements', () =>
        HttpResponse.json({ filaments: [{ slot_id: 1, type: 'PETG', color: '#FF0000', used_grams: 10 }] })
      )
    );
  });

  const mountWithBadge = (units: number, applyToRest?: boolean) =>
    render(
      <PrintModal
        mode="add-to-queue"
        archiveId={1}
        archiveName="Bracket"
        initialSelectedPrinterIds={[1]}
        groupBadge={{ current: 1, total: 2, units }}
        applyToRest={applyToRest}
        onApplyToRestChange={onApplyToRestChange}
        onClose={vi.fn()}
      />
    );

  it('offers the choice on a group that has a rest', async () => {
    mountWithBadge(4);

    expect(await screen.findByRole('checkbox', { name: /apply to the rest/i })).toBeChecked();
  });

  it('⚠️ does not offer it on a one-member group — there is no rest to apply to', async () => {
    mountWithBadge(1);

    await screen.findByText('Bracket');
    expect(screen.queryByRole('checkbox', { name: /apply to the rest/i })).not.toBeInTheDocument();
  });

  it('is controlled: it renders what it is given and reports the change', async () => {
    // The run owns this, because the answer belongs to the GROUP and has to
    // reset when the next one opens. The modal must not keep its own copy.
    mountWithBadge(4, false);

    const box = await screen.findByRole('checkbox', { name: /apply to the rest/i });
    expect(box).not.toBeChecked();

    await userEvent.click(box);

    expect(onApplyToRestChange).toHaveBeenCalledWith(true);
    expect(box).not.toBeChecked();
  });

  it('counts the OTHERS in its hint, not the group', async () => {
    mountWithBadge(4);

    const label = await screen.findByTitle(/other 3 files/i);
    expect(label).toBeInTheDocument();
  });
});
