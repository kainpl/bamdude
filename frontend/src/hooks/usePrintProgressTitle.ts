import { useQueries, useQuery } from '@tanstack/react-query';
import { useEffect, useRef } from 'react';
import { api } from '../api/client';
import { useTheme } from '../contexts/ThemeContext';

const FALLBACK_ACCENT = '#00ae42'; // BamDude green, if --accent cannot be read (e.g. jsdom)

// remaining_time <= 0 means "ETA not known yet" — the backend defaults it to 0
// rather than null — so treat it as unknown rather than as "finishes now".
const eta = (t: number | null): number => (t != null && t > 0 ? t : Infinity);

// Only the fields this needs, so pickActivePrint stays decoupled from the full
// PrinterStatus type and a test can pass plain objects.
export interface ProgressStatus {
  state: string | null;
  progress: number | null;
  remaining_time: number | null;
}

/**
 * Of all printers, pick the RUNNING print to surface in the tab: the one
 * finishing soonest, tie-broken by highest progress. Null when nothing is
 * actively printing.
 */
export function pickActivePrint<T extends ProgressStatus>(statuses: (T | undefined)[]): T | null {
  let best: T | null = null;
  for (const s of statuses) {
    if (!s || s.state !== 'RUNNING' || s.progress == null) continue;
    if (best === null) {
      best = s;
      continue;
    }
    const sr = eta(s.remaining_time);
    const br = eta(best.remaining_time);
    if (sr < br || (sr === br && (s.progress ?? 0) > (best.progress ?? 0))) {
      best = s;
    }
  }
  return best;
}

// Draw a 32x32 progress ring in the current accent colour and return a PNG data
// URL — or null when the browser has no 2d canvas (jsdom), in which case the
// caller updates the title only.
function drawProgressFavicon(pct: number): string | null {
  const canvas = document.createElement('canvas');
  canvas.width = 32;
  canvas.height = 32;
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;

  const accent =
    getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || FALLBACK_ACCENT;

  const cx = 16;
  const cy = 16;
  const r = 13;
  const frac = Math.max(0, Math.min(100, pct)) / 100;
  const start = -Math.PI / 2; // 12 o'clock

  ctx.lineWidth = 4;
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.strokeStyle = 'rgba(128,128,128,0.3)';
  ctx.stroke();

  ctx.beginPath();
  ctx.arc(cx, cy, r, start, start + frac * Math.PI * 2);
  ctx.strokeStyle = accent;
  ctx.lineCap = 'round';
  ctx.stroke();

  return canvas.toDataURL('image/png');
}

// Point the <link rel="icon"> tags at the ring, remembering the originals — or
// restore them when dataUrl is null. `rel~="icon"` leaves apple-touch-icon
// alone, which is a different rel token.
function setFavicon(dataUrl: string | null, originals: Map<HTMLLinkElement, string>) {
  const links = document.querySelectorAll<HTMLLinkElement>('link[rel~="icon"]');
  links.forEach((link) => {
    if (dataUrl) {
      if (!originals.has(link)) originals.set(link, link.href);
      link.href = dataUrl;
    } else {
      const orig = originals.get(link);
      if (orig !== undefined) link.href = orig;
    }
  });
  if (!dataUrl) originals.clear();
}

/**
 * With the "progress in tab" preference on, reflect the soonest-finishing
 * print's percentage in `document.title` and draw a matching progress-ring
 * favicon. Stays completely inert until enabled, and hands the tab back to its
 * defaults once disabled, idle, or unmounted.
 *
 * Mounted once, globally, inside WebSocketProvider.
 */
export function usePrintProgressTitle() {
  const { progressInTitle, resolvedMode, darkAccent, lightAccent } = useTheme();
  // Re-draw the ring when the active accent changes.
  const accent = resolvedMode === 'dark' ? darkAccent : lightAccent;

  const { data: printers } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
    enabled: progressInTitle,
  });

  // No refetchInterval here, deliberately. This hook is mounted globally, so a
  // poll would add one request per printer every interval on EVERY page — the
  // Printers page already runs its own fallback on this exact key, and
  // useWebSocket writes ['printerStatus', id] straight into the cache. A
  // cosmetic title going stale during a WebSocket outage is an acceptable
  // trade for not putting the whole farm on a timer in every open tab.
  const statusQueries = useQueries({
    queries: (progressInTitle ? (printers ?? []) : []).map((p) => ({
      queryKey: ['printerStatus', p.id],
      queryFn: () => api.getPrinterStatus(p.id),
    })),
  });

  const originalsRef = useRef<Map<HTMLLinkElement, string>>(new Map());
  // The tab's own title, captured before this ever touches it, so restoring
  // does not depend on a constant staying in sync with index.html.
  const defaultTitleRef = useRef(document.title);
  // Whether we currently own the title/favicon. Lets the hook stay inert while
  // off — never touching the tab — yet still restore once if it ever took over.
  const ownsRef = useRef(false);

  const active = progressInTitle ? pickActivePrint(statusQueries.map((q) => q.data)) : null;
  const pct = active && active.progress != null ? Math.round(active.progress) : null;

  useEffect(() => {
    if (progressInTitle && pct != null) {
      document.title = `${pct}% · ${defaultTitleRef.current}`;
      setFavicon(drawProgressFavicon(pct), originalsRef.current);
      ownsRef.current = true;
    } else if (ownsRef.current) {
      // Disabled or idle after having taken over — hand the tab back.
      document.title = defaultTitleRef.current;
      setFavicon(null, originalsRef.current);
      ownsRef.current = false;
    }
    // else: never owned the tab → leave it entirely alone.
  }, [progressInTitle, pct, accent]);

  // Restore on unmount, but only if we own it.
  useEffect(() => {
    const originals = originalsRef.current;
    const owns = ownsRef;
    const defaultTitle = defaultTitleRef.current;
    return () => {
      if (owns.current) {
        document.title = defaultTitle;
        setFavicon(null, originals);
      }
    };
  }, []);
}
