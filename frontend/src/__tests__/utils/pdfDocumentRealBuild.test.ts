/**
 * The real PDF.js LEGACY build, with the options the viewer opens every
 * document with, parses a small hand-written PDF — and names an encrypted or
 * broken one the way the viewer's messages expect.
 *
 * There is no Worker here, so PDF.js runs its worker code on this thread
 * ("fake worker"); the browser's worker wiring is the build's to prove, not
 * this test's.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { PDF_DOCUMENT_OPTIONS, classifyOpenError } from '../../utils/pdfDocument';

/** A one-page PDF, 200 × 100 pt, with its cross-reference table computed. */
function tinyPdf(extraTrailer = ''): Uint8Array {
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Contents 4 0 R >>',
    '<< /Length 20 >>\nstream\n0 0 m 200 100 l S  \nendstream',
  ];
  let body = '%PDF-1.4\n';
  const offsets: number[] = [];
  objects.forEach((object, i) => {
    offsets.push(body.length);
    body += `${i + 1} 0 obj\n${object}\nendobj\n`;
  });
  const xref = body.length;
  body += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (const offset of offsets) body += `${String(offset).padStart(10, '0')} 00000 n \n`;
  body += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R ${extraTrailer}>>\nstartxref\n${xref}\n%%EOF\n`;
  return new TextEncoder().encode(body);
}

describe('the PDF.js legacy build', () => {
  it('is the build the viewer imports, with its worker as a Vite worker chunk', () => {
    // The standard build calls APIs only the newest browsers have, and the
    // worker file copied as shipped keeps a class static block Safari 16.0
    // cannot parse; as a worker chunk it goes through our build.
    const source = readFileSync(resolve(__dirname, '../../utils/pdfDocument.ts'), 'utf8');
    expect(source).toContain("import('pdfjs-dist/legacy/build/pdf.mjs')");
    expect(source).toContain("import('pdfjs-dist/legacy/build/pdf.worker.min.mjs?worker')");
    expect(source).not.toContain("'pdfjs-dist/build/");
  });

  it('opens documents without eval, WebAssembly or fetched helper assets', () => {
    expect(PDF_DOCUMENT_OPTIONS).toMatchObject({ isEvalSupported: false, useWasm: false, useWorkerFetch: false });
    for (const key of ['cMapUrl', 'standardFontDataUrl', 'wasmUrl', 'iccUrl']) {
      expect(PDF_DOCUMENT_OPTIONS).not.toHaveProperty(key);
    }
  });

  it('parses a small PDF: one page of the size the file states', async () => {
    const pdfjs = await import('pdfjs-dist/legacy/build/pdf.mjs');
    const task = pdfjs.getDocument({ ...PDF_DOCUMENT_OPTIONS, data: tinyPdf() });
    const doc = await task.promise;
    try {
      expect(doc.numPages).toBe(1);
      const page = await doc.getPage(1);
      const viewport = page.getViewport({ scale: 1 });
      expect([viewport.width, viewport.height]).toEqual([200, 100]);
    } finally {
      await task.destroy();
    }
  });

  it('names a document that is not a PDF as broken', async () => {
    const pdfjs = await import('pdfjs-dist/legacy/build/pdf.mjs');
    const task = pdfjs.getDocument({ ...PDF_DOCUMENT_OPTIONS, data: new TextEncoder().encode('this is not a PDF at all') });
    const error = await task.promise.then(
      () => null,
      (e: unknown) => e,
    );
    await task.destroy();
    expect(error).not.toBeNull();
    expect(classifyOpenError(error)).toBe('broken');
  });

  it('names a document that asks for a password as encrypted', async () => {
    const pdfjs = await import('pdfjs-dist/legacy/build/pdf.mjs');
    // An /Encrypt dictionary with the standard handler: PDF.js asks for the password.
    const encrypt =
      '/Encrypt << /Filter /Standard /V 1 /R 2 /P -44 /O <' + '11'.repeat(32) + '> /U <' + '22'.repeat(32) + '> >> /ID [<00112233445566778899aabbccddeeff> <00112233445566778899aabbccddeeff>] ';
    const task = pdfjs.getDocument({ ...PDF_DOCUMENT_OPTIONS, data: tinyPdf(encrypt) });
    const error = await task.promise.then(
      () => null,
      (e: unknown) => e,
    );
    await task.destroy();
    expect(error).not.toBeNull();
    expect(classifyOpenError(error)).toBe('encrypted');
  });
});
