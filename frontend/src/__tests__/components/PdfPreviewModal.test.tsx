/**
 * The PDF viewer: pages of a document in the preview shell, with page
 * navigation and zoom.
 *
 * PDF.js is replaced at its one boundary, `utils/pdfDocument` — the viewer
 * sees a handle that counts pages, measures them and "draws" them — and the
 * bytes come from the library's own download route through `api`.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { MAX_PDF_BYTES, PdfPreviewModal } from '../../components/PdfPreviewModal';
import { PdfOpenError, type PdfHandle } from '../../utils/pdfDocument';
import i18n from '../../i18n';
import { ApiError, api } from '../../api/client';

const openPdf = vi.hoisted(() => vi.fn());
vi.mock('../../utils/pdfDocument', async () => {
  const actual = await vi.importActual<typeof import('../../utils/pdfDocument')>('../../utils/pdfDocument');
  return { ...actual, openPdf };
});

/** `odd` — pages whose size is not the first page's 600 × 800. */
function fakeHandle(pageCount: number, odd: Record<number, { width: number; height: number }> = {}) {
  const rendered: { page: number; scale: number }[] = [];
  const cancelled: number[] = [];
  const handle: PdfHandle = {
    pageCount,
    firstPageSize: { width: 600, height: 800 },
    pageSize: vi.fn(async (page: number) => odd[page] ?? { width: 600, height: 800 }),
    render: vi.fn((page: number, _canvas: HTMLCanvasElement, scale: number) => {
      rendered.push({ page, scale });
      return { promise: Promise.resolve(), cancel: () => cancelled.push(page) };
    }),
    destroy: vi.fn(async () => {}),
  };
  return { handle, rendered, cancelled };
}

function show(props: Partial<Parameters<typeof PdfPreviewModal>[0]> = {}) {
  const onClose = vi.fn();
  const onDownload = vi.fn();
  render(
    <PdfPreviewModal fileId={7} title="manual.pdf" fileSize={4096} onClose={onClose} onDownload={onDownload} {...props} />,
  );
  return { onClose, onDownload };
}

const pageBox = () => screen.getByRole('spinbutton', { name: 'Page' }) as HTMLInputElement;
const stage = () => screen.getByTestId('pdf-stage');
const renderedPages = (rendered: { page: number }[]) => [...new Set(rendered.map((r) => r.page))].sort((a, b) => a - b);

/** The stage is 632 × 832: an A-ish 600 × 800 page fits it at scale 1 with the 16 px padding. */
function stubGeometry() {
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(function (this: HTMLElement) {
    return this.dataset.testid === 'pdf-stage' ? 632 : 0;
  });
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function (this: HTMLElement) {
    return this.dataset.testid === 'pdf-stage' ? 832 : 0;
  });
}

describe('PdfPreviewModal', () => {
  let getBytes: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    stubGeometry();
    openPdf.mockReset();
    getBytes = vi.spyOn(api, 'getLibraryFileBytes').mockResolvedValue(new ArrayBuffer(16));
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('fetches the file through the authenticated download and opens it as a dialog named after the file', async () => {
    const { handle } = fakeHandle(3);
    openPdf.mockResolvedValue(handle);
    show();

    expect(screen.getByRole('dialog', { name: 'manual.pdf' })).toBeInTheDocument();
    await waitFor(() => expect(pageBox().value).toBe('1'));
    expect(getBytes).toHaveBeenCalledWith(7, expect.anything());
    expect(openPdf).toHaveBeenCalledTimes(1);
    expect(openPdf.mock.calls[0][0]).toBeInstanceOf(ArrayBuffer);
    expect(screen.getByText('/ 3')).toBeInTheDocument();
  });

  it('draws only the pages on screen, fitted to the width', async () => {
    const { handle, rendered } = fakeHandle(40);
    openPdf.mockResolvedValue(handle);
    show();

    await waitFor(() => expect(rendered.length).toBeGreaterThan(0));
    // One screen holds page 1 and the top of page 2; one more is drawn ahead.
    expect(renderedPages(rendered)).toEqual([1, 2, 3]);
    expect(rendered[0].scale).toBeCloseTo(1);
    expect(stage().querySelectorAll('canvas').length).toBe(3);
    // Every page has its box all the same — the scrollbar is the document's.
    expect(stage().querySelectorAll('[data-page]').length).toBe(40);
  });

  it('goes to a page by the buttons, the page box and the keys', async () => {
    const { handle, rendered } = fakeHandle(40);
    openPdf.mockResolvedValue(handle);
    show();
    await waitFor(() => expect(pageBox().value).toBe('1'));
    expect(screen.getByRole('button', { name: 'Previous page' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(pageBox().value).toBe('2'));

    fireEvent.change(pageBox(), { target: { value: '30' } });
    fireEvent.keyDown(pageBox(), { key: 'Enter' });
    await waitFor(() => expect(pageBox().value).toBe('30'));
    await waitFor(() => expect(renderedPages(rendered)).toContain(30));
    // Page 30 sits where the layout puts it: 29 pages of 800 px and their gaps.
    expect(stage().scrollTop).toBe(29 * (800 + 12));

    fireEvent.keyDown(window, { key: 'PageDown' });
    await waitFor(() => expect(pageBox().value).toBe('31'));
    fireEvent.keyDown(window, { key: 'ArrowLeft' });
    await waitFor(() => expect(pageBox().value).toBe('30'));
    fireEvent.keyDown(window, { key: 'End' });
    await waitFor(() => expect(pageBox().value).toBe('40'));
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled();
    fireEvent.keyDown(window, { key: 'Home' });
    await waitFor(() => expect(pageBox().value).toBe('1'));
  });

  it('puts a number the document does not have back to the current page', async () => {
    const { handle } = fakeHandle(5);
    openPdf.mockResolvedValue(handle);
    show();
    await waitFor(() => expect(pageBox().value).toBe('1'));

    fireEvent.change(pageBox(), { target: { value: 'abc' } });
    fireEvent.keyDown(pageBox(), { key: 'Enter' });
    expect(pageBox().value).toBe('1');
  });

  it('zooms in and out, fits the page, and comes back to the width', async () => {
    const { handle, rendered } = fakeHandle(3);
    openPdf.mockResolvedValue(handle);
    show();
    await waitFor(() => expect(rendered.length).toBeGreaterThan(0));
    const percent = () => screen.getByTestId('pdf-zoom').textContent;
    expect(percent()).toBe('100%');

    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    await waitFor(() => expect(percent()).toBe('125%'));
    await waitFor(() => expect(rendered.at(-1)?.scale).toBeCloseTo(1.25));

    fireEvent.click(screen.getByRole('button', { name: 'Zoom out' }));
    fireEvent.click(screen.getByRole('button', { name: 'Zoom out' }));
    await waitFor(() => expect(percent()).toBe('80%'));

    fireEvent.click(screen.getByRole('button', { name: 'Fit width' }));
    await waitFor(() => expect(percent()).toBe('100%'));

    // A stage half as tall: the whole page fits at half the scale.
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function (this: HTMLElement) {
      return this.dataset.testid === 'pdf-stage' ? 432 : 0;
    });
    fireEvent.click(screen.getByRole('button', { name: 'Fit page' }));
    await waitFor(() => expect(percent()).toBe('50%'));
  });

  it('follows the scrollbar: the page box names the page on screen, also right after a zoom', async () => {
    const { handle, rendered } = fakeHandle(40);
    openPdf.mockResolvedValue(handle);
    show();
    await waitFor(() => expect(rendered.length).toBeGreaterThan(0));

    // A zoom on page 1 moves nothing — the next scroll is the operator's, not ours.
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    await waitFor(() => expect(screen.getByTestId('pdf-zoom').textContent).toBe('125%'));
    stage().scrollTop = 5 * (1000 + 12);
    fireEvent.scroll(stage());
    await waitFor(() => expect(pageBox().value).toBe('6'));
    await waitFor(() => expect(renderedPages(rendered)).toContain(6));
  });

  describe('a document with pages of different sizes', () => {
    const percent = () => screen.getByTestId('pdf-zoom').textContent;
    const pageWidth = (n: number) => (stage().querySelector(`[data-page="${n}"]`) as HTMLElement).style.width;

    it('keeps the zoom and the place while scrolling across a page of another size', async () => {
      // Portrait pages and one landscape drawing, as a manual has.
      const { handle } = fakeHandle(10, { 3: { width: 1200, height: 800 } });
      openPdf.mockResolvedValue(handle);
      show();
      // Page 3 is next to the screen, so it is measured: its box is twice as wide.
      await waitFor(() => expect(pageWidth(3)).toBe('1200px'));
      expect(percent()).toBe('100%');

      // The middle of the screen crosses onto the landscape page.
      const onto = 2 * (800 + 12) - 300;
      stage().scrollTop = onto;
      fireEvent.scroll(stage());
      await waitFor(() => expect(pageBox().value).toBe('3'));
      expect(percent()).toBe('100%');
      expect(stage().scrollTop).toBe(onto);
      expect(pageWidth(2)).toBe('600px');

      // And one notch back.
      stage().scrollTop = onto - 250;
      fireEvent.scroll(stage());
      await waitFor(() => expect(pageBox().value).toBe('2'));
      expect(percent()).toBe('100%');
      expect(stage().scrollTop).toBe(onto - 250);
    });

    it('fits the widest page it knows when Fit width is asked for again, and the page on screen for Fit page', async () => {
      const { handle } = fakeHandle(10, { 3: { width: 1200, height: 800 } });
      openPdf.mockResolvedValue(handle);
      show();
      await waitFor(() => expect(pageWidth(3)).toBe('1200px'));

      fireEvent.click(screen.getByRole('button', { name: 'Fit width' }));
      await waitFor(() => expect(percent()).toBe('50%'));
      expect(pageWidth(3)).toBe('600px');

      // Fit page on page 1 (600 × 800 in a 600 × 800 room) is 100 % again.
      fireEvent.click(screen.getByRole('button', { name: 'Fit page' }));
      await waitFor(() => expect(percent()).toBe('100%'));
    });

    it('stays on what it shows when a page above turns out to have another height', async () => {
      // Page 29 is half as tall as the first page it was assumed to be like.
      const { handle, rendered } = fakeHandle(40, { 29: { width: 600, height: 400 } });
      openPdf.mockResolvedValue(handle);
      show();
      await waitFor(() => expect(pageBox().value).toBe('1'));

      fireEvent.change(pageBox(), { target: { value: '30' } });
      fireEvent.keyDown(pageBox(), { key: 'Enter' });
      await waitFor(() => expect(renderedPages(rendered)).toContain(29));
      await waitFor(() =>
        expect((stage().querySelector('[data-page="29"]') as HTMLElement).style.height).toBe('400px'),
      );

      // Still at the top of page 30, which now starts 400 px higher.
      expect(stage().scrollTop).toBe(28 * (800 + 12) + (400 + 12));
      expect(pageBox().value).toBe('30');
    });
  });

  it('cancels what it was drawing and frees the document when it closes', async () => {
    const { handle, rendered, cancelled } = fakeHandle(3);
    openPdf.mockResolvedValue(handle);
    const view = render(<PdfPreviewModal fileId={7} title="manual.pdf" fileSize={10} onClose={vi.fn()} onDownload={vi.fn()} />);
    await waitFor(() => expect(renderedPages(rendered)).toEqual([1, 2, 3]));
    expect(handle.destroy).not.toHaveBeenCalled();

    // The page closes the viewer by unmounting it.
    view.unmount();

    expect([...new Set(cancelled)].sort()).toEqual([1, 2, 3]);
    expect(handle.destroy).toHaveBeenCalledTimes(1);
  });

  it('gives up an open that has not settled when it closes, and when the file changes', async () => {
    // A document the parser hangs on: `openPdf` never answers.
    const signals: AbortSignal[] = [];
    openPdf.mockImplementation((_bytes: ArrayBuffer, signal: AbortSignal) => {
      signals.push(signal);
      return new Promise(() => {});
    });
    const view = render(<PdfPreviewModal fileId={7} title="a.pdf" fileSize={10} onClose={vi.fn()} onDownload={vi.fn()} />);
    await waitFor(() => expect(signals.length).toBe(1));
    expect(signals[0].aborted).toBe(false);

    view.rerender(<PdfPreviewModal fileId={8} title="b.pdf" fileSize={10} onClose={vi.fn()} onDownload={vi.fn()} />);
    await waitFor(() => expect(signals.length).toBe(2));
    expect(signals[0].aborted).toBe(true);
    expect(signals[1].aborted).toBe(false);

    view.unmount();
    expect(signals[1].aborted).toBe(true);
  });

  it('frees the document on unmount', async () => {
    const { handle, rendered } = fakeHandle(3);
    openPdf.mockResolvedValue(handle);
    const view = render(<PdfPreviewModal fileId={7} title="manual.pdf" fileSize={10} onClose={vi.fn()} onDownload={vi.fn()} />);
    await waitFor(() => expect(rendered.length).toBeGreaterThan(0));

    view.unmount();
    expect(handle.destroy).toHaveBeenCalled();
  });

  it('says a password-protected document cannot be shown, and offers the download', async () => {
    openPdf.mockRejectedValue(new PdfOpenError('encrypted'));
    const { onDownload } = show();

    expect(await screen.findByText('This PDF is password-protected and cannot be previewed.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    expect(onDownload).toHaveBeenCalled();
  });

  it('says a broken document cannot be shown', async () => {
    openPdf.mockRejectedValue(new PdfOpenError('broken'));
    show();

    expect(await screen.findByText('This PDF could not be opened. The file may be damaged.')).toBeInTheDocument();
  });

  // Four failures, four sentences: only a document PDF.js refused is "damaged".
  it.each([401, 403, 404])('says the file is not available when the server answers %i, and offers no download', async (status) => {
    getBytes.mockRejectedValue(new ApiError(`HTTP ${status}`, status));
    show();

    expect(
      await screen.findByText('This file is not available: it was moved or deleted, or you no longer have access to it.'),
    ).toBeInTheDocument();
    expect(screen.queryByText(/may be damaged/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Download' })).not.toBeInTheDocument();
    expect(openPdf).not.toHaveBeenCalled();
  });

  it('says the server could not be reached when the request itself fails', async () => {
    getBytes.mockRejectedValue(new TypeError('Failed to fetch'));
    show();

    expect(
      await screen.findByText('The file could not be loaded from the server. Check the connection and try again.'),
    ).toBeInTheDocument();
    expect(screen.queryByText(/may be damaged/)).not.toBeInTheDocument();
    expect(openPdf).not.toHaveBeenCalled();
  });

  it('says the viewer itself could not be loaded, and offers the download', async () => {
    openPdf.mockRejectedValue(new PdfOpenError('viewer'));
    const { onDownload } = show();

    expect(
      await screen.findByText('The PDF viewer could not be loaded. Reload the page and try again, or download the file.'),
    ).toBeInTheDocument();
    expect(screen.queryByText(/may be damaged/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    expect(onDownload).toHaveBeenCalled();
  });

  it('says each of them in the interface language', async () => {
    await act(async () => {
      await i18n.changeLanguage('uk');
    });
    try {
      getBytes.mockRejectedValue(new ApiError('HTTP 404', 404));
      const first = render(<PdfPreviewModal fileId={7} title="a.pdf" fileSize={10} onClose={vi.fn()} onDownload={vi.fn()} />);
      expect(
        await screen.findByText('Цей файл недоступний: його перемістили чи видалили або ви більше не маєте до нього доступу.'),
      ).toBeInTheDocument();
      first.unmount();

      getBytes.mockRejectedValue(new TypeError('Failed to fetch'));
      const second = render(<PdfPreviewModal fileId={7} title="a.pdf" fileSize={10} onClose={vi.fn()} onDownload={vi.fn()} />);
      expect(
        await screen.findByText('Не вдалося завантажити файл із сервера. Перевірте з’єднання і спробуйте ще раз.'),
      ).toBeInTheDocument();
      second.unmount();

      getBytes.mockResolvedValue(new ArrayBuffer(16));
      openPdf.mockRejectedValue(new PdfOpenError('viewer'));
      render(<PdfPreviewModal fileId={7} title="a.pdf" fileSize={10} onClose={vi.fn()} onDownload={vi.fn()} />);
      expect(
        await screen.findByText('Не вдалося завантажити переглядач PDF. Оновіть сторінку і спробуйте ще раз або завантажте файл.'),
      ).toBeInTheDocument();
    } finally {
      await act(async () => {
        await i18n.changeLanguage('en');
      });
    }
  });

  it('never requests a file over the size cap: it offers the download instead', async () => {
    const { onDownload } = show({ fileSize: MAX_PDF_BYTES + 1 });

    expect(await screen.findByText('This PDF is too large to preview. Download it to open it.')).toBeInTheDocument();
    expect(getBytes).not.toHaveBeenCalled();
    expect(openPdf).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    expect(onDownload).toHaveBeenCalled();
  });

  it('drops bytes over the cap even when the listed size was unknown', async () => {
    getBytes.mockResolvedValue({ byteLength: MAX_PDF_BYTES + 1 } as ArrayBuffer);
    show({ fileSize: null });

    expect(await screen.findByText('This PDF is too large to preview. Download it to open it.')).toBeInTheDocument();
    expect(openPdf).not.toHaveBeenCalled();
  });
});
