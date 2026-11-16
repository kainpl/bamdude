/**
 * A PDF file, opened: its pages one under another in the preview shell, with
 * page navigation and zoom.
 *
 * The bytes come from the library's own download route, with the session's
 * headers (never a token in a URL), and go to PDF.js as an ArrayBuffer through
 * `utils/pdfDocument` — the only module that knows PDF.js. Everything this
 * component computes is in `utils/pdfView`.
 *
 * Every page has a box of its size from the start, so the scrollbar is the
 * document's; a canvas exists only for the pages on screen and their
 * neighbours, and is dropped when its page scrolls away — a 500-page manual
 * holds three bitmaps, not 500.
 *
 * ⚠️ A file over `MAX_PDF_BYTES` is never requested: the whole file is held
 * in memory twice while it is parsed. The viewer offers the download instead.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronLeft, ChevronRight, Download, FileWarning, Loader2, MoveHorizontal, RectangleVertical, ZoomIn, ZoomOut } from 'lucide-react';
import { ApiError, api } from '../api/client';
import { FilePreviewShell, PREVIEW_CONTROL_CLASS } from './FilePreviewShell';
import { PdfOpenError, openPdf, type PdfHandle } from '../utils/pdfDocument';
import {
  MAX_SCALE,
  MIN_SCALE,
  clampPage,
  currentPage,
  keyAction,
  anchorAt,
  anchorOffset,
  layoutPages,
  outputScale,
  parsePageInput,
  scaleFor,
  visibleRange,
  widestPage,
  zoomStep,
  type ScrollAnchor,
  type Size,
  type ZoomMode,
} from '../utils/pdfView';

export const MAX_PDF_BYTES = 64 * 1024 * 1024;
/** The most canvas pixels one page is drawn with. */
const MAX_PAGE_PIXELS = 16_000_000;
/** Around the pages, and between them, in CSS pixels. */
const PADDING = 16;
const GAP = 12;
/** Pages drawn beyond the screen on each side. */
const OVERSCAN = 1;
/** What one press of an arrow key scrolls. */
const LINE = 60;

/**
 * `unavailable` — the server refused the file (signed out, no access, moved or
 * deleted); `network` — the request did not get an answer; `viewer` — PDF.js
 * did not load; `broken` — PDF.js read the bytes and refused them. Four
 * failures, four sentences: only the last one is about the document.
 */
type Status = 'loading' | 'ready' | 'encrypted' | 'broken' | 'too_large' | 'unavailable' | 'network' | 'viewer';

const FAILURE_TEXT: Record<Exclude<Status, 'loading' | 'ready'>, string> = {
  too_large: 'filePreview.pdf.tooLarge',
  encrypted: 'filePreview.pdf.encrypted',
  broken: 'filePreview.pdf.broken',
  unavailable: 'filePreview.pdf.unavailable',
  network: 'filePreview.pdf.network',
  viewer: 'filePreview.pdf.viewerFailed',
};
/** Where the download can work: the server answered, or was never asked. */
const OFFERS_DOWNLOAD: ReadonlySet<Status> = new Set<Status>(['too_large', 'encrypted', 'broken', 'viewer']);

interface Props {
  fileId: number;
  title: string;
  /** The file's size in bytes, when known. */
  fileSize?: number | null;
  onClose: () => void;
  /** Save the file instead — offered when it cannot be shown. */
  onDownload: () => void;
}

function PdfPage({ handle, number, width, height, scale, active, onSize }: {
  handle: PdfHandle;
  number: number;
  width: number;
  height: number;
  /** CSS pixels per PDF point. */
  scale: number;
  active: boolean;
  onSize: (page: number, size: Size) => void;
}) {
  const { t } = useTranslation();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!active) return;
    let stale = false;
    handle.pageSize(number).then(
      (size) => {
        if (!stale) onSize(number, size);
      },
      () => {},
    );
    return () => {
      stale = true;
    };
  }, [active, handle, number, onSize]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!active || !canvas || width <= 0 || height <= 0) return;
    setFailed(false);
    // Canvas pixels per PDF point: CSS pixels per point, times canvas pixels per CSS pixel.
    const ratio = outputScale({ width, height }, window.devicePixelRatio || 1, MAX_PAGE_PIXELS);
    const job = handle.render(number, canvas, scale * ratio);
    let stale = false;
    job.promise.catch(() => {
      if (!stale) setFailed(true);
    });
    return () => {
      stale = true;
      job.cancel();
    };
  }, [active, handle, number, width, height, scale]);

  return (
    <div data-page={number} className="relative shrink-0 bg-white shadow-lg" style={{ width, height }}>
      {active && <canvas ref={canvasRef} className="block h-full w-full" />}
      {failed && (
        <p className="absolute inset-0 flex items-center justify-center p-4 text-center text-sm text-gray-600">
          {t('filePreview.pdf.pageFailed')}
        </p>
      )}
    </div>
  );
}

export function PdfPreviewModal({ fileId, title, fileSize, onClose, onDownload }: Props) {
  const { t } = useTranslation();
  const tooManyBytes = fileSize != null && fileSize > MAX_PDF_BYTES;
  const [status, setStatus] = useState<Status>(tooManyBytes ? 'too_large' : 'loading');
  const [handle, setHandle] = useState<PdfHandle | null>(null);
  const [sizes, setSizes] = useState<Size[]>([]);
  const [mode, setMode] = useState<ZoomMode>('width');
  // What a fit mode fits. ⚠️ Chosen once per request — when the document
  // opens and when a fit button is pressed — never from "the page on screen
  // now": pages differ in size, and a scale that followed the current page
  // rescaled the whole document under the operator's wheel.
  const [fitBasis, setFitBasis] = useState<Size>({ width: 0, height: 0 });
  const [stageSize, setStageSize] = useState<Size>({ width: 0, height: 0 });
  const [scrollTop, setScrollTop] = useState(0);
  const [page, setPage] = useState(1);
  const [pageText, setPageText] = useState('1');
  const stageRef = useRef<HTMLDivElement>(null);
  /** The page a scroll of our own is heading for — the scroll event it causes must not rename it. */
  const heading = useRef<number | null>(null);
  const pageRef = useRef(1);
  /** What the top edge of the screen shows — kept there across every new layout. */
  const anchor = useRef<ScrollAnchor>({ page: 1, fraction: 0 });

  // Fetch and open. Whatever was opened is freed when the viewer goes away.
  useEffect(() => {
    if (tooManyBytes) return;
    const abort = new AbortController();
    let opened: PdfHandle | null = null;
    let gone = false;
    (async () => {
      let bytes: ArrayBuffer;
      try {
        bytes = await api.getLibraryFileBytes(fileId, abort.signal);
      } catch (error) {
        if (gone) return;
        const refused = error instanceof ApiError && [401, 403, 404].includes(error.status);
        setStatus(refused ? 'unavailable' : 'network');
        return;
      }
      if (gone) return;
      if (bytes.byteLength > MAX_PDF_BYTES) {
        setStatus('too_large');
        return;
      }
      try {
        const doc = await openPdf(bytes);
        if (gone) {
          void doc.destroy();
          return;
        }
        opened = doc;
        setSizes(Array.from({ length: doc.pageCount }, () => doc.firstPageSize));
        setFitBasis(doc.firstPageSize);
        setHandle(doc);
        setStatus('ready');
      } catch (error) {
        if (gone) return;
        setStatus(error instanceof PdfOpenError ? error.reason : 'broken');
      }
    })();
    return () => {
      gone = true;
      abort.abort();
      void opened?.destroy();
    };
  }, [fileId, tooManyBytes]);

  const measure = useCallback(() => {
    const stage = stageRef.current;
    if (!stage) return;
    setStageSize((before) =>
      before.width === stage.clientWidth && before.height === stage.clientHeight
        ? before
        : { width: stage.clientWidth, height: stage.clientHeight },
    );
  }, []);

  const ready = status === 'ready' && handle !== null;
  useLayoutEffect(() => {
    const stage = stageRef.current;
    if (!ready || !stage) return;
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(stage);
    return () => observer.disconnect();
  }, [ready, measure]);

  const pageCount = handle?.pageCount ?? 0;
  const scale = useMemo(() => scaleFor(mode, fitBasis, stageSize, PADDING), [mode, fitBasis, stageSize]);
  const layout = useMemo(() => layoutPages(sizes, scale, GAP), [sizes, scale]);
  const range = visibleRange(layout.tops, scrollTop, stageSize.height, OVERSCAN);

  const goTo = useCallback(
    (target: number) => {
      const next = clampPage(target, pageCount);
      pageRef.current = next;
      setPage(next);
      setPageText(String(next));
      const stage = stageRef.current;
      if (!stage) return;
      const before = stage.scrollTop;
      stage.scrollTop = layout.tops[next - 1] ?? 0;
      // No scroll event follows a scroll that went nowhere.
      heading.current = stage.scrollTop === before ? null : next;
      anchor.current = anchorAt(layout, stage.scrollTop, GAP);
      setScrollTop(stage.scrollTop);
    },
    [layout, pageCount],
  );

  // A new layout — another scale, or a page measured at another size — moves
  // the pages: put what the top edge of the screen showed back under it.
  const layoutSeen = useRef(layout);
  useLayoutEffect(() => {
    if (layoutSeen.current === layout) return;
    layoutSeen.current = layout;
    const stage = stageRef.current;
    if (!stage) return;
    const before = stage.scrollTop;
    stage.scrollTop = anchorOffset(layout, anchor.current, GAP);
    // As in goTo: no scroll event follows a scroll that went nowhere.
    heading.current = stage.scrollTop === before ? null : pageRef.current;
    setScrollTop(stage.scrollTop);
  }, [layout]);

  const onScroll = () => {
    const stage = stageRef.current;
    if (!stage) return;
    setScrollTop(stage.scrollTop);
    anchor.current = anchorAt(layout, stage.scrollTop, GAP);
    if (heading.current !== null) {
      heading.current = null;
      return;
    }
    const now = currentPage(layout.tops, stage.scrollTop, stage.clientHeight);
    pageRef.current = now;
    setPage(now);
    setPageText(String(now));
  };

  const zoomTo = useCallback(
    (next: ZoomMode) => {
      measure();
      // A fit request: the widest page known for the width, the page on screen for the page.
      if (next === 'width') setFitBasis(widestPage(sizes));
      else if (next === 'page') setFitBasis(sizes[pageRef.current - 1] ?? { width: 0, height: 0 });
      setMode(next);
    },
    [measure, sizes],
  );

  const onSize = useCallback((number: number, size: Size) => {
    setSizes((before) => {
      const known = before[number - 1];
      if (!known || (known.width === size.width && known.height === size.height)) return before;
      const next = before.slice();
      next[number - 1] = size;
      return next;
    });
  }, []);

  // The keys. Escape is the shell's (the modal stack). Not while another
  // dialog sits above this one, never with a modifier (Ctrl + is the
  // browser's own zoom), and not while the page box is being typed in.
  useEffect(() => {
    if (!ready) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const stage = stageRef.current;
      if (!stage || stage.closest('[inert]')) return;
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) return;
      const action = keyAction(e.key);
      if (!action) return;
      e.preventDefault();
      if (action === 'next') goTo(pageRef.current + 1);
      else if (action === 'prev') goTo(pageRef.current - 1);
      else if (action === 'first') goTo(1);
      else if (action === 'last') goTo(pageCount);
      else if (action === 'scrollDown') stage.scrollTop += LINE;
      else if (action === 'scrollUp') stage.scrollTop -= LINE;
      else if (action === 'zoomIn') zoomTo(zoomStep(scale, 1));
      else if (action === 'zoomOut') zoomTo(zoomStep(scale, -1));
      else zoomTo('width');
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [ready, goTo, pageCount, scale, zoomTo]);

  const submitPage = () => {
    const wanted = parsePageInput(pageText, pageCount);
    if (wanted === null) setPageText(String(page));
    else goTo(wanted);
  };
  const onPageKey = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    submitPage();
  };

  // The stage's empty part is the lightbox's ground: a click there closes.
  const onGroundClick = (e: ReactMouseEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget) onClose();
  };

  const controls = ready ? (
    <>
      <button
        type="button"
        className={PREVIEW_CONTROL_CLASS}
        onClick={() => zoomTo(zoomStep(scale, -1))}
        disabled={scale <= MIN_SCALE}
        aria-label={t('filePreview.zoomOut')}
        title={t('filePreview.zoomOut')}
      >
        <ZoomOut className="h-5 w-5" />
      </button>
      <span data-testid="pdf-zoom" className="min-w-12 text-center text-sm tabular-nums text-bambu-gray">
        {Math.round(scale * 100)}%
      </span>
      <button
        type="button"
        className={PREVIEW_CONTROL_CLASS}
        onClick={() => zoomTo(zoomStep(scale, 1))}
        disabled={scale >= MAX_SCALE}
        aria-label={t('filePreview.zoomIn')}
        title={t('filePreview.zoomIn')}
      >
        <ZoomIn className="h-5 w-5" />
      </button>
      <button
        type="button"
        className={`${PREVIEW_CONTROL_CLASS} ${mode === 'width' ? 'text-white' : ''}`}
        onClick={() => zoomTo('width')}
        aria-pressed={mode === 'width'}
        aria-label={t('filePreview.pdf.fitWidth')}
        title={t('filePreview.pdf.fitWidth')}
      >
        <MoveHorizontal className="h-5 w-5" />
      </button>
      <button
        type="button"
        className={`${PREVIEW_CONTROL_CLASS} ${mode === 'page' ? 'text-white' : ''}`}
        onClick={() => zoomTo('page')}
        aria-pressed={mode === 'page'}
        aria-label={t('filePreview.pdf.fitPage')}
        title={t('filePreview.pdf.fitPage')}
      >
        <RectangleVertical className="h-5 w-5" />
      </button>
    </>
  ) : null;

  if (!ready) {
    return (
      <FilePreviewShell title={title} onClose={onClose}>
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 p-6 text-center text-bambu-gray" onClick={onGroundClick}>
          {status === 'loading' ? (
            <>
              <Loader2 className="h-10 w-10 animate-spin" />
              <p>{t('common.loading')}</p>
            </>
          ) : (
            <>
              <FileWarning className="h-10 w-10" />
              <p>{t(FAILURE_TEXT[status === 'ready' ? 'broken' : status])}</p>
              {OFFERS_DOWNLOAD.has(status) && (
                <button
                  type="button"
                  onClick={onDownload}
                  className="inline-flex items-center gap-2 rounded-lg bg-white/10 px-4 py-2 text-sm text-white transition-colors hover:bg-white/20"
                >
                  <Download className="h-4 w-4" />
                  {t('common.download')}
                </button>
              )}
            </>
          )}
        </div>
      </FilePreviewShell>
    );
  }

  return (
    <FilePreviewShell title={title} onClose={onClose} controls={controls}>
      <div ref={stageRef} data-testid="pdf-stage" className="absolute inset-0 overflow-auto [overflow-anchor:none]" onScroll={onScroll} onClick={onGroundClick}>
        <div
          className="flex min-w-fit flex-col items-center"
          style={{ padding: PADDING, gap: GAP, minHeight: '100%' }}
          onClick={onGroundClick}
        >
          {sizes.map((_, index) => (
            <PdfPage
              key={index}
              handle={handle}
              number={index + 1}
              width={layout.widths[index]}
              height={layout.heights[index]}
              scale={scale}
              active={index + 1 >= range.first && index + 1 <= range.last}
              onSize={onSize}
            />
          ))}
        </div>
      </div>
      {/* Page navigation floats over the stage: the bar above is the zoom's, and a phone has no room for both. */}
      <div className="pointer-events-none absolute inset-x-0 bottom-4 flex justify-center">
        <div className="pointer-events-auto flex items-center gap-1 rounded-full bg-black/70 px-2 py-1 shadow-lg">
          <button
            type="button"
            className={PREVIEW_CONTROL_CLASS}
            onClick={() => goTo(page - 1)}
            disabled={page <= 1}
            aria-label={t('filePreview.pdf.previousPage')}
            title={t('filePreview.pdf.previousPage')}
          >
            <ChevronLeft className="h-5 w-5" />
          </button>
          <input
            type="number"
            inputMode="numeric"
            min={1}
            max={pageCount}
            value={pageText}
            onChange={(e) => setPageText(e.target.value)}
            onKeyDown={onPageKey}
            onBlur={() => setPageText(String(page))}
            aria-label={t('filePreview.pdf.page')}
            className="h-8 w-14 rounded border border-white/20 bg-black/40 px-1 text-center text-sm tabular-nums text-white [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
          />
          <span className="pr-1 text-sm tabular-nums text-bambu-gray">/ {pageCount}</span>
          <button
            type="button"
            className={PREVIEW_CONTROL_CLASS}
            onClick={() => goTo(page + 1)}
            disabled={page >= pageCount}
            aria-label={t('filePreview.pdf.nextPage')}
            title={t('filePreview.pdf.nextPage')}
          >
            <ChevronRight className="h-5 w-5" />
          </button>
        </div>
      </div>
    </FilePreviewShell>
  );
}
