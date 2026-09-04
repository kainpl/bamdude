/**
 * An import is somebody else's export, so half of what it has to say is what it
 * could NOT take — a file the library refused, a plate the 3MF no longer
 * carries, an attachment listed in the manifest and absent from the archive.
 * Those arrive as `CardNote` CODES, exactly like a card fill's, and are shown
 * before the product page opens rather than dropped on the floor.
 *
 * ⚠️ **The folder is a DESTINATION, not a link.** It is where files nobody
 * already has land; the server never joins it to the product, because
 * "everything in here belongs to this product" is not what an operator said by
 * importing into their Downloads folder. So the field is omitted entirely when
 * nothing was picked, and the server makes a folder named after the product.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { api, ApiError } from '../../../api/client';
import { ImportProductDialog } from '../../../components/products/ImportProductDialog';

const navigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return { ...actual, useNavigate: () => navigate };
});

const zip = () => new File(['PK'], 'desk-lamp.zip', { type: 'application/zip' });

function mount() {
  render(<ImportProductDialog onClose={() => {}} />);
}

describe('ImportProductDialog', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    navigate.mockReset();
    vi.spyOn(api, 'getLibraryFolders').mockResolvedValue([
      { id: 4, name: 'Lamps', parent_id: null, children: [] },
    ] as never);
  });

  it('posts the archive and opens the product it made', async () => {
    const send = vi.spyOn(api, 'importProduct').mockResolvedValue({
      product: { id: 21, name: 'Desk lamp' },
      warnings: [],
    } as never);
    mount();

    const file = zip();
    fireEvent.change(await screen.findByTestId('import-file-input'), { target: { files: [file] } });
    fireEvent.click(screen.getByRole('button', { name: /^import$/i }));

    await waitFor(() => expect(send).toHaveBeenCalledWith(file, null));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/products/21'));
  });

  it('sends the chosen folder as the destination for files nobody already has', async () => {
    const send = vi.spyOn(api, 'importProduct').mockResolvedValue({
      product: { id: 21, name: 'Desk lamp' },
      warnings: [],
    } as never);
    mount();

    fireEvent.change(await screen.findByTestId('import-file-input'), { target: { files: [zip()] } });
    fireEvent.click(await screen.findByRole('button', { name: /lamps/i }));
    fireEvent.click(screen.getByRole('button', { name: /^import$/i }));

    await waitFor(() => expect(send).toHaveBeenCalledWith(expect.any(File), 4));
  });

  it('shows every warning in the operator language, not as a code', async () => {
    vi.spyOn(api, 'importProduct').mockResolvedValue({
      product: { id: 21, name: 'Desk lamp' },
      warnings: [
        { code: 'import_plate_missing', params: { filename: 'lamp.3mf', plate_index: 2 } },
        { code: 'import_cover_missing', params: {} },
      ],
    } as never);
    mount();

    fireEvent.change(await screen.findByTestId('import-file-input'), { target: { files: [zip()] } });
    fireEvent.click(screen.getByRole('button', { name: /^import$/i }));

    expect(await screen.findByText(/no longer carries plate 2/i)).toBeInTheDocument();
    expect(screen.getByText(/cover picture is listed/i)).toBeInTheDocument();
  });

  it('renders a code it has no phrasing for rather than a key path', async () => {
    vi.spyOn(api, 'importProduct').mockResolvedValue({
      product: { id: 21, name: 'Desk lamp' },
      warnings: [{ code: 'something_new_on_the_wire', params: {} }],
    } as never);
    mount();

    fireEvent.change(await screen.findByTestId('import-file-input'), { target: { files: [zip()] } });
    fireEvent.click(screen.getByRole('button', { name: /^import$/i }));

    expect(await screen.findByText(/something_new_on_the_wire/)).toBeInTheDocument();
  });

  it('repeats the server’s own words when the archive is not an export', async () => {
    vi.spyOn(api, 'importProduct').mockRejectedValue(
      new ApiError('The uploaded file is not a ZIP archive', 400),
    );
    mount();

    fireEvent.change(await screen.findByTestId('import-file-input'), { target: { files: [zip()] } });
    fireEvent.click(screen.getByRole('button', { name: /^import$/i }));

    expect(await screen.findByText(/not a ZIP archive/i)).toBeInTheDocument();
    expect(navigate).not.toHaveBeenCalled();
  });

  it('says an over-limit archive is too large, in its own words', async () => {
    vi.spyOn(api, 'importProduct').mockRejectedValue(new ApiError('An import may be at most 1 bytes', 413));
    mount();

    fireEvent.change(await screen.findByTestId('import-file-input'), { target: { files: [zip()] } });
    fireEvent.click(screen.getByRole('button', { name: /^import$/i }));

    expect(await screen.findByText(/too large to import/i)).toBeInTheDocument();
  });

  it('cannot be submitted before an archive is chosen', async () => {
    mount();
    expect(await screen.findByRole('button', { name: /^import$/i })).toBeDisabled();
  });
});
