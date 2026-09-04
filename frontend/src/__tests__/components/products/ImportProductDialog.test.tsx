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

import { useState } from 'react';
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

/** The dialog the way a page opens it — a control that mounts it and takes it
 *  away again, which is the only shape in which "the focus comes back" can be
 *  observed at all. */
function Openable() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        open import
      </button>
      {open && <ImportProductDialog onClose={() => setOpen(false)} />}
    </>
  );
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

  it('repeats a foreign category verbatim rather than a key path', async () => {
    // ⚠️ `import_bad_category` fires exactly when the category is NOT one
    // BamDude has, so its value is foreign text by construction and looking it
    // up can only ever miss. Untranslated, the note read "Skipped foo.exe —
    // “products.attachments.category.exe” is not a category here."
    vi.spyOn(api, 'importProduct').mockResolvedValue({
      product: { id: 21, name: 'Desk lamp' },
      warnings: [{ code: 'import_bad_category', params: { name: 'foo.exe', category: 'exe' } }],
    } as never);
    mount();

    fireEvent.change(await screen.findByTestId('import-file-input'), { target: { files: [zip()] } });
    fireEvent.click(screen.getByRole('button', { name: /^import$/i }));

    expect(await screen.findByText(/skipped foo\.exe — “exe” is not a category here/i)).toBeInTheDocument();
    expect(screen.queryByText(/products\.attachments\.category/)).not.toBeInTheDocument();
  });

  it('names the ZIP’s files root when a member is over the per-member cap', async () => {
    // The backend's per-member cap fires `skipped_too_large` with
    // `category: "files"`, which is a ZIP root and not an attachment category —
    // it still needs a word an operator recognises.
    vi.spyOn(api, 'importProduct').mockResolvedValue({
      product: { id: 21, name: 'Desk lamp' },
      warnings: [
        { code: 'skipped_too_large', params: { name: 'huge.3mf', size: 2048, limit: 1024, category: 'files' } },
      ],
    } as never);
    mount();

    fireEvent.change(await screen.findByTestId('import-file-input'), { target: { files: [zip()] } });
    fireEvent.click(screen.getByRole('button', { name: /^import$/i }));

    expect(await screen.findByText(/skipped huge\.3mf/i)).toBeInTheDocument();
    expect(screen.queryByText(/products\.attachments\.category/)).not.toBeInTheDocument();
  });

  it('repeats an unknown field name rather than a key path', async () => {
    vi.spyOn(api, 'importProduct').mockResolvedValue({
      product: { id: 21, name: 'Desk lamp' },
      warnings: [{ code: 'filled_field', params: { field: 'some_new_column' } }],
    } as never);
    mount();

    fireEvent.change(await screen.findByTestId('import-file-input'), { target: { files: [zip()] } });
    fireEvent.click(screen.getByRole('button', { name: /^import$/i }));

    expect(await screen.findByText(/filled in some_new_column/i)).toBeInTheDocument();
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

  it('is a modal dialog named by its heading, and hands the focus back on Escape', async () => {
    // ⚠️ The overlay is opened from a page that stays in the tree behind it, so
    // without the role and the focus move a screen reader announces nothing at
    // all and a keyboard user starts at the top of the PAGE — every control
    // behind the dialog comes before anything in it. `useDialogFocus` is not a
    // trap and does not claim to be: Tab still walks out. What it fixes is the
    // two ends, and the return end is only observable if the dialog actually
    // unmounts — hence the opener rather than a bare mount.
    render(<Openable />);
    const opener = screen.getByRole('button', { name: 'open import' });
    opener.focus();
    fireEvent.click(opener);

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleName('Import a product');
    expect(dialog).toHaveFocus();

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });
});
