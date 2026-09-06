import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../../utils';
import { server } from '../../mocks/server';
import { PlanFromFilesModal } from '../../../components/library/PlanFromFilesModal';

const PREVIEW = {
  files: [
    { id: 5, filename: 'job-p1s.gcode.3mf', sliced_for_model: 'P1S', plates: [{ plate_index: 1, sliced: true, print_time_seconds: 3600 }] },
    { id: 6, filename: 'job-x1c.gcode.3mf', sliced_for_model: 'X1C', plates: [{ plate_index: 0, sliced: true, print_time_seconds: 3000 }] },
  ],
  parts: [
    { name_key: 'flask', name: 'flask', yields: [{ library_file_id: 5, plate_index: 1, count: 2 }, { library_file_id: 6, plate_index: 0, count: 3 }] },
    { name_key: 'cap', name: 'cap', yields: [{ library_file_id: 5, plate_index: 1, count: 1 }] },
  ],
  catalog_product: null,
};
const ORDER = { id: 42, name: 'Flasks', status: 'active', lines: [], figures: { ordered: 0, printed: 0, complete: 0, remaining: 0, total_time_seconds: 0, total_filament_grams: 0, total_cost: 0, defective: 0, margin: null, progress: 0, other_prints_count: 0, all_printed: false, from_stock_units: 0, bankable_surplus: 0, prints_in_progress: 0, prints_queued: 0 }, procurement: [], other_archive_ids: [], customer_id: null, customer_name: null, description: null, color: null, notes: null, attachments: null, tags: null, due_date: null, priority: 'normal', price: null, url: null, cover_image_filename: null, created_at: '2026-09-06T10:00:00', updated_at: '2026-09-06T10:00:00' };

describe('PlanFromFilesModal', () => {
  const created = vi.fn();
  beforeEach(() => {
    created.mockReset();
    server.use(
      http.post('/api/v1/library/files/parts-preview', () => HttpResponse.json(PREVIEW)),
      http.post('/api/v1/projects/from-files', async ({ request }) => {
        created(await request.json());
        return HttpResponse.json(ORDER);
      }),
      http.get('/api/v1/projects/42', () => HttpResponse.json(ORDER)),
      http.get('/api/v1/projects/42/plan', () => HttpResponse.json({ lines: [], totals: { prints: 0, print_time_seconds: 0, filament_used_grams: 0, cost: null }, part_names: {}, product_names: {}, truncated: false })),
      http.delete('/api/v1/projects/42', () => HttpResponse.json({ message: 'Project deleted' })),
    );
  });

  it('shows the unified parts with where each comes from, and refuses to calculate with no target', async () => {
    render(<PlanFromFilesModal fileIds={[5, 6]} onClose={() => {}} />);
    expect(await screen.findByText('flask')).toBeInTheDocument();
    expect(screen.getByText('job-p1s.gcode.3mf · plate 1 · ×2')).toBeInTheDocument();
    expect(screen.getByText('job-x1c.gcode.3mf · whole file · ×3')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Calculate' })).toBeDisabled();
  });

  it('creates a job order from the targets and moves to the plan', async () => {
    render(<PlanFromFilesModal fileIds={[5, 6]} onClose={() => {}} />);
    await userEvent.type(await screen.findByLabelText('flask'), '100');
    await userEvent.click(screen.getByRole('button', { name: 'Calculate' }));
    await waitFor(() => expect(created).toHaveBeenCalledWith({ kind: 'job', name: 'job-p1s', file_ids: [5, 6], targets: { flask: 100 } }));
    expect(await screen.findByTestId('plan-block')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Keep the order' })).toBeInTheDocument();
  });

  it('cancelling on the plan step deletes the order', async () => {
    const onClose = vi.fn();
    const deleted = vi.fn();
    server.use(http.delete('/api/v1/projects/42', () => { deleted(); return HttpResponse.json({ message: 'Project deleted' }); }));
    render(<PlanFromFilesModal fileIds={[5, 6]} onClose={onClose} />);
    await userEvent.type(await screen.findByLabelText('flask'), '10');
    await userEvent.click(screen.getByRole('button', { name: 'Calculate' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(deleted).toHaveBeenCalled());
    expect(onClose).toHaveBeenCalled();
  });

  it('offers the catalogue product and asks for units instead', async () => {
    server.use(http.post('/api/v1/library/files/parts-preview', () => HttpResponse.json({ ...PREVIEW, catalog_product: { id: 9, name: 'Flask kit', parts: [{ id: 1, name: 'flask', qty_per_unit: 2 }] } })));
    render(<PlanFromFilesModal fileIds={[5, 6]} onClose={() => {}} />);
    expect(await screen.findByLabelText('Use product “Flask kit”')).toBeChecked();
    await userEvent.clear(screen.getByLabelText('Units'));
    await userEvent.type(screen.getByLabelText('Units'), '7');
    await userEvent.click(screen.getByRole('button', { name: 'Calculate' }));
    await waitFor(() => expect(created).toHaveBeenCalledWith({ kind: 'catalog', name: 'job-p1s', product_id: 9, file_ids: [5, 6], quantity: 7 }));
  });
});
