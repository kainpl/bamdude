/**
 * Tests for per-part defective steppers in EditArchiveModal (Task 9 of the
 * parts-ledger plan). When an archive carries `parts` (Task 8), the flat
 * "Defective Parts" number input is replaced by one clamped stepper per
 * part; an archive with no parts keeps the flat field.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { EditArchiveModal } from '../../components/EditArchiveModal';
import type { Archive } from '../../api/client';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const baseArchive: Archive = {
  id: 7,
  printer_id: 1,
  project_id: null,
  project_name: null,
  filename: 'multi-part.gcode.3mf',
  file_path: '/archives/multi-part.gcode.3mf',
  file_size: 1024,
  content_hash: null,
  source_content_hash: null,
  applied_patches: null,
  effective_hash: null,
  thumbnail_path: null,
  timelapse_path: null,
  source_3mf_path: null,
  f3d_path: null,
  duplicates: null,
  duplicate_count: 0,
  duplicate_sequence: 0,
  original_archive_id: null,
  object_count: null,
  skip_objects_supported: false,
  print_name: 'Multi-part plate',
  print_time_seconds: null,
  actual_time_seconds: null,
  time_accuracy: null,
  filament_used_grams: null,
  filament_type: null,
  filament_color: null,
  layer_height: null,
  total_layers: null,
  nozzle_diameter: null,
  bed_temperature: null,
  bed_type: null,
  nozzle_temperature: null,
  sliced_for_model: null,
  plate_index: null,
  status: 'completed',
  started_at: null,
  completed_at: null,
  extra_data: null,
  makerworld_url: null,
  designer: null,
  external_url: null,
  is_favorite: false,
  tags: '',
  notes: '',
  cost: null,
  photos: null,
  failure_reason: null,
  quantity: 6,
  defective_count: 0,
  energy_kwh: null,
  energy_cost: null,
  swap_compatible: false,
  queue_id: null,
  batch_id: null,
  error_message: null,
  created_at: '2024-01-01T00:00:00Z',
  created_by_id: null,
  created_by_username: null,
  parts: [
    { id: 1, name: 'lid', name_key: 'lid', quantity: 2, defective: 0 },
    { id: 2, name: 'base.stl', name_key: 'base.stl', quantity: 4, defective: 1 },
  ],
};

describe('per-part defective entry', () => {
  const mockOnClose = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    server.use(
      http.get('/api/v1/projects/', () => HttpResponse.json([])),
      http.get('/api/v1/archives/tags', () => HttpResponse.json([])),
      http.patch('/api/v1/archives/:id', async ({ request }) => {
        const body = await request.json();
        // Global constraint: PATCH always answers with parts: [] — the modal
        // must never read fresh per-part state back out of this response.
        return HttpResponse.json({ ...baseArchive, ...(body as object), parts: [] });
      })
    );
  });

  it('shows one stepper per part instead of the flat field', () => {
    render(<EditArchiveModal archive={baseArchive} onClose={mockOnClose} />);

    expect(screen.getByText('lid')).toBeInTheDocument();
    expect(screen.getByText('base.stl')).toBeInTheDocument();
    // the flat defective input is gone
    expect(screen.queryByTestId('defective-count-input')).toBeNull();
  });

  it('caps each stepper at that part quantity and derives the sum', () => {
    render(<EditArchiveModal archive={baseArchive} onClose={mockOnClose} />);

    const lid = screen.getByTestId('part-defective-1') as HTMLInputElement;
    fireEvent.change(lid, { target: { value: '99' } });
    expect(lid.value).toBe('2');
    // summary line shows 2 (lid, clamped) + 1 (base.stl, untouched) = 3
    expect(screen.getByTestId('parts-defective-total').textContent).toContain('3');
  });

  it('an archive without parts keeps the flat field', () => {
    render(<EditArchiveModal archive={{ ...baseArchive, parts: [] }} onClose={mockOnClose} />);

    expect(screen.getByTestId('defective-count-input')).toBeInTheDocument();
    expect(screen.queryByTestId('part-defective-1')).toBeNull();
  });

  it('sends parts_defective plus the derived defective_count on save', async () => {
    const user = userEvent.setup();
    let sentBody: Record<string, unknown> | null = null;
    server.use(
      http.patch('/api/v1/archives/:id', async ({ request }) => {
        sentBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...baseArchive, ...sentBody, parts: [] });
      })
    );

    render(<EditArchiveModal archive={baseArchive} onClose={mockOnClose} />);

    const lid = screen.getByTestId('part-defective-1') as HTMLInputElement;
    fireEvent.change(lid, { target: { value: '1' } });

    await user.click(screen.getByRole('button', { name: /save/i }));

    await waitFor(() => {
      expect(sentBody).not.toBeNull();
    });
    expect(sentBody!.parts_defective).toEqual([
      { id: 1, defective: 1 },
      { id: 2, defective: 1 },
    ]);
    // 1 (lid) + 1 (base.stl) = 2
    expect(sentBody!.defective_count).toBe(2);
  });
});
