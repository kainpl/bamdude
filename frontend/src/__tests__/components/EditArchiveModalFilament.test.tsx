/**
 * Filament used (g) in Edit Archive (audit D6 part 2, upstream d227d422).
 *
 * A print whose 3MF never arrived has no weight, and nothing but the operator
 * can supply one. The field is the archive's figure only — no spool is debited.
 *
 * It is a text field, not a number input: a number input reports "" for
 * anything the browser judges malformed — a decimal comma included — and that
 * would read as "cleared" and wipe a good figure while the field still showed
 * what was typed.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { EditArchiveModal } from '../../components/EditArchiveModal';
import type { Archive } from '../../api/client';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const baseArchive = {
  id: 7,
  printer_id: 1,
  project_id: null,
  project_line_id: null,
  filename: 'lamp.gcode.3mf',
  file_path: '',
  print_name: 'Lamp',
  status: 'completed',
  filament_used_grams: null,
  tags: null,
  notes: null,
  cost: null,
  photos: null,
  failure_reason: null,
  quantity: 1,
  defective_count: 0,
  parts: [],
  external_url: null,
  extra_data: null,
} as unknown as Archive;

let sent: Record<string, unknown>[] = [];

function renderModal(archive: Partial<Archive> = {}) {
  render(<EditArchiveModal archive={{ ...baseArchive, ...archive } as Archive} onClose={vi.fn()} />);
}

const field = () => screen.getByLabelText(/filament used/i);

async function save(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: /save/i }));
  await waitFor(() => expect(sent).toHaveLength(1));
  return sent[0];
}

describe('EditArchiveModal — filament used', () => {
  beforeEach(() => {
    sent = [];
    server.use(
      http.get('/api/v1/projects/', () => HttpResponse.json([])),
      http.get('/api/v1/archives/tags', () => HttpResponse.json([])),
      http.patch('/api/v1/archives/:id', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        sent.push(body);
        return HttpResponse.json({ ...baseArchive, ...body });
      }),
    );
  });

  it('shows the archive figure and says no spool is debited', () => {
    renderModal({ filament_used_grams: 46.16 });
    expect(field()).toHaveValue('46.16');
    expect(screen.getByText(/no spool is debited/i)).toBeInTheDocument();
  });

  it('sends a typed figure', async () => {
    const user = userEvent.setup();
    renderModal();
    await user.type(field(), '46.16');
    expect((await save(user)).filament_used_grams).toBe(46.16);
  });

  it('reads a decimal comma as a decimal point', async () => {
    const user = userEvent.setup();
    renderModal();
    await user.type(field(), '46,16');
    expect(field()).toHaveValue('46,16');
    expect((await save(user)).filament_used_grams).toBe(46.16);
  });

  it('keeps only a number while typing', async () => {
    const user = userEvent.setup();
    renderModal();
    await user.type(field(), '4a6.1.6g');
    expect(field()).toHaveValue('46.16');
  });

  it('clamps to what the server accepts', async () => {
    const user = userEvent.setup();
    renderModal();
    await user.type(field(), '250000');
    expect(field()).toHaveValue('100000');
  });

  it('sends null when the figure is cleared', async () => {
    const user = userEvent.setup();
    renderModal({ filament_used_grams: 12 });
    await user.clear(field());
    expect((await save(user)).filament_used_grams).toBeNull();
  });

  it('leaves an untouched figure out of the save', async () => {
    const user = userEvent.setup();
    renderModal({ filament_used_grams: 12.345678 });
    expect('filament_used_grams' in (await save(user))).toBe(false);
  });
});
