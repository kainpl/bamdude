import { describe, expect, it } from 'vitest';
import { fireEvent, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { render } from '../utils';
import { ArchiveTrashPage } from '../../pages/ArchiveTrashPage';

describe('archive trash receipts', () => {
  it.each([
    { deleted: 2, retained: [], message: 'Deleted 2 archive(s) from trash.' },
    { deleted: 0, retained: [{ id: 2, reasons: ['active_archive', 'swap_owed'] }], message: 'Deleted 0 archive(s); kept 1: The print is still active; A table swap is still owed' },
    { deleted: 1, retained: [{ id: 2, reasons: ['plate_gate'] }], message: 'Deleted 1 archive(s); kept 1: The printer is waiting for Clear plate / Repeat' },
    { deleted: 0, retained: [{ id: 2, reasons: ['plate_gate'] }, { id: 3, reasons: ['plate_gate'] }], message: 'Deleted 0 archive(s); kept 2: The printer is waiting for Clear plate / Repeat' },
    { deleted: 0, retained: [{ id: 2, reasons: ['future_reason'] }], message: 'Deleted 0 archive(s); kept 1: The archive is still held by print work' },
    { deleted: 0, retained: [{ id: 2, reasons: [] }], message: 'Deleted 0 archive(s); kept 1: The archive is still held by print work' },
  ])('reports actual deletions and retained work: $message', async ({ deleted, retained, message }) => {
    server.use(
      http.get('/api/v1/archives/trash', () => HttpResponse.json({ items: [{ id: 2, filename: 'held.3mf', file_size: 0, deleted_at: '2026-01-01T00:00:00', created_by_id: null, project_id: null }], total: 1, retention_days: 30 })),
      http.get('/api/v1/archives/trash/settings', () => HttpResponse.json({ retention_days: 30 })),
      http.delete('/api/v1/archives/trash', () => HttpResponse.json({ deleted, retained })),
    );
    render(<ArchiveTrashPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Empty trash' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/Archives still held by print work are kept/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete permanently' }));
    expect(await screen.findByText(message)).toBeInTheDocument();
  });
});
