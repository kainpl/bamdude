/**
 * The file details dialog: a file's facts, and the one thing in it the
 * operator edits — a link of their own.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { LibraryFileDetailsModal } from '../../components/LibraryFileDetailsModal';
import type { LibraryFileListItem } from '../../api/client';

const HASH = 'a3f5c9d2e1b40718293a4b5c6d7e8f90112233445566778899aabbccddeeff00';

const listed = {
  id: 7,
  folder_id: 3,
  product_ids: [],
  is_external: false,
  filename: 'lamp.3mf',
  file_type: '3mf',
  file_tags: [],
  file_size: 2 * 1024 * 1024,
  thumbnail_path: null,
  duplicate_count: 0,
  created_by_id: 1,
  created_by_username: 'iryna',
  created_at: '2024-01-02T10:00:00Z',
  fs_modified_at: null,
  print_name: null,
  print_time_seconds: null,
  filament_used_grams: null,
  object_count: null,
  skip_objects_supported: false,
  sliced_for_model: null,
  plan_eligible: false,
  swap_compatible: false,
  swap_compatibility_known: false,
  notes_count: 2,
  print_count: 0,
  tags: [],
} as unknown as LibraryFileListItem;

function detail(extra: Record<string, unknown> = {}) {
  return {
    ...listed,
    folder_name: 'Lamps',
    products: [],
    file_path: 'library/files/x.3mf',
    file_hash: HASH,
    metadata: null,
    last_printed_at: null,
    notes: null,
    duplicates: null,
    updated_at: '2024-03-04T12:30:00Z',
    source_type: null,
    source_url: null,
    external_url: null,
    ...extra,
  };
}

let saved: unknown[] = [];

function serve(file: Record<string, unknown>, put?: (body: { external_url: string | null }) => Response) {
  server.use(
    http.get('/api/v1/library/files/7', () => HttpResponse.json(file)),
    http.put('/api/v1/library/files/7/external-url', async ({ request }) => {
      const body = (await request.json()) as { external_url: string | null };
      saved.push(body);
      if (put) return put(body);
      return HttpResponse.json({ ...file, external_url: body.external_url });
    }),
  );
}

function show(canEdit = true) {
  render(<LibraryFileDetailsModal file={listed} canEdit={canEdit} onClose={() => {}} />);
  return screen.findByRole('dialog', { name: 'File details' });
}

describe('LibraryFileDetailsModal', () => {
  beforeEach(() => {
    saved = [];
  });

  it("shows the file's facts", async () => {
    serve(detail());
    const dialog = await show();

    const fact = async (label: string) => {
      const term = await within(dialog).findByText(label);
      return term.nextElementSibling as HTMLElement;
    };
    expect(await fact('Name')).toHaveTextContent('lamp.3mf');
    expect(await fact('Type')).toHaveTextContent('3MF');
    expect(await fact('Size')).toHaveTextContent('2.0 MB');
    await waitFor(async () => expect(await fact('Folder')).toHaveTextContent('Lamps'));
    expect(await fact('Added by')).toHaveTextContent('iryna');
    expect(await fact('Notes')).toHaveTextContent('2');
    // The hash in its short form; the whole of it is one hover away.
    const hash = await fact('Hash');
    await waitFor(() => expect(hash).toHaveTextContent('a3f5c9d2e1b4'));
    expect(hash).not.toHaveTextContent(HASH);
    expect(within(hash).getByTitle(HASH)).toBeInTheDocument();
  });

  it('says a file outside every folder is in the library root', async () => {
    serve(detail({ folder_id: null, folder_name: null }));
    const dialog = await show();

    const term = await within(dialog).findByText('Folder');
    await waitFor(() => expect(term.nextElementSibling).toHaveTextContent('Library root'));
  });

  it("shows the file's origin as a link nobody edits here", async () => {
    serve(detail({ source_type: 'makerworld', source_url: 'https://makerworld.com/models/1' }));
    const dialog = await show();

    const origin = await within(dialog).findByRole('link', { name: 'https://makerworld.com/models/1' });
    expect(origin).toHaveAttribute('href', 'https://makerworld.com/models/1');
    expect(origin).toHaveAttribute('target', '_blank');
    expect(origin).toHaveAttribute('rel', 'noopener noreferrer');
    expect(within(dialog).queryByDisplayValue('https://makerworld.com/models/1')).not.toBeInTheDocument();
  });

  it('saves a link the operator types, and shows it as a link that opens apart from the app', async () => {
    serve(detail());
    const dialog = await show();

    const box = await within(dialog).findByRole('textbox', { name: 'Link' });
    await userEvent.type(box, 'https://www.printables.com/model/1');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(saved).toEqual([{ external_url: 'https://www.printables.com/model/1' }]));
    const link = await within(dialog).findByRole('link', { name: 'Open link' });
    expect(link).toHaveAttribute('href', 'https://www.printables.com/model/1');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('removes a saved link', async () => {
    serve(detail({ external_url: 'https://example.com/a' }));
    const dialog = await show();

    expect(await within(dialog).findByDisplayValue('https://example.com/a')).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Remove link' }));

    await waitFor(() => expect(saved).toEqual([{ external_url: null }]));
    await waitFor(() => expect(within(dialog).queryByRole('link', { name: 'Open link' })).not.toBeInTheDocument());
    expect(within(dialog).getByRole('textbox', { name: 'Link' })).toHaveValue('');
  });

  it("shows the server's refusal and keeps what was typed", async () => {
    serve(detail(), () =>
      HttpResponse.json({ detail: 'The link must be a web address that starts with http:// or https://.' }, { status: 422 }),
    );
    const dialog = await show();

    const box = await within(dialog).findByRole('textbox', { name: 'Link' });
    await userEvent.type(box, 'javascript:alert(1)');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('The link must be a web address');
    expect(box).toHaveValue('javascript:alert(1)');
    expect(within(dialog).queryByRole('link', { name: 'Open link' })).not.toBeInTheDocument();
  });

  it('never renders a stored address that is not http(s) as a link', async () => {
    // The server refuses such a value; a row written some other way must still not become an href.
    serve(detail({ external_url: 'javascript:alert(1)', source_url: 'javascript:alert(2)' }));
    const dialog = await show(false);

    await within(dialog).findByText('javascript:alert(1)');
    expect(within(dialog).queryAllByRole('link')).toEqual([]);
  });

  it('is read-only for someone who may not edit the file', async () => {
    serve(detail({ external_url: 'https://example.com/a' }));
    const dialog = await show(false);

    const link = await within(dialog).findByRole('link', { name: 'https://example.com/a' });
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(within(dialog).queryByRole('textbox', { name: 'Link' })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: 'Save' })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: 'Remove link' })).not.toBeInTheDocument();
  });
});
