/**
 * `openPdf` around a PDF.js replaced at its two imports: what it does with
 * the loading task, the worker and the worker's thread when the open fails,
 * hangs or is given up.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const pdfjs = vi.hoisted(() => ({
  /** Set by a test: what `getDocument` hands back. */
  task: null as null | { promise: Promise<unknown>; destroy: ReturnType<typeof vi.fn> },
  workerDestroy: vi.fn(),
  terminate: vi.fn(),
}));

vi.mock('pdfjs-dist/legacy/build/pdf.mjs', () => ({
  getDocument: () => pdfjs.task,
  PDFWorker: { create: () => ({ destroy: pdfjs.workerDestroy }) },
}));
vi.mock('pdfjs-dist/legacy/build/pdf.worker.min.mjs?worker', () => ({
  default: class {
    terminate = pdfjs.terminate;
  },
}));

import { PdfOpenError, openPdf } from '../../utils/pdfDocument';

/**
 * A document the parser hangs on: the task never settles, and its own
 * `destroy` never answers either — it waits for a worker that is busy for good.
 */
function hangingTask() {
  return { promise: new Promise<unknown>(() => {}), destroy: vi.fn(() => new Promise<void>(() => {})) };
}

/** Until `openPdf` has loaded PDF.js and is waiting for the document. */
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('openPdf', () => {
  beforeEach(() => {
    pdfjs.task = null;
    pdfjs.workerDestroy.mockClear();
    pdfjs.terminate.mockClear();
  });

  it('frees the task, the worker and its thread when the document is refused', async () => {
    const destroy = vi.fn(async () => {});
    pdfjs.task = { promise: Promise.reject(Object.assign(new Error('bad'), { name: 'InvalidPDFException' })), destroy };

    const error = await openPdf(new ArrayBuffer(8)).then(
      () => null,
      (e: unknown) => e,
    );

    expect(error).toBeInstanceOf(PdfOpenError);
    expect((error as PdfOpenError).reason).toBe('broken');
    expect(destroy).toHaveBeenCalled();
    expect(pdfjs.workerDestroy).toHaveBeenCalledTimes(1);
    expect(pdfjs.terminate).toHaveBeenCalledTimes(1);
  });

  it('ends the worker of a document that never opens as soon as the open is given up', async () => {
    const task = hangingTask();
    pdfjs.task = task;
    const abort = new AbortController();

    const opening = openPdf(new ArrayBuffer(8), abort.signal);
    const outcome = opening.then(
      () => 'opened',
      (e: unknown) => (e as { name?: string }).name,
    );
    await flush();
    expect(pdfjs.terminate).not.toHaveBeenCalled();

    abort.abort();

    // Without waiting for the task's own destroy, which never answers here.
    expect(task.destroy).toHaveBeenCalledTimes(1);
    expect(pdfjs.workerDestroy).toHaveBeenCalledTimes(1);
    expect(pdfjs.terminate).toHaveBeenCalledTimes(1);
    expect(await outcome).toBe('AbortError');
  });

  it('starts no worker for an open that was given up before PDF.js loaded', async () => {
    pdfjs.task = hangingTask();
    const abort = new AbortController();
    abort.abort();

    await expect(openPdf(new ArrayBuffer(8), abort.signal)).rejects.toMatchObject({ name: 'AbortError' });
    expect(pdfjs.workerDestroy).not.toHaveBeenCalled();
    expect(pdfjs.terminate).not.toHaveBeenCalled();
  });
});
