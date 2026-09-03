import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import { FromFileDialog } from '../../../components/products/FromFileDialog';

/** The real envelope of `getLibraryFilesPaged`: `{items, meta}` — NOT the
 *  `{items, total}` sketch in the brief, and the rows carry `folder_id`, not a
 *  `folder_path` (the path is resolved from the folder tree). */
const page = {
  items: [{ id: 7, filename: 'flask.3mf', folder_id: 3, file_type: '3mf' }],
  meta: { total: 1, current_page: 1, per_page: 20, last_page: 1 },
};

const folders = [
  { id: 3, name: 'Flasks', parent_id: null, projects: [], is_external: false, external_path: null, external_readonly: false, file_count: 1, children: [] },
];

describe('FromFileDialog', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getLibraryFolders').mockResolvedValue(folders as never);
  });

  it('creates a product from the picked file', async () => {
    vi.spyOn(api, 'getLibraryFilesPaged').mockResolvedValue(page as never);
    const create = vi.spyOn(api, 'createProductFromFile').mockResolvedValue({ id: 9, name: 'flask' } as never);
    const onCreated = vi.fn();
    render(<FromFileDialog onClose={() => {}} onCreated={onCreated} />);
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'flask' } });
    fireEvent.click(await screen.findByRole('button', { name: /create product/i }));
    await waitFor(() => expect(create).toHaveBeenCalledWith(7));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(expect.objectContaining({ id: 9 })));
  });

  it('searches the library through the server, debounced', async () => {
    const list = vi.spyOn(api, 'getLibraryFilesPaged').mockResolvedValue(page as never);
    render(<FromFileDialog onClose={() => {}} onCreated={() => {}} />);
    await screen.findByText('flask.3mf');
    // The folder is named, so two files of the same name stay tellable apart.
    expect(screen.getByText('/Flasks')).toBeInTheDocument();
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'lid' } });
    await waitFor(() => expect(list).toHaveBeenLastCalledWith({ q: 'lid', page: 1, per_page: 20 }));
  });

  it('a failed creation is a toast, and the dialog stays open', async () => {
    vi.spyOn(api, 'getLibraryFilesPaged').mockResolvedValue(page as never);
    vi.spyOn(api, 'createProductFromFile').mockRejectedValue(new Error('That file is already a product'));
    const onCreated = vi.fn();
    render(<FromFileDialog onClose={() => {}} onCreated={onCreated} />);
    fireEvent.click(await screen.findByRole('button', { name: /create product/i }));
    expect(await screen.findByText(/already a product/i)).toBeInTheDocument();
    expect(onCreated).not.toHaveBeenCalled();
    expect(screen.getByRole('searchbox')).toBeInTheDocument();
  });
});
