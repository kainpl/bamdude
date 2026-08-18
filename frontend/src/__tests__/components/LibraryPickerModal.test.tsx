/**
 * Picking a batch of library files for a queue.
 *
 * Two things are worth pinning. First, what the dialog is allowed to offer:
 * exactly what the drop zones accept, so a file chosen here can never be
 * refused by the Schedule dialog it is about to open. Second, that the
 * selection survives moving around — a picker whose ticks reset when you
 * change folder is a picker for one folder, which is the thing the File
 * Manager already was.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider } from 'react-i18next';
import i18n from '../../i18n';
import { LibraryPickerModal } from '../../components/LibraryPickerModal';
import { offerableFiles } from '../../lib/offerableFiles';
import { api, type LibraryFileListItem, type LibraryFolderTree } from '../../api/client';

const file = (over: Partial<LibraryFileListItem> = {}): LibraryFileListItem =>
  ({
    id: 1,
    folder_id: null,
    project_ids: [],
    is_external: false,
    filename: 'part.gcode.3mf',
    file_type: '3mf',
    file_tags: ['gcode', '3mf'],
    file_size: 1024,
    thumbnail_path: null,
    duplicate_count: 0,
    created_at: '2026-08-01T00:00:00Z',
    fs_modified_at: null,
    print_name: null,
    sliced_for_model: 'P1S',
    ...over,
  }) as LibraryFileListItem;

const folder = (id: number, name: string): LibraryFolderTree =>
  ({ id, name, children: [], is_external: false, external_readonly: false }) as LibraryFolderTree;

function renderPicker(props: Partial<Parameters<typeof LibraryPickerModal>[0]> = {}) {
  const onConfirm = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}>
        <LibraryPickerModal
          targetName="Farm A"
          onCancel={vi.fn()}
          onConfirm={onConfirm}
          {...props}
        />
      </I18nextProvider>
    </QueryClientProvider>,
  );
  return { onConfirm };
}

describe('what it is allowed to offer', () => {
  it('drops a file with no G-code inside, whatever its extension says', () => {
    const unsliced = file({ id: 2, file_tags: ['3mf', 'project'] });

    expect(offerableFiles([file(), unsliced])).toHaveLength(1);
  });

  it('drops a file with no recorded model — nothing about it can be verified', () => {
    expect(offerableFiles([file({ sliced_for_model: null })])).toHaveLength(0);
  });

  it('keeps only what is sliced for the printer being loaded', () => {
    const files = [file({ id: 1, sliced_for_model: 'P1S' }), file({ id: 2, sliced_for_model: 'A1' })];

    expect(offerableFiles(files, 'P1S').map((f) => f.id)).toEqual([1]);
  });

  it('compares models case-insensitively', () => {
    expect(offerableFiles([file({ sliced_for_model: 'p1s' })], 'P1S')).toHaveLength(1);
  });

  it('filters nothing when the model cannot be mapped, exactly as a drop does', () => {
    // `mapModelCode` answers '' only for a missing model. Refusing everything
    // on that basis would make an unrecognised machine unloadable.
    const files = [file({ id: 1, sliced_for_model: 'P1S' }), file({ id: 2, sliced_for_model: 'A1' })];

    expect(offerableFiles(files, '')).toHaveLength(2);
  });

  it('offers every model when there is no printer to match — the auto-queue', () => {
    const files = [file({ id: 1, sliced_for_model: 'P1S' }), file({ id: 2, sliced_for_model: 'A1' })];

    expect(offerableFiles(files)).toHaveLength(2);
  });

  it('survives files it has not been given yet', () => {
    expect(offerableFiles(undefined)).toEqual([]);
  });
});

describe('the dialog', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getLibraryFolders').mockResolvedValue([folder(7, 'Minis'), folder(8, 'Spares')]);
  });

  it('shows only what it may offer', async () => {
    vi.spyOn(api, 'getLibraryFiles').mockResolvedValue([
      file({ id: 1, filename: 'ready.gcode.3mf' }),
      file({ id: 2, filename: 'source.3mf', file_tags: ['3mf', 'project'] }),
    ]);

    renderPicker();

    expect(await screen.findByText('ready.gcode.3mf')).toBeInTheDocument();
    expect(screen.queryByText('source.3mf')).not.toBeInTheDocument();
  });

  it('keeps a tick made in one folder after moving to another', async () => {
    vi.spyOn(api, 'getLibraryFiles').mockResolvedValue([
      file({ id: 1, filename: 'in-minis.gcode.3mf', folder_id: 7 }),
      file({ id: 2, filename: 'in-spares.gcode.3mf', folder_id: 8 }),
    ]);
    const { onConfirm } = renderPicker();
    const user = userEvent.setup();

    await user.click(await screen.findByText('Minis'));
    await user.click(await screen.findByText('in-minis.gcode.3mf'));
    await user.click(screen.getByText('Spares'));
    // The first file is out of sight now — the selection is not.
    expect(screen.queryByText('in-minis.gcode.3mf')).not.toBeInTheDocument();
    await user.click(await screen.findByText('in-spares.gcode.3mf'));
    await user.click(screen.getByRole('button', { name: /Add to queue/i }));

    expect(onConfirm).toHaveBeenCalledWith([
      { id: 1, name: 'in-minis.gcode.3mf' },
      { id: 2, name: 'in-spares.gcode.3mf' },
    ]);
  });

  it('unticks what is ticked again', async () => {
    vi.spyOn(api, 'getLibraryFiles').mockResolvedValue([file({ id: 1, filename: 'one.gcode.3mf' })]);
    renderPicker();
    const user = userEvent.setup();

    const row = await screen.findByText('one.gcode.3mf');
    await user.click(row);
    await user.click(row);

    expect(screen.getByRole('button', { name: /Add to queue/i })).toBeDisabled();
  });

  it('searches the whole library, not the folder that happens to be open', async () => {
    vi.spyOn(api, 'getLibraryFiles').mockResolvedValue([
      file({ id: 1, filename: 'in-minis.gcode.3mf', folder_id: 7 }),
      file({ id: 2, filename: 'in-spares.gcode.3mf', folder_id: 8 }),
    ]);
    renderPicker();
    const user = userEvent.setup();

    await user.click(await screen.findByText('Minis'));
    await user.type(screen.getByPlaceholderText(/Search the whole library/i), 'spares');

    expect(await screen.findByText('in-spares.gcode.3mf')).toBeInTheDocument();
  });

  it('hands back the print name where there is one', async () => {
    vi.spyOn(api, 'getLibraryFiles').mockResolvedValue([
      file({ id: 5, filename: 'ugly-export.gcode.3mf', print_name: 'Bracket v3' }),
    ]);
    const { onConfirm } = renderPicker();
    const user = userEvent.setup();

    await user.click(await screen.findByText('Bracket v3'));
    await user.click(screen.getByRole('button', { name: /Add to queue/i }));

    expect(onConfirm).toHaveBeenCalledWith([{ id: 5, name: 'Bracket v3' }]);
  });

  it('says why it is empty when the model has nothing sliced for it', async () => {
    vi.spyOn(api, 'getLibraryFiles').mockResolvedValue([file({ sliced_for_model: 'A1' })]);

    renderPicker({ printerModel: 'P1S' });

    expect(await screen.findByText(/sliced for P1S/i)).toBeInTheDocument();
  });

  it('cannot be confirmed with nothing ticked', async () => {
    vi.spyOn(api, 'getLibraryFiles').mockResolvedValue([file()]);
    const { onConfirm } = renderPicker();

    await waitFor(() => expect(screen.getByRole('button', { name: /Add to queue/i })).toBeDisabled());
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
