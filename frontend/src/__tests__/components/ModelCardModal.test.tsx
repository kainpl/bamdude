/**
 * One modal, two sources.
 *
 * ⚠️ **The archive half is PINNED, not redesigned.** It is the project-page
 * dialog this file's component grew out of, and it still reads the archive's
 * own parsed card and PATCHes edits back — that is the only surface in BamDude
 * that WRITES 3MF metadata, and it writes into an archive copy, never into a
 * library file. The tests below name its load, its edit form and its save so a
 * later refactor of the file half cannot quietly move it.
 *
 * The library-file half is read-only by design (spec §Decisions 5): a library
 * 3MF is somebody's source of truth and the card is database data. What it
 * offers instead is a way OUT — make a product of this file, or fill an
 * existing product's blank fields from it.
 *
 * ⚠️ **Auxiliary members are placed by the `url` the server sent**, never by
 * their category: the server decides whether a member can go behind a camera
 * stream token (`card-file`, an `<img>`) or must stay on the bearer surface
 * (`card-download`, a click), and a designer's stray `.txt` inside
 * `Model Pictures/` is a download like any other document.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { QueryClient } from '@tanstack/react-query';
import { screen, fireEvent, waitFor, within } from '@testing-library/react';
import { render } from '../utils';
import { api } from '../../api/client';
import { ModelCardModal } from '../../components/ModelCardModal';

const navigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return { ...actual, useNavigate: () => navigate };
});

const archiveCard = {
  title: 'Desk lamp',
  description: '<p>A lamp</p>',
  designer: 'Ada',
  designer_user_id: '4242',
  license: 'CC-BY',
  copyright: null,
  creation_date: '2024-01-01',
  modification_date: null,
  origin: 'MakerWorld',
  profile_title: '0.20 Standard',
  profile_description: '<p>fast</p>',
  profile_cover: null,
  profile_user_id: null,
  profile_user_name: 'Ada',
  design_model_id: '99',
  design_profile_id: null,
  design_region: null,
  model_pictures: [{ name: 'front.png', path: 'Auxiliaries/Model Pictures/front.png', url: '/img/front.png' }],
  profile_pictures: [],
  thumbnails: [],
};

const fileCard = {
  title: 'Desk lamp',
  description: '<p>A lamp</p>',
  designer: 'Ada',
  designer_user_id: '4242',
  license: 'CC-BY',
  copyright: null,
  creation_date: '2024-01-01',
  modification_date: null,
  origin: 'MakerWorld',
  profile_title: null,
  profile_description: null,
  profile_cover: null,
  profile_user_id: null,
  profile_user_name: null,
  design_model_id: '99',
  design_profile_id: null,
  design_region: null,
  auxiliaries: {
    pictures: [
      {
        name: 'front.png',
        zip_path: 'Auxiliaries/Model Pictures/front.png',
        size: 1024,
        url: '/api/v1/library/files/3/card-file/Auxiliaries/Model%20Pictures/front.png',
      },
    ],
    bom_docs: [
      {
        name: 'bom.xlsx',
        zip_path: 'Auxiliaries/Bill of Materials/bom.xlsx',
        size: 2048,
        url: '/api/v1/library/files/3/card-download/Auxiliaries/Bill%20of%20Materials/bom.xlsx',
      },
    ],
  },
  error: null,
};

describe('ModelCardModal — an archive (pinned behaviour)', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    navigate.mockReset();
  });

  it('reads the archive project page and shows what the 3MF says', async () => {
    const load = vi.spyOn(api, 'getArchiveProjectPage').mockResolvedValue(archiveCard as never);
    render(<ModelCardModal source={{ kind: 'archive', id: 12, name: 'lamp.3mf' }} onClose={() => {}} />);

    await waitFor(() => expect(load).toHaveBeenCalledWith(12));
    expect(await screen.findByText('Desk lamp')).toBeInTheDocument();
    expect(screen.getByText('Ada')).toBeInTheDocument();
    expect(screen.getByText('CC-BY')).toBeInTheDocument();
  });

  it('edits the card and PATCHes it back — the archive is the one writable card', async () => {
    vi.spyOn(api, 'getArchiveProjectPage').mockResolvedValue(archiveCard as never);
    const save = vi.spyOn(api, 'updateArchiveProjectPage').mockResolvedValue({} as never);
    render(<ModelCardModal source={{ kind: 'archive', id: 12 }} onClose={() => {}} />);

    fireEvent.click(await screen.findByRole('button', { name: /edit/i }));
    fireEvent.change(screen.getByPlaceholderText('Designer'), { target: { value: 'Grace' } });
    fireEvent.click(screen.getByRole('button', { name: /save/i }));

    await waitFor(() =>
      expect(save).toHaveBeenCalledWith(12, expect.objectContaining({ designer: 'Grace' })),
    );
  });

  it('never asks the library card route for an archive', async () => {
    const card = vi.spyOn(api, 'getLibraryFileCard');
    vi.spyOn(api, 'getArchiveProjectPage').mockResolvedValue(archiveCard as never);
    render(<ModelCardModal source={{ kind: 'archive', id: 12 }} onClose={() => {}} />);

    expect(await screen.findByText('Desk lamp')).toBeInTheDocument();
    expect(card).not.toHaveBeenCalled();
  });
});

describe('ModelCardModal — a library file', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    navigate.mockReset();
  });

  it('reads the file card and shows its fields', async () => {
    const load = vi.spyOn(api, 'getLibraryFileCard').mockResolvedValue(fileCard as never);
    render(<ModelCardModal source={{ kind: 'file', id: 3, name: 'lamp.3mf' }} onClose={() => {}} />);

    await waitFor(() => expect(load).toHaveBeenCalledWith(3));
    expect(await screen.findByText('Desk lamp')).toBeInTheDocument();
    expect(screen.getByText('Ada')).toBeInTheDocument();
  });

  it('renders a picture from the url the server built, with the stream token on it', async () => {
    vi.spyOn(api, 'getLibraryFileCard').mockResolvedValue(fileCard as never);
    render(<ModelCardModal source={{ kind: 'file', id: 3 }} onClose={() => {}} />);

    const picture = await screen.findByAltText('front.png');
    expect(picture).toHaveAttribute('src', expect.stringContaining('/card-file/'));
  });

  it('lists a non-picture auxiliary as a download, not as an image', async () => {
    vi.spyOn(api, 'getLibraryFileCard').mockResolvedValue(fileCard as never);
    render(<ModelCardModal source={{ kind: 'file', id: 3 }} onClose={() => {}} />);

    expect(await screen.findByRole('button', { name: /bom\.xlsx/i })).toBeInTheDocument();
    expect(screen.queryByAltText('bom.xlsx')).not.toBeInTheDocument();
  });

  it('never offers the archive edit form for a library file', async () => {
    vi.spyOn(api, 'getLibraryFileCard').mockResolvedValue(fileCard as never);
    render(<ModelCardModal source={{ kind: 'file', id: 3 }} onClose={() => {}} />);

    expect(await screen.findByText('Desk lamp')).toBeInTheDocument();
    expect(screen.queryByPlaceholderText('Designer')).not.toBeInTheDocument();
  });

  it('makes a product of the file and opens it', async () => {
    vi.spyOn(api, 'getLibraryFileCard').mockResolvedValue(fileCard as never);
    const create = vi.spyOn(api, 'createProductFromFile').mockResolvedValue({ id: 55 } as never);
    render(<ModelCardModal source={{ kind: 'file', id: 3 }} onClose={() => {}} />);

    fireEvent.click(await screen.findByRole('button', { name: /create product/i }));

    await waitFor(() => expect(create).toHaveBeenCalledWith(3));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/products/55'));
  });

  it('re-reads into a product the file is linked to and reports what it did', async () => {
    vi.spyOn(api, 'getLibraryFileCard').mockResolvedValue(fileCard as never);
    vi.spyOn(api, 'getProducts').mockResolvedValue([
      { id: 8, name: 'Desk lamp' },
      { id: 9, name: 'Something else' },
    ] as never);
    const reread = vi.spyOn(api, 'rereadProductCard').mockResolvedValue({
      product: { id: 8 },
      notes: [{ code: 'filled_field', params: { field: 'designer' } }],
    } as never);

    render(<ModelCardModal source={{ kind: 'file', id: 3, linkedProductIds: [8] }} onClose={() => {}} />);

    fireEvent.click(await screen.findByRole('button', { name: /re-read into/i }));
    // `findBy`: the picker opens showing `#8` and repaints with the NAME once
    // the catalog answers — the id is the fallback for a product the list does
    // not carry, not a placeholder to click through.
    const menu = await screen.findByRole('menu');
    fireEvent.click(await within(menu).findByRole('menuitem', { name: 'Desk lamp' }));

    await waitFor(() => expect(reread).toHaveBeenCalledWith(8, 3));
    expect(await screen.findByText(/filled in designer/i)).toBeInTheDocument();
  });

  it('refreshes the order cards too — a re-read can give the product its first cover', async () => {
    // Same reason as `ProductHeader`'s twin: the 3MF's Model Pictures land as
    // attachments and the first picture is the implicit cover, which an order
    // card renders off the `projects` query.
    vi.spyOn(api, 'getLibraryFileCard').mockResolvedValue(fileCard as never);
    vi.spyOn(api, 'getProducts').mockResolvedValue([{ id: 8, name: 'Desk lamp' }] as never);
    vi.spyOn(api, 'rereadProductCard').mockResolvedValue({ product: { id: 8 }, notes: [] } as never);
    const invalidate = vi.spyOn(QueryClient.prototype, 'invalidateQueries');

    render(<ModelCardModal source={{ kind: 'file', id: 3, linkedProductIds: [8] }} onClose={() => {}} />);

    fireEvent.click(await screen.findByRole('button', { name: /re-read into/i }));
    const menu = await screen.findByRole('menu');
    fireEvent.click(await within(menu).findByRole('menuitem', { name: 'Desk lamp' }));

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['product', 8] }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['products'] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['projects'] });
  });

  it('names the re-read picker as a menu before it is opened', async () => {
    vi.spyOn(api, 'getLibraryFileCard').mockResolvedValue(fileCard as never);
    render(<ModelCardModal source={{ kind: 'file', id: 3, linkedProductIds: [8] }} onClose={() => {}} />);

    const trigger = await screen.findByRole('button', { name: /re-read into/i });
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
  });

  it('places a document that merely LIVES in a card-file directory as a download', async () => {
    // The tail of the url is the member's own path inside the 3MF, so a
    // designer's folder called `card-file` used to make `includes('/card-file/')`
    // true for a bill of materials — rendered as a broken `<img>` on a token
    // surface it is deliberately not served from. The predicate is anchored to
    // the ROUTE's position instead.
    vi.spyOn(api, 'getLibraryFileCard').mockResolvedValue({
      ...fileCard,
      auxiliaries: {
        bom_docs: [
          {
            name: 'bom.xlsx',
            zip_path: 'Auxiliaries/Bill of Materials/card-file/bom.xlsx',
            size: 2048,
            url: '/api/v1/library/files/3/card-download/Auxiliaries/Bill%20of%20Materials/card-file/bom.xlsx',
          },
        ],
      },
    } as never);
    render(<ModelCardModal source={{ kind: 'file', id: 3 }} onClose={() => {}} />);

    expect(await screen.findByRole('button', { name: /bom\.xlsx/i })).toBeInTheDocument();
    expect(screen.queryByAltText('bom.xlsx')).not.toBeInTheDocument();
  });

  it('shows the MakerWorld link rather than "no card" for a file that carries only an id', async () => {
    // `hasContent` decides between the body and the empty state, so it has to
    // count everything the body can render — the id and the copyright notice
    // included, or the link sits underneath a message saying there is no card.
    vi.spyOn(api, 'getLibraryFileCard').mockResolvedValue({
      ...fileCard,
      title: null,
      description: null,
      designer: null,
      license: null,
      copyright: '© Ada 2024',
      profile_title: null,
      auxiliaries: {},
      design_model_id: '99',
    } as never);
    render(<ModelCardModal source={{ kind: 'file', id: 3 }} onClose={() => {}} />);

    expect(await screen.findByRole('link', { name: /makerworld/i })).toBeInTheDocument();
    expect(screen.getByText('© Ada 2024')).toBeInTheDocument();
    expect(screen.queryByText(/carries no model card/i)).not.toBeInTheDocument();
  });

  it('moves focus into the lightbox and gives it back on close', async () => {
    vi.spyOn(api, 'getLibraryFileCard').mockResolvedValue(fileCard as never);
    render(<ModelCardModal source={{ kind: 'file', id: 3 }} onClose={() => {}} />);

    const opener = (await screen.findByAltText('front.png')).closest('button') as HTMLButtonElement;
    opener.focus();
    fireEvent.click(opener);

    expect(screen.getByTestId('card-lightbox')).toHaveFocus();

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByTestId('card-lightbox')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it('offers no re-read at all when the file is linked to nothing', async () => {
    vi.spyOn(api, 'getLibraryFileCard').mockResolvedValue(fileCard as never);
    render(<ModelCardModal source={{ kind: 'file', id: 3 }} onClose={() => {}} />);

    expect(await screen.findByText('Desk lamp')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /re-read into/i })).not.toBeInTheDocument();
  });

  it('says the file could not be read instead of showing an empty card', async () => {
    vi.spyOn(api, 'getLibraryFileCard').mockResolvedValue({
      ...fileCard,
      title: null,
      description: null,
      designer: null,
      auxiliaries: {},
      error: 'not a zip',
    } as never);
    render(<ModelCardModal source={{ kind: 'file', id: 3 }} onClose={() => {}} />);

    expect(await screen.findByText(/not a zip/)).toBeInTheDocument();
  });

  it('closes on Escape', async () => {
    vi.spyOn(api, 'getLibraryFileCard').mockResolvedValue(fileCard as never);
    const onClose = vi.fn();
    render(<ModelCardModal source={{ kind: 'file', id: 3 }} onClose={onClose} />);

    expect(await screen.findByText('Desk lamp')).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalled();
  });
});
