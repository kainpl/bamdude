/**
 * The Progress card must vanish for a target of 0 — not leave a bare "0" behind.
 *
 * `0 && <jsx>` evaluates to 0, and React renders the NUMBER rather than nothing.
 * Once a target could legitimately be 0 ("don't measure this project in plates,
 * only in parts") every one of those short-circuits painted a stray zero where
 * the block used to be. Reported from the running app with a "0" sitting above
 * the parts bar.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { Routes, Route } from 'react-router-dom';
import { http, HttpResponse } from 'msw';

import { render } from '../utils';
import { server } from '../mocks/server';
import { strayZeroTextNodes as strayZeroes } from '../domHelpers';
import { ProjectDetailPage } from '../../pages/ProjectDetailPage';

const stats = {
  total_archives: 2,
  total_items: 6,
  completed_prints: 3,
  defective_parts: 0,
  failed_prints: 0,
  queued_prints: 0,
  in_progress_prints: 0,
  total_print_time_hours: 1,
  total_filament_grams: 100,
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

function mockProject(targetCount: number, targetPartsCount: number) {
  return {
    id: 1,
    name: 'Zero Target Project',
    description: '',
    color: '#00ae42',
    status: 'active',
    target_count: targetCount,
    target_parts_count: targetPartsCount,
    // The page reads these unguarded (``project.budget !== null`` then
    // ``.toFixed``), so a fixture that omits them crashes the render rather
    // than testing anything.
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
    stats: {
      ...stats,
      progress_percent: targetCount > 0 ? 40 : null,
      parts_progress_percent: targetPartsCount > 0 ? 15 : null,
    },
  };
}

async function renderWith(targetCount: number, targetPartsCount: number) {
  server.use(
    http.get('/api/v1/projects/1', () => HttpResponse.json(mockProject(targetCount, targetPartsCount))),
    http.get('/api/v1/projects/1/archives', () => HttpResponse.json([])),
    http.get('/api/v1/projects/1/bom', () => HttpResponse.json([])),
    http.get('/api/v1/projects/1/timeline', () => HttpResponse.json([])),
    http.get('/api/v1/projects/1/folders', () => HttpResponse.json([])),
    http.get('/api/v1/projects/1/files', () => HttpResponse.json([])),
    http.get('/api/v1/projects/1/print-plan', () => HttpResponse.json({ items: [] })),
    http.get('/api/v1/projects/', () => HttpResponse.json([])),
  );

  window.history.pushState({}, '', '/projects/1');
  render(
    <Routes>
      <Route path="/projects/:id" element={<ProjectDetailPage />} />
    </Routes>,
  );

  // The name appears in the breadcrumb and in the header — findAllBy, not getBy.
  await waitFor(() => expect(screen.getAllByText('Zero Target Project').length).toBeGreaterThan(0));
}

describe('ProjectDetailPage — a target of 0 hides its progress bar', () => {
  beforeEach(() => {
    window.history.pushState({}, '', '/');
  });

  it('measuring by parts only leaves no stray zero', async () => {
    await renderWith(0, 20);

    expect(screen.getByText(/Parts Progress/i)).toBeInTheDocument();
    expect(screen.queryByText(/Plates Progress/i)).not.toBeInTheDocument();
    expect(strayZeroes()).toHaveLength(0);
  });

  it('measuring by plates only leaves no stray zero', async () => {
    await renderWith(5, 0);

    expect(screen.getByText(/Plates Progress/i)).toBeInTheDocument();
    expect(screen.queryByText(/Parts Progress/i)).not.toBeInTheDocument();
    expect(strayZeroes()).toHaveLength(0);
  });

  it('no targets at all leaves no stray zero', async () => {
    await renderWith(0, 0);

    expect(screen.queryByText(/Progress/i)).not.toBeInTheDocument();
    expect(strayZeroes()).toHaveLength(0);
  });
});
