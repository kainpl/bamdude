/**
 * The editor's «Picture» section (spec part-thumbnails §12.4; plan E5, D8–D10, D14): it holds a draft
 * and sends nothing; the saved state stands apart from the draft; the gallery offers exactly what the
 * server would pin; a re-render is the one immediate action, and it does not touch the part.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { useState } from 'react';
import { render } from '../../utils';
import { api, ApiError } from '../../../api/client';
import type { PartImageCandidate, ProductPart, ProductPartKind } from '../../../api/client';
import { PartImageSection } from '../../../components/products/PartImageSection';
import type { PartImageDraft } from '../../../components/products/partImageDraft';

function savedPart(over: Partial<ProductPart> = {}): ProductPart {
  return {
    id: 3,
    kind: 'printed',
    name: 'Body',
    name_key: 'body.stl',
    qty_per_unit: 1,
    aliases: ['body.stl'],
    auto: false,
    unit_price: null,
    sourcing_url: null,
    remarks: null,
    sort_order: 3,
    ignored: false,
    stock_balance: 0,
    variant_option_ids: [],
    image: { kind: 'render', status: 'ready', v: 'r3' },
    image_choice: { source: 'auto', instance: null, pin: null, photo: null },
    ...over,
  };
}

function candidate(over: Partial<PartImageCandidate>): PartImageCandidate {
  return {
    library_file_id: 9,
    filename: 'flask.gcode.3mf',
    hidden: false,
    plate_index: 1,
    identify_id: 11,
    method: 'toolpath',
    reason: null,
    plate_status: 'ready',
    v: 'v11',
    pinned: false,
    ...over,
  };
}

/** The section is controlled: a host keeps its draft and reports every change. */
function Host({
  part,
  kind = 'printed',
  initial = { source: 'auto' },
  onDraft = () => {},
  canRerender = true,
}: {
  part: ProductPart | null;
  kind?: ProductPartKind;
  initial?: PartImageDraft;
  onDraft?: (draft: PartImageDraft) => void;
  canRerender?: boolean;
}) {
  const [draft, setDraft] = useState<PartImageDraft>(initial);
  return (
    <PartImageSection
      productId={7}
      partId={part?.id ?? null}
      image={part?.image}
      choice={part?.image_choice}
      kind={kind}
      name="Body"
      draft={draft}
      onChange={(next) => {
        setDraft(next);
        onDraft(next);
      }}
      disabled={false}
      photoInputId="photo-input"
      canRerender={canRerender}
    />
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  let n = 0;
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => `blob:preview-${++n}`), revokeObjectURL: vi.fn() }));
});
afterEach(() => vi.unstubAllGlobals());

describe('a new part', () => {
  it('offers automatic and a photo; the plate objects wait for the saved part', () => {
    const candidates = vi.spyOn(api, 'getPartImageCandidates');
    render(<Host part={null} />);
    expect(screen.getByRole('radio', { name: /Automatic/ })).toBeChecked();
    expect(screen.getByText(/can be chosen once the part is saved/)).toBeInTheDocument();
    expect(candidates).not.toHaveBeenCalled();
  });

  it('a chosen photo becomes the draft with a local preview — nothing is sent', () => {
    const upload = vi.spyOn(api, 'uploadPartPhoto');
    const onDraft = vi.fn();
    render(<Host part={null} onDraft={onDraft} />);
    const file = new File(['x'], 'body.png', { type: 'image/png' });
    fireEvent.change(screen.getByLabelText('Choose a photo'), { target: { files: [file] } });
    expect(onDraft).toHaveBeenLastCalledWith({ source: 'photo', file });
    expect(screen.getByRole('radio', { name: 'Photo' })).toBeChecked();
    expect(screen.getByTestId('part-photo-preview')).toHaveAttribute('src', 'blob:preview-1');
    expect(screen.getByText('Chosen: body.png')).toBeInTheDocument();
    expect(upload).not.toHaveBeenCalled();
  });

  it('a purchased part has no plate objects to pin (D8)', () => {
    render(<Host part={null} kind="purchased" />);
    expect(screen.queryByText('An object of a plate')).not.toBeInTheDocument();
    expect(screen.getByRole('radio', { name: 'Photo' })).toBeInTheDocument();
  });
});

describe('the gallery of a saved printed part (D8, D9)', () => {
  it('groups the objects by file and plate; only a ready one can be chosen', async () => {
    vi.spyOn(api, 'getPartImageCandidates').mockResolvedValue([
      candidate({ identify_id: 11 }),
      candidate({ identify_id: 12, method: 'skipped', reason: 'over_cap', v: null }),
      candidate({ identify_id: 13, method: 'missing', reason: 'empty_render', v: null }),
      candidate({ library_file_id: 10, filename: null, hidden: true, plate_index: 2, identify_id: 21, v: 'v21' }),
      candidate({ library_file_id: 10, filename: null, hidden: true, plate_index: 3, identify_id: 31, method: null, plate_status: 'pending', v: null }),
    ]);
    render(<Host part={savedPart()} />);
    const plate = await screen.findByTestId('part-image-plate-9:1');
    expect(within(plate).getByText('flask.gcode.3mf · Plate 1')).toBeInTheDocument();
    const ready = within(screen.getByTestId('part-image-candidate-11')).getByRole('radio');
    expect(ready).toHaveAccessibleName('flask.gcode.3mf · Plate 1 · Object #11');
    // skipped and missing: no button to choose them, and why
    const skipped = screen.getByTestId('part-image-candidate-12');
    expect(within(skipped).queryByRole('radio')).not.toBeInTheDocument();
    expect(within(skipped).getByText('Skipped — too many objects on the plate')).toBeInTheDocument();
    expect(within(screen.getByTestId('part-image-candidate-13')).getByText('No picture')).toBeInTheDocument();
    // a file the reader may not open: named so — its object's picture is still shown
    const hidden = screen.getByTestId('part-image-plate-10:2');
    expect(within(hidden).getByText('A file you cannot open · Plate 2')).toBeInTheDocument();
    expect(within(screen.getByTestId('part-image-candidate-21')).getByTestId('part-thumb-img').getAttribute('src')).toContain(
      '/api/v1/products/7/files/10/plates/2/objects/21/image?v=v21',
    );
    expect(within(screen.getByTestId('part-image-candidate-31')).getByText('Being prepared')).toBeInTheDocument();
  });

  it('choosing an object is a draft, not a request', async () => {
    vi.spyOn(api, 'getPartImageCandidates').mockResolvedValue([candidate({ identify_id: 11 })]);
    const set = vi.spyOn(api, 'setPartImage');
    const onDraft = vi.fn();
    render(<Host part={savedPart()} onDraft={onDraft} />);
    fireEvent.click(await screen.findByRole('radio', { name: /Object #11/ }));
    expect(onDraft).toHaveBeenLastCalledWith({
      source: 'instance',
      instance: { library_file_id: 9, plate_index: 1, identify_id: 11 },
    });
    expect(set).not.toHaveBeenCalled();
  });

  it('the pinned object is checked and says so', async () => {
    vi.spyOn(api, 'getPartImageCandidates').mockResolvedValue([candidate({ identify_id: 11, pinned: true })]);
    const key = { library_file_id: 9, plate_index: 1, identify_id: 11 };
    render(
      <Host
        part={savedPart({ image_choice: { source: 'instance', instance: key, pin: { valid: true, reason: null }, photo: null } })}
        initial={{ source: 'instance', instance: key }}
      />,
    );
    expect(await screen.findByRole('radio', { name: /Object #11/ })).toBeChecked();
    expect(screen.getByText('pinned')).toBeInTheDocument();
  });

  it('a failed load says so and retries', async () => {
    const get = vi
      .spyOn(api, 'getPartImageCandidates')
      .mockRejectedValueOnce(new ApiError('boom', 500))
      .mockResolvedValueOnce([candidate({})]);
    render(<Host part={savedPart()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' })); // LoadFailedNote: common.retry
    expect(await screen.findByRole('radio', { name: /Object #11/ })).toBeInTheDocument();
    expect(get).toHaveBeenCalledTimes(2);
  });
});

describe('the saved state, apart from the draft', () => {
  it('an invalid pin says why, and that automatic is in effect', () => {
    const key = { library_file_id: 9, plate_index: 1, identify_id: 11 };
    render(
      <Host
        part={savedPart({
          image_choice: { source: 'instance', instance: key, pin: { valid: false, reason: 'object_gone' }, photo: null },
        })}
        initial={{ source: 'instance', instance: key }}
      />,
    );
    expect(screen.getByText('Saved: plate object #11')).toBeInTheDocument();
    expect(screen.getByTestId('part-image-pin-invalid')).toHaveTextContent(
      'The pinned object no longer holds (the object is gone from the plate) — automatic is in effect.',
    );
  });

  it('a photo gone from the disk says so', () => {
    render(
      <Host
        part={savedPart({ image: null, image_choice: { source: 'photo', instance: null, pin: null, photo: { present: false } } })}
        initial={{ source: 'photo', file: null }}
      />,
    );
    expect(screen.getByText('The photo is gone from the disk — automatic is in effect.')).toBeInTheDocument();
    expect(screen.getByText('The saved photo stays')).toBeInTheDocument();
  });
});

describe('the plates and «Re-render» (D10)', () => {
  it('counts the plates and queues them all, without touching the part', async () => {
    vi.spyOn(api, 'getPartImageCandidates').mockResolvedValue([
      candidate({ identify_id: 11 }),
      candidate({ identify_id: 12 }),
      candidate({ library_file_id: 10, plate_index: 2, identify_id: 21, method: null, plate_status: 'pending', v: null }),
      candidate({ library_file_id: 11, plate_index: 1, identify_id: 31, method: null, plate_status: 'failed', v: null }),
    ]);
    const rerender = vi.spyOn(api, 'rerenderPartImages').mockResolvedValue({ queued: 3 });
    const update = vi.spyOn(api, 'updateProductPart');
    render(<Host part={savedPart()} />);
    expect(await screen.findByTestId('part-image-plates')).toHaveTextContent(
      'Plates: 1 ready · 1 queued · 1 without a picture',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Re-render' }));
    await waitFor(() => expect(rerender).toHaveBeenCalledWith(7, true));
    expect(await screen.findByText('Plates queued for re-render: 3')).toBeInTheDocument();
    expect(update).not.toHaveBeenCalled();
  });

  it('is shut without the right to change the product', async () => {
    vi.spyOn(api, 'getPartImageCandidates').mockResolvedValue([candidate({})]);
    render(<Host part={savedPart()} canRerender={false} />);
    expect(await screen.findByRole('button', { name: 'Re-render' })).toBeDisabled();
  });
});

describe('the local preview (Review Focus 5)', () => {
  it('revokes every address it made: on a new file and when it leaves', () => {
    const { unmount } = render(<Host part={null} />);
    const input = screen.getByLabelText('Choose a photo');
    fireEvent.change(input, { target: { files: [new File(['a'], 'a.png', { type: 'image/png' })] } });
    fireEvent.change(input, { target: { files: [new File(['b'], 'b.png', { type: 'image/png' })] } });
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:preview-1');
    unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:preview-2');
  });
});

describe('after the browser check (plan E5, Task 41)', () => {
  it('the native file field hides behind its own button — it never says «no file chosen» beside the chosen one', () => {
    render(<Host part={null} />);
    expect(screen.getByLabelText('Choose a photo')).toHaveClass('sr-only');
    expect(screen.getByText('Choose a photo', { selector: 'label' })).toHaveAttribute('for', 'photo-input');
  });

  it('a new part has nothing saved to describe', () => {
    render(<Host part={null} />);
    expect(screen.queryByText(/^Saved:/)).not.toBeInTheDocument();
    expect(screen.getByText('No picture yet')).toBeInTheDocument();
  });
});

describe('after the final review (plan E5)', () => {
  it('a queued picture says it is being prepared, never «Shown now» (review 1)', () => {
    render(<Host part={savedPart({ image: { kind: 'render', status: 'pending', v: null } })} />);
    expect(screen.getByText('Being prepared')).toBeInTheDocument();
    expect(screen.queryByText('Shown now')).not.toBeInTheDocument();
  });

  it('the chosen file survives a trip to another option (review 3)', () => {
    const onDraft = vi.fn();
    render(<Host part={null} onDraft={onDraft} />);
    const file = new File(['a'], 'a.png', { type: 'image/png' });
    fireEvent.change(screen.getByLabelText('Choose a photo'), { target: { files: [file] } });
    fireEvent.click(screen.getByRole('radio', { name: /Automatic/ }));
    fireEvent.click(screen.getByRole('radio', { name: 'Photo' }));
    expect(onDraft).toHaveBeenLastCalledWith({ source: 'photo', file });
    expect(screen.getByText('Chosen: a.png')).toBeInTheDocument();
  });

  it('the photo button shows any focus of its field — the dialog moves it there on a refusal (review 6)', () => {
    render(<Host part={null} />);
    expect(screen.getByText('Choose a photo', { selector: 'label' })).toHaveClass('peer-focus:ring-2');
  });
});
