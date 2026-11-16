/**
 * The one place that talks to PDF.js: a document's bytes in, a small handle
 * out — page count, page sizes, "draw page N on this canvas".
 *
 * ⚠️ The LEGACY build, and its worker as a Vite worker chunk. The standard
 * build calls APIs only the newest browsers have, and the worker file copied
 * as it ships carries a class static block Safari 16.0–16.3 cannot parse;
 * imported through `?worker`, it goes through our build like any other chunk
 * and is served from our own origin (`script-src 'self'` covers it).
 *
 * ⚠️ Nothing here may need a looser Content-Security-Policy. PDF.js is told
 * to use neither eval nor WebAssembly and is given no URL to fetch helper
 * assets from, so a document comes down to the bytes we hand it:
 *  - no CMaps: text in a font that relies on a predefined CJK CMap is not drawn;
 *  - no standard-font files: a document that does not embed Helvetica / Times /
 *    Courier is drawn with the system's look-alike;
 *  - no WebAssembly decoders: a JPEG 2000 or JBIG2 image inside a page is left
 *    out, the rest of the page is drawn.
 *
 * Both imports are dynamic: PDF.js is a megabyte nobody pays for until a PDF
 * is opened.
 */

export interface PdfPageSize {
  width: number;
  height: number;
}

export interface PdfRender {
  /** Settles when the page is drawn — or when the drawing was cancelled. */
  promise: Promise<void>;
  cancel: () => void;
}

export interface PdfHandle {
  pageCount: number;
  /** Page 1 at scale 1 — the size every page is assumed to have until it is measured. */
  firstPageSize: PdfPageSize;
  /** A page's size at scale 1, in CSS pixels. */
  pageSize: (page: number) => Promise<PdfPageSize>;
  /** Draw `page` on `canvas` at `scale` canvas pixels per PDF point; the canvas is sized here. */
  render: (page: number, canvas: HTMLCanvasElement, scale: number) => PdfRender;
  /** Free the document and stop its worker. */
  destroy: () => Promise<void>;
}

/** `viewer` — PDF.js itself could not be loaded; the document was never looked at. */
export type PdfOpenFailure = 'encrypted' | 'broken' | 'viewer';

export class PdfOpenError extends Error {
  readonly reason: PdfOpenFailure;
  constructor(reason: PdfOpenFailure) {
    super(`pdf_${reason}`);
    this.name = 'PdfOpenError';
    this.reason = reason;
  }
}

/**
 * What every document is opened with.
 *
 * `isEvalSupported` is an option of PDF.js up to 4.x; later versions have no
 * eval path at all. It stays here so that a change of version can never turn
 * one on.
 */
export const PDF_DOCUMENT_OPTIONS = {
  isEvalSupported: false,
  useWasm: false,
  useWorkerFetch: false,
  enableXfa: false,
} as const;

/** Which of the viewer's two messages an error from PDF.js means. */
export function classifyOpenError(error: unknown): 'encrypted' | 'broken' {
  return (error as { name?: unknown } | null)?.name === 'PasswordException' ? 'encrypted' : 'broken';
}

/** PDF.js and its worker chunk. A chunk that does not load (a deploy replaced it, the network dropped) is the viewer's failure, not the document's. */
async function loadPdfJs() {
  try {
    return await Promise.all([
      import('pdfjs-dist/legacy/build/pdf.mjs'),
      import('pdfjs-dist/legacy/build/pdf.worker.min.mjs?worker'),
    ]);
  } catch {
    throw new PdfOpenError('viewer');
  }
}

const givenUp = () => new DOMException('The open was given up', 'AbortError');

/**
 * Open a document. `signal` gives the open up: the caller is gone (the viewer
 * closed, or shows another file) and the promise rejects with an `AbortError`.
 *
 * ⚠️ Giving up ends the worker's THREAD at once, without waiting for PDF.js's
 * own teardown: that teardown asks the worker to finish, and a worker hung in
 * the parser — the very case a caller gives up on — never answers.
 */
export async function openPdf(data: ArrayBuffer, signal?: AbortSignal): Promise<PdfHandle> {
  const [pdfjs, workerModule] = await loadPdfJs();
  if (signal?.aborted) throw givenUp();
  // A worker of this document's own: closing the viewer ends it, and a
  // document that hangs the parser takes nothing else with it.
  const port = new workerModule.default();
  const worker = pdfjs.PDFWorker.create({ port });
  const task = pdfjs.getDocument({ ...PDF_DOCUMENT_OPTIONS, data, worker });
  let released = false;
  const release = async () => {
    if (released) return;
    released = true;
    try {
      await task.destroy();
    } catch {
      /* already gone */
    }
    worker.destroy();
    port.terminate();
  };
  /** The thread first, PDF.js's teardown unwaited. */
  const abandon = () => {
    if (released) return;
    released = true;
    void Promise.resolve(task.destroy()).catch(() => {});
    worker.destroy();
    port.terminate();
  };
  let onAbort: () => void = () => {};
  const aborted = new Promise<never>((_, reject) => {
    onAbort = () => {
      abandon();
      reject(givenUp());
    };
    signal?.addEventListener('abort', onAbort, { once: true });
  });

  try {
    const opening = (async () => {
      const opened = await task.promise;
      const viewport = (await opened.getPage(1)).getViewport({ scale: 1 });
      return { opened, firstPageSize: { width: viewport.width, height: viewport.height } };
    })();
    // After a give-up nobody waits for it any more.
    opening.catch(() => {});
    const { opened: doc, firstPageSize } = await Promise.race([opening, aborted]);
    const sizeOf = async (page: number): Promise<PdfPageSize> => {
      const viewport = (await doc.getPage(page)).getViewport({ scale: 1 });
      return { width: viewport.width, height: viewport.height };
    };
    return {
      pageCount: doc.numPages,
      firstPageSize,
      pageSize: sizeOf,
      render: (pageNumber, canvas, scale) => {
        let cancelled = false;
        let renderTask: { cancel: () => void; promise: Promise<unknown> } | null = null;
        const promise = (async () => {
          const page = await doc.getPage(pageNumber);
          if (cancelled) return;
          const viewport = page.getViewport({ scale });
          canvas.width = Math.max(1, Math.floor(viewport.width));
          canvas.height = Math.max(1, Math.floor(viewport.height));
          renderTask = page.render({ canvas, viewport });
          try {
            await renderTask.promise;
          } catch (error) {
            if ((error as { name?: unknown } | null)?.name !== 'RenderingCancelledException') throw error;
          }
        })();
        return {
          promise,
          cancel: () => {
            cancelled = true;
            renderTask?.cancel();
          },
        };
      },
      destroy: release,
    };
  } catch (error) {
    if (signal?.aborted) throw givenUp();
    await release();
    throw new PdfOpenError(classifyOpenError(error));
  } finally {
    signal?.removeEventListener('abort', onAbort);
  }
}
