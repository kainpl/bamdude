/**
 * Two-level print plan (Task 6, 2026-08-29 project-templates-and-plate-plan):
 * a file's plate rows (Task 5, plate_index > 0) group under one parent row;
 * a legacy/single-plate file (one row, plate_index===0) keeps rendering as
 * the flat row it always was. Each plate child gets its own copies stepper
 * — PATCHing by the plan ITEM id, not the library file id — and pins
 * PrintModal to its own plate via preselectedPlateId when printed/queued
 * individually.
 *
 * This also exercises the fix for the live-wrong bug Task 5 left behind:
 * the copies mutation used to PATCH `/print-plan/items/{itemId}` with the
 * library_file_id in the itemId slot. Silently wrong (both are numbers, no
 * type error) for any file with more than one plate row.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import { Routes, Route } from 'react-router-dom';
import { http, HttpResponse } from 'msw';

import { render } from '../utils';
import { server } from '../mocks/server';
import { ProjectDetailPage } from '../../pages/ProjectDetailPage';
import type { PrintModalProps } from '../../components/PrintModal/types';

// The real PrintModal pulls in printer/filament-mapping queries this test
// has no reason to mock — swap it for a prop-recording stub so "did the
// plate button open the modal pinned to the right plate" is a prop
// assertion, not a walk through the real dialog's internals.
const printModalMock = vi.hoisted(() => ({
  calls: [] as PrintModalProps[],
}));

vi.mock('../../components/PrintModal', () => ({
  PrintModal: (props: PrintModalProps) => {
    printModalMock.calls.push(props);
    return <div data-testid="print-modal-mock" />;
  },
}));

const stats = {
  total_archives: 0,
  total_items: 0,
  completed_prints: 1,
  defective_parts: 0,
  failed_prints: 0,
  queued_prints: 0,
  in_progress_prints: 0,
  total_print_time_hours: 0,
  total_filament_grams: 0,
  progress_percent: null as number | null,
  parts_progress_percent: null as number | null,
  estimated_cost: 0,
  total_energy_kwh: 0,
  total_energy_cost: 0,
  remaining_prints: null,
  remaining_parts: null,
  bom_total_items: 0,
  bom_completed_items: 0,
  bom_cost: 0,
};

const project = {
  id: 1,
  name: 'Plan Plates Project',
  description: '',
  color: '#00ae42',
  status: 'active',
  target_count: 0,
  target_parts_count: 0,
  budget: null,
  notes: null,
  tags: null,
  due_date: null,
  priority: 'normal',
  url: null,
  parent_id: null,
  is_template: false,
  attachments: [],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
  stats,
};

// Shape matches PrintPlanItem (api/client.ts) — file 10 has two plate rows
// (Task 5 split), file 20 is a legacy single row at plate_index 0.
const planItems = [
  {
    id: 1,
    library_file_id: 10,
    plate_index: 1,
    copies: 2,
    order_index: 0,
    filename: 'multi.gcode.3mf',
    print_name: 'Multi Plate File',
    file_type: '3mf',
    thumbnail_path: null,
    swap_compatible: true,
    filament_grams: 20,
    print_time_seconds: 1800,
    object_count: 1,
    cost_per_copy: 0.5,
    total_filament_grams: 40,
    total_print_time_seconds: 3600,
    total_objects: 2,
    total_cost: 1,
    printed_count: 0,
    remaining_count: 2,
  },
  {
    id: 2,
    library_file_id: 10,
    plate_index: 2,
    copies: 3,
    order_index: 0,
    filename: 'multi.gcode.3mf',
    print_name: 'Multi Plate File',
    file_type: '3mf',
    thumbnail_path: null,
    swap_compatible: true,
    filament_grams: 15,
    print_time_seconds: 1200,
    object_count: 1,
    cost_per_copy: 0.4,
    total_filament_grams: 45,
    total_print_time_seconds: 3600,
    total_objects: 3,
    total_cost: 1.2,
    printed_count: 1,
    remaining_count: 2,
  },
  {
    id: 3,
    library_file_id: 20,
    plate_index: 0,
    copies: 5,
    order_index: 1,
    filename: 'single.gcode.3mf',
    print_name: 'Single Plate File',
    file_type: '3mf',
    thumbnail_path: null,
    swap_compatible: true,
    filament_grams: 10,
    print_time_seconds: 600,
    object_count: 1,
    cost_per_copy: 0.2,
    total_filament_grams: 50,
    total_print_time_seconds: 3000,
    total_objects: 5,
    total_cost: 1,
    printed_count: 0,
    remaining_count: 5,
  },
];

const printPlanResponse = {
  items: planItems,
  totals_filament_grams: 135,
  totals_print_time_seconds: 10200,
  totals_objects: 10,
  totals_cost: 3.2,
  default_filament_cost_per_kg: 20,
};

// Minimal LibraryFileListItem fixtures. ProjectDetailPage resolves plan
// rows against these (filesById) for isPrintable() + the print/queue
// buttons — needs `gcode` in file_tags. Fetched via the legacy flat
// getLibraryFiles route, not FileManagerPage's paged one.
const libraryFiles = [
  {
    id: 10,
    filename: 'multi.gcode.3mf',
    print_name: 'Multi Plate File',
    file_tags: ['gcode', '3mf'],
    folder_id: null,
    project_ids: [1],
    is_external: false,
    file_type: 'gcode',
    file_size: 2048,
    thumbnail_path: null,
    duplicate_count: 0,
    created_by_id: null,
    created_by_username: null,
    created_at: '2026-08-01T00:00:00Z',
    fs_modified_at: null,
    print_time_seconds: 3600,
    filament_used_grams: 85,
    object_count: 5,
    skip_objects_supported: false,
    sliced_for_model: null,
    swap_compatible: true,
    notes_count: 0,
  },
  {
    id: 20,
    filename: 'single.gcode.3mf',
    print_name: 'Single Plate File',
    file_tags: ['gcode', '3mf'],
    folder_id: null,
    project_ids: [1],
    is_external: false,
    file_type: 'gcode',
    file_size: 1024,
    thumbnail_path: null,
    duplicate_count: 0,
    created_by_id: null,
    created_by_username: null,
    created_at: '2026-08-01T00:00:00Z',
    fs_modified_at: null,
    print_time_seconds: 3000,
    filament_used_grams: 50,
    object_count: 5,
    skip_objects_supported: false,
    sliced_for_model: null,
    swap_compatible: true,
    notes_count: 0,
  },
];

let patchCalls: Array<{ itemId: number; copies: number }> = [];

async function renderPlanPage(items: typeof planItems = planItems) {
  server.use(
    http.get('/api/v1/projects/1', () => HttpResponse.json(project)),
    http.get('/api/v1/projects/1/archives', () => HttpResponse.json([])),
    http.get('/api/v1/projects/1/bom', () => HttpResponse.json([])),
    http.get('/api/v1/projects/1/timeline', () => HttpResponse.json([])),
    http.get('/api/v1/projects/1/folders', () => HttpResponse.json([])),
    http.get('/api/v1/projects/1/print-plan', () => HttpResponse.json({ ...printPlanResponse, items })),
    http.get('/api/v1/projects/', () => HttpResponse.json([])),
    http.get('/api/v1/library/files', () => HttpResponse.json(libraryFiles)),
    http.patch('/api/v1/projects/1/print-plan/items/:itemId', async ({ params, request }) => {
      const body = (await request.json()) as { copies: number };
      const itemId = Number(params.itemId);
      patchCalls.push({ itemId, copies: body.copies });
      const original = planItems.find((i) => i.id === itemId)!;
      return HttpResponse.json({ ...original, copies: body.copies });
    }),
  );

  window.history.pushState({}, '', '/projects/1');
  render(
    <Routes>
      <Route path="/projects/:id" element={<ProjectDetailPage />} />
    </Routes>,
  );

  await waitFor(() => expect(screen.getAllByText('Plan Plates Project').length).toBeGreaterThan(0));
}

describe('ProjectDetailPage plan — two-level plate rows', () => {
  beforeEach(() => {
    window.history.pushState({}, '', '/');
    printModalMock.calls = [];
    patchCalls = [];
  });

  it('groups plate rows under one file parent; a single-plate file stays flat', async () => {
    await renderPlanPage();

    expect(await screen.findByTestId('plan-parent-10')).toBeInTheDocument();
    expect(screen.getByTestId('plan-plate-1')).toBeInTheDocument();
    expect(screen.getByTestId('plan-plate-2')).toBeInTheDocument();

    // file 20 has a single plate_index===0 row — the existing flat row,
    // no parent/child split for it.
    expect(screen.queryByTestId('plan-parent-20')).not.toBeInTheDocument();
    expect(screen.queryByTestId('plan-plate-3')).not.toBeInTheDocument();
    expect(screen.getByText('Single Plate File')).toBeInTheDocument();
  });

  it('a child print button pins its plate via preselectedPlateId', async () => {
    await renderPlanPage();

    const printBtn = await screen.findByTestId('plan-plate-print-2');
    fireEvent.click(printBtn);

    await waitFor(() => expect(printModalMock.calls.length).toBeGreaterThan(0));
    const lastCall = printModalMock.calls[printModalMock.calls.length - 1];
    expect(lastCall.mode).toBe('reprint');
    expect(lastCall.libraryFileId).toBe(10);
    expect(lastCall.preselectedPlateId).toBe(2);
  });

  it('a child copies edit PATCHes by the plan item id, not the file id', async () => {
    await renderPlanPage();

    const row = await screen.findByTestId('plan-plate-2');
    const input = within(row).getByRole('spinbutton') as HTMLInputElement;
    fireEvent.change(input, { target: { value: '9' } });
    fireEvent.blur(input);

    await waitFor(() => expect(patchCalls.length).toBeGreaterThan(0));
    expect(patchCalls[0]).toEqual({ itemId: 2, copies: 9 });
  });

  it('a file whose plate rows are non-contiguous in the plan still forms one parent', async () => {
    // Plan order: fileA plate 1, fileB (flat), fileA plate 2 — a re-slice
    // can plant a new plate row for a file whose order_index puts it away
    // from its siblings, so grouping must not depend on adjacency. Expect
    // exactly one plan-parent-10, both plates as its children in plate
    // order, and fileB (20) still rendering flat.
    const scattered = [planItems[0], planItems[2], planItems[1]];
    await renderPlanPage(scattered);

    const parents = await screen.findAllByTestId('plan-parent-10');
    expect(parents).toHaveLength(1);

    const group = parents[0].parentElement as HTMLElement;
    // Row containers only — `plan-plate-<id>`, not their nested
    // `plan-plate-print-<id>` / `plan-plate-queue-<id>` buttons, which
    // also start with the "plan-plate-" prefix.
    const childTestIds = Array.from(group.querySelectorAll('[data-testid^="plan-plate-"]'))
      .map((el) => el.getAttribute('data-testid'))
      .filter((id): id is string => /^plan-plate-\d+$/.test(id ?? ''));
    // plate_index 1 then 2, regardless of the scattered input order above.
    expect(childTestIds).toEqual(['plan-plate-1', 'plan-plate-2']);

    expect(screen.queryByTestId('plan-parent-20')).not.toBeInTheDocument();
    const flatText = screen.getByText('Single Plate File');
    expect(flatText).toBeInTheDocument();

    // fileA's first occurrence precedes fileB's in the scattered plan, so
    // its (single) parent group renders before fileB's flat row — not
    // split around it.
    const relativePosition = parents[0].compareDocumentPosition(flatText);
    expect(relativePosition & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
