/**
 * `X-Label-Warnings`: what the renderer had to say about a label, carried in a
 * response header beside the picture.
 *
 * A header holds Latin-1 only, so the server percent-encodes the sentences as
 * UTF-8. Every reader takes BOTH forms — the encoded one, and a value that was
 * sent as it is — and never fails on a value it cannot decode.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { api, parseLabelWarnings } from '../../api/client';

const FIRST = 'Текст «Матеріал» не вміщується у свою рамку.';
const SECOND = 'QR-код обрізано: 100% ширини.';
const ENCODED = encodeURIComponent(`${FIRST} | ${SECOND}`);
const RAW = `${FIRST} | ${SECOND}`;

/** A response as `fetch` hands it over, with the header's value exactly as given. */
function answer(header: string | null) {
  return {
    ok: true,
    status: 200,
    headers: { get: (name: string) => (name.toLowerCase() === 'x-label-warnings' ? header : null) },
    blob: async () => new Blob(['png']),
    json: async () => ({}),
  };
}

const readers: [string, () => Promise<{ warnings: string[] }>][] = [
  ['the spool-label picture (the dialog’s preview and Save as PNG)', () => api.renderSpoolLabelPictures({ spool_ids: [1], dpi: 300 } as never, false)],
  ['the sheet preview', () => api.previewLabelSheet({ sheet: {} as never, template_id: 1 })],
  ['the design preview', () => api.previewLabelTemplate({} as never)],
];

describe('parseLabelWarnings', () => {
  it('reads the percent-encoded form', () => {
    expect(parseLabelWarnings(ENCODED)).toEqual([FIRST, SECOND]);
  });

  it('reads a value that was sent as it is', () => {
    expect(parseLabelWarnings(RAW)).toEqual([FIRST, SECOND]);
    expect(parseLabelWarnings('Text does not fit its box. | The barcode was cut.')).toEqual([
      'Text does not fit its box.',
      'The barcode was cut.',
    ]);
  });

  it('keeps a value it cannot decode as it came, instead of failing', () => {
    expect(parseLabelWarnings('Scaled to 100% of the cell')).toEqual(['Scaled to 100% of the cell']);
    expect(parseLabelWarnings('%E0%A4%A')).toEqual(['%E0%A4%A']);
  });

  it('is empty without the header', () => {
    expect(parseLabelWarnings(null)).toEqual([]);
    expect(parseLabelWarnings('')).toEqual([]);
  });
});

describe.each(readers)('%s', (_name, read) => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reads Cyrillic warnings the server percent-encoded', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => answer(ENCODED)));

    expect((await read()).warnings).toEqual([FIRST, SECOND]);
  });

  it('reads Cyrillic warnings that arrive as they are', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => answer(RAW)));

    expect((await read()).warnings).toEqual([FIRST, SECOND]);
  });

  it('has none without the header', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => answer(null)));

    expect((await read()).warnings).toEqual([]);
  });
});
