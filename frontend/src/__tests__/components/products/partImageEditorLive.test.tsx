/**
 * The open editor reads the saved picture live (plan E5 r2, E5-R1; D15–D16): a render that finishes,
 * a new version, a pin that stops holding reach the open dialog through the product's own query —
 * which the server event and every write refetch — never by replacing the editing base: what was
 * typed, the chosen file and a touched draft survive. The tab's stored part is a snapshot; this is not.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { useEffect, useRef } from 'react';
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { render } from '../../utils';
import { api, ApiError } from '../../../api/client';
import type { PartImageChoice, PartImageRef, Permission, Product, ProductPart } from '../../../api/client';
import { CompositionTab } from '../../../components/products/detail/CompositionTab';
import { PART_IMAGE_VIEW_KEYS } from '../../../utils/queryInvalidation';

vi.mock('../../../contexts/AuthContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../contexts/AuthContext')>();
  return {
    ...actual,
    useAuth: () => {
      const real = actual.useAuth();
      return { ...real, hasPermission: (_p: Permission) => true };
    },
  };
});

const AUTO: PartImageChoice = { source: 'auto', instance: null, pin: null, photo: null };
const PHOTO: PartImageChoice = { source: 'photo', instance: null, pin: null, photo: { present: true } };
const PENDING: PartImageRef = { kind: 'render', status: 'pending', v: null };
const ready = (v: string): PartImageRef => ({ kind: 'render', status: 'ready', v });
const photoFile = new File(['png'], 'body.png', { type: 'image/png' });

function piece(over: Partial<ProductPart> & Pick<ProductPart, 'id' | 'name'>): ProductPart {
  return {
    kind: 'printed',
    name_key: `${over.name.toLowerCase()}.stl`,
    qty_per_unit: 1,
    aliases: [`${over.name.toLowerCase()}.stl`],
    auto: false,
    unit_price: null,
    sourcing_url: null,
    remarks: null,
    sort_order: over.id,
    ignored: false,
    stock_balance: 0,
    variant_option_ids: [],
    image: PENDING,
    image_choice: AUTO,
    ...over,
  };
}
const productOf = (parts: ProductPart[]) =>
  ({ id: 7, code: 'PR-0007', name: 'Flask', parts, variant_groups: [] }) as unknown as Product;

const handle = vi.hoisted(() => ({ client: null as QueryClient | null }));
let answer: Product;

function Host() {
  const qc = useQueryClient();
  useEffect(() => {
    handle.client = qc;
  }, [qc]);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const query = useQuery({ queryKey: ['product', 7], queryFn: () => api.getProduct(7) });
  return query.data ? <CompositionTab product={query.data} headingRef={headingRef} /> : null;
}

/** What the socket handler does for `part_images_changed` (E4). */
async function pictureEvent() {
  const client = handle.client as QueryClient;
  await act(async () => {
    for (const key of PART_IMAGE_VIEW_KEYS) await client.invalidateQueries({ queryKey: [...key] });
  });
}

async function openEditor(partId: number) {
  fireEvent.click(within(await screen.findByTestId(`part-${partId}-row`)).getByRole('button', { name: 'Actions' }));
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Edit' }));
  return screen.findByRole('dialog', { name: 'Edit part' });
}

const section = () => screen.getByTestId('part-image-section');
const savedThumb = () => within(section()).getAllByTestId('part-thumb')[0];
const save = () => screen.getByRole('button', { name: /^(save part|saving…)$/i });

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, 'getUiPreferences').mockResolvedValue({ currency: 'EUR' } as never);
  vi.spyOn(api, 'getProductSources').mockResolvedValue({ parts: [] } as never);
  vi.spyOn(api, 'getProduct').mockImplementation(async () => answer);
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:x'), revokeObjectURL: vi.fn() }));
});

describe('an open editor and the server’s picture', () => {
  it('a finished render reaches it; the typed name and the chosen photo stay', async () => {
    answer = productOf([piece({ id: 1, name: 'Body' })]);
    render(<Host />);
    const dialog = await openEditor(1);
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Unsaved name' } });
    fireEvent.change(within(dialog).getByLabelText('Choose a photo'), { target: { files: [photoFile] } });
    answer = productOf([piece({ id: 1, name: 'Body', image: ready('v2') })]);
    await pictureEvent();
    await waitFor(() => expect(savedThumb()).toHaveAttribute('data-state', 'ready'));
    expect(within(savedThumb()).getByTestId('part-thumb-img').getAttribute('src')).toContain('v=v2');
    expect(within(dialog).getByLabelText('Name')).toHaveValue('Unsaved name');
    expect(within(dialog).getByRole('radio', { name: 'Photo' })).toBeChecked();
    expect(within(dialog).getByText('Chosen: body.png')).toBeInTheDocument();
  });

  it('a new version shows, and a render that failed leaves no picture', async () => {
    answer = productOf([piece({ id: 1, name: 'Body', image: ready('v1') })]);
    render(<Host />);
    await openEditor(1);
    answer = productOf([piece({ id: 1, name: 'Body', image: ready('v2') })]);
    await pictureEvent();
    await waitFor(() => expect(within(savedThumb()).getByTestId('part-thumb-img').getAttribute('src')).toContain('v=v2'));
    answer = productOf([piece({ id: 1, name: 'Body', image: null })]);
    await pictureEvent();
    await waitFor(() => expect(savedThumb()).toHaveAttribute('data-state', 'none'));
    expect(within(section()).getByText('No picture yet')).toBeInTheDocument();
  });

  it('a pin that stops holding says why in the open editor; the untouched pin is still what Save keeps', async () => {
    const key = { library_file_id: 9, plate_index: 1, identify_id: 11 };
    const pin = (valid: boolean): PartImageChoice => ({
      source: 'instance',
      instance: key,
      pin: { valid, reason: valid ? null : 'object_gone' },
      photo: null,
    });
    answer = productOf([piece({ id: 1, name: 'Body', image: ready('v1'), image_choice: pin(true) })]);
    const set = vi.spyOn(api, 'setPartImage');
    render(<Host />);
    await openEditor(1);
    expect(within(section()).queryByTestId('part-image-pin-invalid')).not.toBeInTheDocument();
    answer = productOf([piece({ id: 1, name: 'Body', image: PENDING, image_choice: pin(false) })]);
    await pictureEvent();
    expect(await within(section()).findByTestId('part-image-pin-invalid')).toHaveTextContent('the object is gone from the plate');
    fireEvent.click(save());
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Edit part' })).not.toBeInTheDocument());
    expect(set).not.toHaveBeenCalled();
  });

  it('an untouched draft follows the saved choice — Save never reverts another client’s photo (D16)', async () => {
    answer = productOf([piece({ id: 1, name: 'Body' })]);
    const update = vi.spyOn(api, 'updateProductPart').mockImplementation(async () => answer.parts[0] as never);
    const set = vi.spyOn(api, 'setPartImage');
    const upload = vi.spyOn(api, 'uploadPartPhoto');
    render(<Host />);
    const dialog = await openEditor(1);
    expect(within(dialog).getByRole('radio', { name: /Automatic/ })).toBeChecked();
    // somebody else uploads a photo meanwhile
    answer = productOf([piece({ id: 1, name: 'Body', image: { kind: 'photo', status: 'ready', v: 'p.png' }, image_choice: PHOTO })]);
    await pictureEvent();
    await waitFor(() => expect(within(dialog).getByRole('radio', { name: 'Photo' })).toBeChecked());
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Body 2' } });
    fireEvent.click(save());
    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    expect(set).not.toHaveBeenCalled();
    expect(upload).not.toHaveBeenCalled();
  });

  it('a touched draft is the operator’s: a refresh does not move it, and Save sends it (D16)', async () => {
    answer = productOf([piece({ id: 1, name: 'Body', image: { kind: 'photo', status: 'ready', v: 'p1.png' }, image_choice: PHOTO })]);
    const set = vi.spyOn(api, 'setPartImage').mockImplementation(async () => answer.parts[0] as never);
    render(<Host />);
    const dialog = await openEditor(1);
    fireEvent.click(within(dialog).getByRole('radio', { name: /Automatic/ }));
    answer = productOf([piece({ id: 1, name: 'Body', image: { kind: 'photo', status: 'ready', v: 'p2.png' }, image_choice: PHOTO })]);
    await pictureEvent();
    await waitFor(() => expect(within(savedThumb()).getByTestId('part-thumb-img').getAttribute('src')).toContain('v=p2.png'));
    expect(within(dialog).getByRole('radio', { name: /Automatic/ })).toBeChecked();
    fireEvent.click(save());
    await waitFor(() => expect(set).toHaveBeenCalledWith(1, { source: 'auto' }));
  });

  it('a new part whose photo was refused sees its render arrive; the retry is the photo alone', async () => {
    answer = productOf([piece({ id: 1, name: 'Body' })]);
    const gear = piece({ id: 9, name: 'Gear' });
    const create = vi.spyOn(api, 'createProductPart').mockImplementation(async () => {
      answer = productOf([piece({ id: 1, name: 'Body' }), gear]); // what the refetch after the create reads
      return gear as never;
    });
    const upload = vi
      .spyOn(api, 'uploadPartPhoto')
      .mockRejectedValueOnce(new ApiError('The photo must be a PNG, JPEG or WebP picture', 422))
      .mockResolvedValueOnce({ ...gear, image: { kind: 'photo', status: 'ready', v: 'g.png' } } as never);
    render(<Host />);
    fireEvent.click(await screen.findByRole('button', { name: 'Add part' }));
    const dialog = await screen.findByRole('dialog', { name: 'New part' });
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Gear' } });
    fireEvent.change(within(dialog).getByLabelText('Choose a photo'), { target: { files: [photoFile] } });
    fireEvent.click(save());
    expect(await screen.findByRole('alert')).toHaveTextContent('The part is saved; its picture is not');
    answer = productOf([piece({ id: 1, name: 'Body' }), { ...gear, image: ready('g9') }]);
    await pictureEvent();
    await waitFor(() => expect(savedThumb()).toHaveAttribute('data-state', 'ready'));
    expect(within(screen.getByRole('dialog', { name: 'Edit part' })).getByText('Chosen: body.png')).toBeInTheDocument();
    fireEvent.click(save());
    await waitFor(() => expect(upload).toHaveBeenCalledTimes(2));
    expect(create).toHaveBeenCalledTimes(1);
    expect(upload.mock.calls[1]).toEqual([9, photoFile]);
  });
});
