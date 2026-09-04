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

import { useState } from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { QueryClient } from '@tanstack/react-query';
import { screen, fireEvent, waitFor, within } from '@testing-library/react';
import { render } from '../utils';
import { api } from '../../api/client';
import { ModelCardModal } from '../../components/ModelCardModal';
import type { ModelCardSource } from '../../components/ModelCardModal';

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

/** The modal the way a page opens it — a control that mounts it and takes it
 *  away again, which is the only shape in which "the focus comes back" can be
 *  observed at all. */
function Openable({ source }: { source: ModelCardSource }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        open card
      </button>
      {open && <ModelCardModal source={source} onClose={() => setOpen(false)} />}
    </>
  );
}

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

  it('says every one of its own words through the catalogue, not in hardcoded English', async () => {
    // ⚠️ This half predates the modal and shipped with its labels written into
    // the JSX — Edit / Save / Cancel, the field placeholders, the two empty
    // states, "Images (n)". Nothing about it was translatable, on a screen
    // whose other half always was. Only the STRINGS moved: the load, the edit
    // form and the PATCH are pinned by the three tests around this one.
    vi.spyOn(api, 'getArchiveProjectPage').mockResolvedValue(archiveCard as never);
    render(<ModelCardModal source={{ kind: 'archive', id: 12 }} onClose={() => {}} />);

    // The count is a plural, so it comes from the catalogue with its own forms.
    expect(await screen.findByRole('heading', { name: 'Images (1)' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /edit/i }));
    expect(screen.getByPlaceholderText('Profile title')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Description' })).toBeInTheDocument();
  });

  it('says a load failure and an empty card in the catalogue’s words too', async () => {
    vi.spyOn(api, 'getArchiveProjectPage').mockRejectedValue(new Error('nope'));
    const { unmount } = render(<ModelCardModal source={{ kind: 'archive', id: 12 }} onClose={() => {}} />);
    expect(await screen.findByText('Could not load the model card of this print.')).toBeInTheDocument();
    unmount();

    vi.spyOn(api, 'getArchiveProjectPage').mockResolvedValue({
      ...archiveCard,
      title: null,
      description: null,
      designer: null,
      profile_title: null,
      model_pictures: [],
      profile_pictures: [],
    } as never);
    render(<ModelCardModal source={{ kind: 'archive', id: 13 }} onClose={() => {}} />);
    expect(await screen.findByText('This print carries no model card.')).toBeInTheDocument();
  });

  it('is a modal dialog named by its heading, and hands the focus back on Escape', async () => {
    vi.spyOn(api, 'getArchiveProjectPage').mockResolvedValue(archiveCard as never);
    render(<Openable source={{ kind: 'archive', id: 12 }} />);

    const opener = screen.getByRole('button', { name: 'open card' });
    opener.focus();
    fireEvent.click(opener);

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleName(/model card/i);
    expect(dialog).toHaveFocus();

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
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
    // the ROUTE's WHOLE shape instead: `/api/v1/library/files/<id>/card-file/`
    // from the start of the url, which is the only thing `_card_route` builds.
    // Anchoring on the tail alone (`/files/<id>/card-file/`) still matched a
    // designer who nested `files/9/card-file/` inside their own folders.
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
          {
            name: 'parts.pdf',
            zip_path: 'Auxiliaries/Bill of Materials/files/9/card-file/parts.pdf',
            size: 1024,
            url: '/api/v1/library/files/3/card-download/Auxiliaries/Bill%20of%20Materials/files/9/card-file/parts.pdf',
          },
        ],
      },
    } as never);
    render(<ModelCardModal source={{ kind: 'file', id: 3 }} onClose={() => {}} />);

    expect(await screen.findByRole('button', { name: /bom\.xlsx/i })).toBeInTheDocument();
    expect(screen.queryByAltText('bom.xlsx')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /parts\.pdf/i })).toBeInTheDocument();
    expect(screen.queryByAltText('parts.pdf')).not.toBeInTheDocument();
  });

  it('renders a real card-file url as a picture whatever the member is called', async () => {
    // The other half of the anchor: a picture the server DID put on the token
    // surface must still be an `<img>`, however deep the designer's own folders
    // go and however the name is percent-encoded.
    vi.spyOn(api, 'getLibraryFileCard').mockResolvedValue({
      ...fileCard,
      auxiliaries: {
        pictures: [
          {
            name: 'a.png',
            zip_path: 'Auxiliaries/Model Pictures/a.png',
            size: 512,
            url: '/api/v1/library/files/12/card-file/Auxiliaries/Model%20Pictures/a.png',
          },
        ],
      },
    } as never);
    render(<ModelCardModal source={{ kind: 'file', id: 3 }} onClose={() => {}} />);

    const picture = await screen.findByAltText('a.png');
    expect(picture).toHaveAttribute('src', expect.stringContaining('/card-file/'));
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

  it('is a modal dialog named by its heading, and hands the focus back on Escape', async () => {
    vi.spyOn(api, 'getLibraryFileCard').mockResolvedValue(fileCard as never);
    render(<Openable source={{ kind: 'file', id: 3 }} />);

    const opener = screen.getByRole('button', { name: 'open card' });
    opener.focus();
    fireEvent.click(opener);

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleName(/model card/i);
    expect(dialog).toHaveFocus();

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it('announces the lightbox as a modal dialog of its own', async () => {
    vi.spyOn(api, 'getLibraryFileCard').mockResolvedValue(fileCard as never);
    render(<ModelCardModal source={{ kind: 'file', id: 3 }} onClose={() => {}} />);

    fireEvent.click((await screen.findByAltText('front.png')).closest('button') as HTMLButtonElement);

    const lightbox = screen.getByTestId('card-lightbox');
    expect(lightbox).toHaveAttribute('role', 'dialog');
    expect(lightbox).toHaveAttribute('aria-modal', 'true');
    expect(lightbox).toHaveAccessibleName('Picture viewer');
  });
});
