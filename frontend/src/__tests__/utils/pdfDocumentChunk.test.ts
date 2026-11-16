/**
 * The PDF.js chunk that does not load — a deploy replaced it, or the network
 * dropped — is the viewer's failure, named apart from a damaged document.
 */
import { describe, it, expect, vi } from 'vitest';

vi.mock('pdfjs-dist/legacy/build/pdf.mjs', () => {
  throw new TypeError('Failed to fetch dynamically imported module');
});
vi.mock('pdfjs-dist/legacy/build/pdf.worker.min.mjs?worker', () => ({ default: class {} }));

import { PdfOpenError, openPdf } from '../../utils/pdfDocument';

describe('openPdf without its PDF.js chunk', () => {
  it('reports the viewer, not the document', async () => {
    const error = await openPdf(new ArrayBuffer(8)).then(
      () => null,
      (e: unknown) => e,
    );

    expect(error).toBeInstanceOf(PdfOpenError);
    expect((error as PdfOpenError).reason).toBe('viewer');
  });
});
