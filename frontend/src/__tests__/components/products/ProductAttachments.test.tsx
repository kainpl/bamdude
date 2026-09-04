/**
 * ⚠️ **Downloads go through `fetch`, not a bare `<a href>`.** The route is
 * behind `PROJECTS_READ` and this app authenticates with a bearer token, which
 * a link cannot carry — the link would 401 and look like a missing file. The
 * blob is revoked afterwards, so the test spies on both halves of the pair:
 * a `createObjectURL` without its `revokeObjectURL` leaks the whole file for
 * as long as the tab lives.
 *
 * `pictures` are deliberately absent from this section: they are the gallery,
 * and showing them twice would give the operator two delete buttons for one
 * file.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, fireEvent, waitFor, within } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Product } from '../../../api/client';
import { ProductAttachments } from '../../../components/products/ProductAttachments';

const entry = (category: string, filename: string, original: string, source = 'manual') => ({
  category,
  filename,
  original_name: original,
  size: 2048,
  sort_order: 0,
  source,
  source_file_id: null,
  uploaded_at: null,
});

const product = {
  id: 7,
  name: 'Flask',
  has_cover: false,
  cover_image_filename: null,
  attachments: [
    entry('bom_docs', 'bom.xlsx', 'bill-of-materials.xlsx'),
    entry('assembly', 'guide.pdf', 'assembly.pdf'),
    entry('pictures', 'a.png', 'front.png'),
  ],
} as unknown as Product;

/**
 * Answer the attachment download, and ONLY it, with a canned response.
 *
 * A blanket `vi.stubGlobal('fetch', …)` would also answer `AuthProvider`'s own
 * startup calls, which run through `request()` and would then reject on a
 * response object that has no `json` — the component under test would render
 * with no permissions and the assertion would fail for a reason that has
 * nothing to do with downloading. Returns the array of URLs it intercepted.
 */
function onlyAttachmentFetch(canned: Partial<Response> & { blob: () => Promise<Blob> }): string[] {
  const seen: string[] = [];
  const real = globalThis.fetch;
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/attachments/')) {
        seen.push(url);
        return Promise.resolve(canned as unknown as Response);
      }
      return real(input, init);
    }),
  );
  return seen;
}

describe('ProductAttachments', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('uploads into the category whose section was used', async () => {
    const upload = vi.spyOn(api, 'uploadProductAttachment').mockResolvedValue({} as never);
    render(<ProductAttachments product={product} canEdit />);

    const file = new File(['x'], 'bom.csv', { type: 'text/csv' });
    fireEvent.change(screen.getByTestId('attachment-input-bom_docs'), { target: { files: [file] } });
    await waitFor(() => expect(upload).toHaveBeenCalledWith(7, file, 'bom_docs'));
  });

  it('an upload into one section leaves the other sections usable', async () => {
    // One mutation serves all three sections, so a bare `isPending` greyed out
    // every Upload button while any one section was busy — three controls
    // reporting one section's work, and an operator with three documents to
    // attach waiting for each in turn.
    let settle: (value: unknown) => void = () => {};
    vi.spyOn(api, 'uploadProductAttachment').mockReturnValue(
      new Promise((resolve) => {
        settle = resolve;
      }) as never,
    );
    render(<ProductAttachments product={product} canEdit />);

    const upload = (category: string) =>
      within(screen.getByTestId(`attachment-section-${category}`)).getByRole('button', { name: /upload/i });

    fireEvent.change(screen.getByTestId('attachment-input-bom_docs'), {
      target: { files: [new File(['x'], 'bom.csv', { type: 'text/csv' })] },
    });

    await waitFor(() => expect(upload('bom_docs')).toBeDisabled());
    expect(upload('assembly')).toBeEnabled();
    expect(upload('other')).toBeEnabled();

    settle({});
    await waitFor(() => expect(upload('bom_docs')).toBeEnabled());
  });

  it('downloads through fetch and revokes the object URL', async () => {
    const create = vi.fn().mockReturnValue('blob:product-attachment');
    const revoke = vi.fn();
    const seen = onlyAttachmentFetch({ ok: true, blob: async () => new Blob(['x']) });
    Object.defineProperty(window.URL, 'createObjectURL', { value: create, configurable: true });
    Object.defineProperty(window.URL, 'revokeObjectURL', { value: revoke, configurable: true });

    render(<ProductAttachments product={product} canEdit />);
    fireEvent.click(screen.getByRole('button', { name: /download: bill-of-materials\.xlsx/i }));

    await waitFor(() => expect(create).toHaveBeenCalled());
    await waitFor(() => expect(revoke).toHaveBeenCalledWith('blob:product-attachment'));
    expect(seen[0]).toContain('/products/7/attachments/bom.xlsx');
  });

  it('says so when a download is refused instead of saving an error page', async () => {
    // ⚠️ A SENTENCE with the status in it, not a bare `HTTP 404`. Every other
    // refusal on this page is the server's own words, already in the operator's
    // language; this was the one message that would have read as English to
    // everybody.
    onlyAttachmentFetch({ ok: false, status: 404, blob: async () => new Blob([]) });
    render(<ProductAttachments product={product} canEdit />);

    fireEvent.click(screen.getByRole('button', { name: /download: bill-of-materials\.xlsx/i }));
    expect(await screen.findByText(/could not download this file \(HTTP 404\)/i)).toBeInTheDocument();
  });

  it('deletes an attachment', async () => {
    const remove = vi.spyOn(api, 'deleteProductAttachment').mockResolvedValue([] as never);
    render(<ProductAttachments product={product} canEdit />);

    fireEvent.click(screen.getByRole('button', { name: /delete: assembly\.pdf/i }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith(7, 'guide.pdf'));
  });

  it('says where a document came from, and says nothing about a plain upload', () => {
    // ⚠️ Provenance is the thing an operator cannot recover any other way: a
    // file read out of the 3MF is replaced wholesale by the next re-read, and
    // one that arrived in an export ZIP belongs to somebody else's farm.
    // `manual` stays unlabelled on purpose — a badge on every row says nothing.
    render(
      <ProductAttachments
        product={{
          ...product,
          attachments: [
            entry('bom_docs', 'a.pdf', 'from-the-3mf.pdf', '3mf'),
            entry('bom_docs', 'b.pdf', 'from-an-export.pdf', 'import'),
            entry('bom_docs', 'c.pdf', 'uploaded-here.pdf', 'manual'),
          ],
        }}
        canEdit
      />,
    );

    const row = (name: string) => screen.getByText(name).closest('li') as HTMLElement;
    expect(within(row('from-the-3mf.pdf')).getByText(/from the 3MF/i)).toBeInTheDocument();
    expect(within(row('from-an-export.pdf')).getByText(/imported/i)).toBeInTheDocument();
    expect(within(row('uploaded-here.pdf')).queryByText(/from the 3MF|imported/i)).not.toBeInTheDocument();
  });

  it('leaves a source it does not know unlabelled rather than mislabelled', () => {
    // The wire type is a plain `str` (a hand-edited column or a restored backup
    // can carry a fourth value), so the branches are equalities and there is no
    // else-arm to guess with.
    render(
      <ProductAttachments
        product={{ ...product, attachments: [entry('bom_docs', 'd.pdf', 'from-the-future.pdf', 'sync')] }}
        canEdit
      />,
    );

    const row = screen.getByText('from-the-future.pdf').closest('li') as HTMLElement;
    expect(within(row).queryByText(/from the 3MF|imported/i)).not.toBeInTheDocument();
  });

  it('never lists a picture — that file belongs to the gallery', () => {
    render(<ProductAttachments product={product} canEdit />);

    expect(screen.getByText('bill-of-materials.xlsx')).toBeInTheDocument();
    expect(screen.queryByText('front.png')).not.toBeInTheDocument();
  });

  it('shows every category, empty ones included, so the operator can fill them', () => {
    render(<ProductAttachments product={{ ...product, attachments: [] }} canEdit />);

    expect(screen.getByTestId('attachment-section-bom_docs')).toBeInTheDocument();
    expect(screen.getByTestId('attachment-section-assembly')).toBeInTheDocument();
    expect(screen.getByTestId('attachment-section-other')).toBeInTheDocument();
  });

  it('offers no upload or delete without the permission', () => {
    render(<ProductAttachments product={product} canEdit={false} />);

    expect(screen.queryByTestId('attachment-input-bom_docs')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /delete: assembly\.pdf/i })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /download: bill-of-materials\.xlsx/i })).toBeInTheDocument();
  });

  it('breaks a sort_order tie on the filename, exactly as the gallery does', () => {
    // This list sorted by `sort_order` alone while the gallery and the server
    // both broke the tie on `filename` — and a tie is ordinary, since every
    // upload into an empty category starts at 0. Both components go through
    // `byAttachmentOrder` now, so the same two entries come out in the same
    // order wherever they are shown.
    const tied = {
      ...product,
      attachments: [entry('bom_docs', 'z.pdf', 'zebra.pdf'), entry('bom_docs', 'a.pdf', 'alpha.pdf')],
    } as unknown as Product;
    render(<ProductAttachments product={tied} canEdit />);

    const shown = within(screen.getByTestId('attachment-section-bom_docs'))
      .getAllByRole('listitem')
      .map((li) => li.textContent);
    expect(shown[0]).toContain('alpha.pdf');
    expect(shown[1]).toContain('zebra.pdf');
  });
});
