/**
 * `projectLineId` rides beside `projectId` all the way into the payload.
 *
 * Without it nothing in the UI stamps a print with the order LINE it counts
 * against, and the order page can only ever attribute prints by guessing from
 * the parts. The reprint-from-archive branch stays the exception it already
 * is: it targets an archive that carries its own binding, so it sends neither
 * id.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { cleanup, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { PrintModal } from '../../components/PrintModal';
import { api } from '../../api/client';

const mockPrinters = [
  { id: 1, name: 'X1 Carbon', model: 'X1C', ip_address: '192.168.1.100', enabled: true, is_active: true },
];

describe('PrintModal — order line', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(mockPrinters)),
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json({ connected: true, state: 'IDLE', ams: [], vt_tray: [] }),
      ),
      http.get('/api/v1/archives/:id/plates', () =>
        HttpResponse.json({ is_multi_plate: false, plates: [] }),
      ),
      http.get('/api/v1/archives/:id/filament-requirements', () => HttpResponse.json({ filaments: [] })),
      http.get('/api/v1/library/files/:id', () =>
        HttpResponse.json({ id: 5, filename: 'flask.gcode.3mf', file_tags: ['gcode', '3mf', 'sliced'] }),
      ),
      http.get('/api/v1/library/files/:id/plates', () =>
        HttpResponse.json({ is_multi_plate: false, plates: [] }),
      ),
      http.get('/api/v1/library/files/:id/filament-requirements', () =>
        HttpResponse.json({ filaments: [] }),
      ),
    );
  });

  it('sends project_line_id beside project_id on a direct print of a library file', async () => {
    const print = vi
      .spyOn(api, 'printLibraryFile')
      .mockResolvedValue({ status: 'dispatched' } as never);
    const user = userEvent.setup();

    render(
      <PrintModal
        mode="reprint"
        libraryFileId={5}
        archiveName="flask.gcode.3mf"
        projectId={3}
        projectLineId={10}
        onClose={() => {}}
      />,
    );

    await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());
    await user.click(screen.getByText('X1 Carbon'));
    await user.click(screen.getByRole('button', { name: /^print$/i }));

    await waitFor(() =>
      expect(print).toHaveBeenCalledWith(
        5,
        1,
        expect.objectContaining({ project_id: 3, project_line_id: 10 }),
      ),
    );
  });

  it('sends a null line when the caller named an order but no line', async () => {
    const print = vi
      .spyOn(api, 'printLibraryFile')
      .mockResolvedValue({ status: 'dispatched' } as never);
    const user = userEvent.setup();

    render(
      <PrintModal
        mode="reprint"
        libraryFileId={5}
        archiveName="flask.gcode.3mf"
        projectId={3}
        onClose={() => {}}
      />,
    );

    await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());
    await user.click(screen.getByText('X1 Carbon'));
    await user.click(screen.getByRole('button', { name: /^print$/i }));

    await waitFor(() =>
      expect(print).toHaveBeenCalledWith(
        5,
        1,
        expect.objectContaining({ project_id: 3, project_line_id: null }),
      ),
    );
  });

  it('omits both ids on a reprint from an archive', async () => {
    const reprint = vi
      .spyOn(api, 'reprintArchive')
      .mockResolvedValue({ status: 'dispatched' } as never);
    const user = userEvent.setup();

    render(<PrintModal mode="reprint" archiveId={1} archiveName="Benchy" onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());
    await user.click(screen.getByText('X1 Carbon'));
    await user.click(screen.getByRole('button', { name: /^print$/i }));

    await waitFor(() => expect(reprint).toHaveBeenCalled());
    const options = reprint.mock.calls[0][2] as Record<string, unknown>;
    expect(options).not.toHaveProperty('project_id');
    expect(options).not.toHaveProperty('project_line_id');
  });

  it('carries the line into the auto-queue payload', async () => {
    // The third payload site. Auto mode never picks a printer — the router
    // does that at dispatch — so the line has to travel on the item itself or
    // the print lands attributed to the order and to nothing in it.
    const add = vi.spyOn(api, 'addToAutoQueue').mockResolvedValue({ id: 1 } as never);
    const user = userEvent.setup();

    render(
      <PrintModal
        mode="add-to-queue"
        libraryFileId={5}
        archiveName="flask.gcode.3mf"
        projectId={3}
        projectLineId={10}
        initialDispatchMode="auto"
        lockDispatchMode
        onClose={() => {}}
      />,
    );

    await user.click(await screen.findByRole('button', { name: /^add to queue$/i }));

    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(expect.objectContaining({ project_id: 3, project_line_id: 10 })),
    );
  });

  it('drops the caller’s line when several plates are ticked, and keeps the order', async () => {
    // ⚠️ The plan block opens this dialog pinned to ITS line, which is an answer
    // about the plate the block offered — not about the file. Tick a second plate
    // of that file and every row went out stamped with that line, including the
    // plates that make another product's parts. The order still travels; the
    // backend writers resolve the line per row, which is the only place that
    // knows which plate each row is for.
    server.use(
      http.get('/api/v1/library/files/:id/plates', () =>
        HttpResponse.json({
          is_multi_plate: true,
          plates: [
            { index: 1, name: 'Plate 1', has_thumbnail: false, thumbnail_url: null, objects: ['A'], filaments: [], print_time_seconds: 60, filament_used_grams: 1 },
            { index: 2, name: 'Plate 2', has_thumbnail: false, thumbnail_url: null, objects: ['B'], filaments: [], print_time_seconds: 60, filament_used_grams: 1 },
          ],
        }),
      ),
    );
    const add = vi.spyOn(api, 'addToAutoQueue').mockResolvedValue({ id: 1 } as never);
    const user = userEvent.setup();

    render(
      <PrintModal
        mode="add-to-queue"
        libraryFileId={5}
        archiveName="flask.gcode.3mf"
        projectId={3}
        projectLineId={10}
        initialDispatchMode="auto"
        lockDispatchMode
        onClose={() => {}}
      />,
    );

    // One plate ticked (the auto-select) still carries the caller's own line.
    await user.click(await screen.findByRole('button', { name: /^add to queue$/i }));
    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(expect.objectContaining({ project_id: 3, project_line_id: 10 })),
    );

    add.mockClear();
    cleanup();

    render(
      <PrintModal
        mode="add-to-queue"
        libraryFileId={5}
        archiveName="flask.gcode.3mf"
        projectId={3}
        projectLineId={10}
        initialDispatchMode="auto"
        lockDispatchMode
        onClose={() => {}}
      />,
    );

    await user.click(await screen.findByText('Select All 2 Plates'));
    await user.click(screen.getByRole('button', { name: 'Queue 2 Plates' }));

    await waitFor(() => expect(add).toHaveBeenCalled());
    expect(add.mock.calls[0][0]).toMatchObject({ project_id: 3, project_line_id: null });
  });

  it('carries the line into the add-to-queue payload', async () => {
    const add = vi.spyOn(api, 'addToQueue').mockResolvedValue({ id: 1 } as never);
    const user = userEvent.setup();

    render(
      <PrintModal
        mode="add-to-queue"
        libraryFileId={5}
        archiveName="flask.gcode.3mf"
        projectId={3}
        projectLineId={10}
        onClose={() => {}}
      />,
    );

    await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());
    await user.click(screen.getByText('X1 Carbon'));
    await user.click(screen.getByRole('button', { name: /^add to queue$/i }));

    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(expect.objectContaining({ project_id: 3, project_line_id: 10 })),
    );
  });
});
