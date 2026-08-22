import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X, Loader2, Printer, CheckSquare, Square, Search, FileText } from 'lucide-react';
import { api, type InventorySpool } from '../api/client';
import { getSwatchStyle } from '../utils/colors';
import { Button } from './Button';
import { useToast } from '../contexts/ToastContext';
import { useMutation, useQuery } from '@tanstack/react-query';
import type { LabelDevice } from '../api/client';
import { DEFAULT_SPOOL_DISPLAY_TEMPLATE, formatSpoolDisplayName } from '../utils/spoolName';

/** Subset of InventorySpool the modal needs. The label name is composed via
 *  ``formatSpoolDisplayName`` against ``spoolDisplayTemplate`` so the bold
 *  central label line matches the user's Inventory naming-template setting —
 *  see ``utils/spoolName.ts`` for the placeholder registry. */
type SpoolForLabel = InventorySpool;

interface LabelTemplatePickerModalProps {
  isOpen: boolean;
  onClose: () => void;
  /** All spools the modal can choose from. Typically the page's current
   *  filter result so the modal stays consistent with what the user sees. */
  availableSpools: SpoolForLabel[];
  /** IDs to pre-check when the modal opens. Per-card icon passes a single ID;
   *  the bulk header button passes every visible ID so the user lands in
   *  "all checked" and refines downward. */
  initialSelectedIds: number[];
  spoolmanMode: boolean;
  /** User's spool naming template (settings.spool_display_template). The
   *  composed name per spool is forwarded to the backend so the label PDF
   *  reflects the same naming rules as the on-screen list. */
  spoolDisplayTemplate?: string;
}

/** ⚠️ The same slack ``label_template.py`` allows, and it has to be: a design
 *  drawn to exactly the cell size is a design the backend accepts and this
 *  dialog must not grey out. */
const FIT_TOLERANCE_MM = 0.5;

/** How a batch is going out. ``driver`` is the OS print driver — a PDF, which
 *  may be one label per page or a whole sheet of them, and may be in colour.
 *  ``device`` is a thermal printer on somebody's desk, reached through the
 *  bridge running there. */
type PrintRoute = 'driver' | 'device';

function openBlobInNewTab(blob: Blob): void {
  const url = window.URL.createObjectURL(blob);
  // Do NOT pass `noopener,noreferrer`: per the WindowFeatures spec, `noopener`
  // forces window.open to return `null` even on success, which made the
  // `if (!win)` popup-block fallback below fire on EVERY click — so the blob
  // tab opened (downloading a random-named PDF on systems without an inline
  // viewer) AND the `<a download>` fallback fired (downloading a second copy
  // named bamdude-labels.pdf). Two identical PDFs per click — issue #1628.
  // The blob is same-origin, the destination is a passive PDF tab with no
  // script context, and `noreferrer` is a no-op for blob URLs, so dropping
  // these flags has no security impact.
  const win = window.open(url, '_blank');
  if (!win) {
    const a = document.createElement('a');
    a.href = url;
    a.download = 'bamdude-labels.pdf';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }
  setTimeout(() => window.URL.revokeObjectURL(url), 60_000);
}

// Thin wrapper over ``getSwatchStyle`` from utils/colors so the modal's render
// sites keep their existing call shape. Transparent (alpha=00) spools now
// render as a checkerboard pattern instead of collapsing to solid black
// (#1545).
function swatchStyle(rgba: string | null | undefined): React.CSSProperties {
  return getSwatchStyle(rgba);
}

/** Build a lowercased haystack that the search input matches against. */
function searchableText(s: SpoolForLabel, displayName: string): string {
  return [displayName, s.color_name, s.material, s.subtype, s.brand, `#${s.id}`]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();
}

type SortMode = 'id' | 'color';

/** Sort key for the "by colour" mode (upstream Bambuddy #1410).
 *
 * Returns a 2-tuple so JS array compare does the right thing without
 * spelling out a comparator: ``[bucket, position]``. Chromatic colours
 * (saturation above the threshold) go in bucket 0 ordered by HSL hue,
 * so the sheet reads as a continuous rainbow. Achromatic colours
 * (white / grey / black, plus missing / invalid rgba) go in bucket 1
 * ordered by lightness so the neutrals trail at the end of the
 * rainbow going dark → light. Multi-colour spools sort on their
 * primary ``rgba``; their ``extra_colors`` stripe is still rendered
 * on the label itself but doesn't drive the sort.
 */
function colorSortKey(rgba: string | null | undefined): [number, number] {
  if (!rgba) return [1, 0]; // unknown colour — bucket with neutrals at black
  const cleaned = rgba.replace(/^#/, '').slice(0, 6);
  if (cleaned.length !== 6) return [1, 0];
  const r = parseInt(cleaned.slice(0, 2), 16);
  const g = parseInt(cleaned.slice(2, 4), 16);
  const b = parseInt(cleaned.slice(4, 6), 16);
  if ([r, g, b].some(Number.isNaN)) return [1, 0];

  const rn = r / 255;
  const gn = g / 255;
  const bn = b / 255;
  const max = Math.max(rn, gn, bn);
  const min = Math.min(rn, gn, bn);
  const l = (max + min) / 2;
  const delta = max - min;
  // Saturation in the HSL definition. Achromatic cutoff at 0.1 is
  // generous — matches what feels "grey enough" to a user picking
  // colours, without sending dark muted colours like deep navy into
  // the neutrals bucket.
  const s = delta === 0 ? 0 : delta / (1 - Math.abs(2 * l - 1));
  if (s < 0.1) return [1, l]; // neutrals: ordered black → white

  let h = 0;
  if (max === rn) h = ((gn - bn) / delta) % 6;
  else if (max === gn) h = (bn - rn) / delta + 2;
  else h = (rn - gn) / delta + 4;
  h = h * 60;
  if (h < 0) h += 360;
  return [0, h]; // chromatic: ordered by hue 0..360
}

export function LabelTemplatePickerModal({
  isOpen,
  onClose,
  availableSpools,
  initialSelectedIds,
  spoolmanMode,
  spoolDisplayTemplate,
}: LabelTemplatePickerModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  // The design being printed, by id — the dialog used to hold one of six
  // hard-coded names here, which is what kept the catalogue invisible.
  const [pending, setPending] = useState<number | null>(null);
  // Which way this batch is going out, once there is a choice to make. Stays
  // null until somebody picks; when there is no desk printer to pick, the
  // question is never asked and the driver is simply what happens.
  const [route, setRoute] = useState<PrintRoute | null>(null);
  const [sheetId, setSheetId] = useState<number | null>(null);
  const [deviceTemplateId, setDeviceTemplateId] = useState<number | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [search, setSearch] = useState('');
  const [materialFilter, setMaterialFilter] = useState<string>('');
  // #1410: session-only sort toggle. Resets to 'id' each time the modal
  // opens. ``handlePick`` sends the queue order matching this sort, so
  // 'color' produces a label sheet ordered by hue (rainbow + neutrals).
  const [sortMode, setSortMode] = useState<SortMode>('id');
  // #1870: session-only monochrome toggle for black & white thermal printers.
  // Resets to off each time the modal opens.
  const [monochrome, setMonochrome] = useState(false);

  // Devices are only asked for while the modal is open, and only listed when
  // adopted — an unadopted one would be an option that silently refuses.
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings, enabled: isOpen });
  const deviceLabels = Boolean(settings?.device_labels_enabled);
  const { data: allDevices } = useQuery({
    queryKey: ['label-devices'],
    queryFn: api.getLabelDevices,
    enabled: isOpen && deviceLabels,
  });
  const devices = (allDevices ?? []).filter((d: LabelDevice) => d.enabled);

  // The catalogue, which is the whole point: adding a design adds an option
  // here, and renaming one renames it. Sheets are paper and only ever apply to
  // the driver — a thermal printer feeds a roll, not a page of stickers.
  const { data: templates } = useQuery({
    queryKey: ['label-templates'],
    queryFn: api.getLabelTemplates,
    enabled: isOpen,
  });
  const { data: sheets } = useQuery({
    queryKey: ['label-sheets'],
    queryFn: api.getLabelSheets,
    enabled: isOpen,
  });

  // A route step nobody can answer is a step that only costs a click.
  const routeIsAChoice = deviceLabels && devices.length > 0;
  const effectiveRoute: PrintRoute | null = routeIsAChoice ? route : 'driver';
  const wantedTarget = effectiveRoute === 'device' ? 'thermal' : 'driver';
  const designs = (templates ?? []).filter((row) => row.target === wantedTarget);
  const sheet = (sheets ?? []).find((row) => row.id === sheetId) ?? null;

  /** Why this design cannot go on the chosen paper, or null if it can.
   *
   * ⚠️ Said here as well as refused there. A design prints at its own size or
   * not at all — fractional scaling of a label destroys bar ratios silently —
   * so the honest answer is a sentence, and it is cheaper to read it before
   * clicking than after. */
  const cellComplaint = (design: { width_mm: number; height_mm: number }): string | null => {
    if (!sheet) return null;
    const fits =
      design.width_mm <= sheet.cell_width_mm + FIT_TOLERANCE_MM &&
      design.height_mm <= sheet.cell_height_mm + FIT_TOLERANCE_MM;
    return fits
      ? null
      : t('inventory.labels.doesNotFitCell', {
          w: design.width_mm,
          h: design.height_mm,
          cw: sheet.cell_width_mm,
          ch: sheet.cell_height_mm,
        });
  };

  const [sending, setSending] = useState<number | null>(null);
  const send = useMutation({ mutationFn: api.createLabelJobs });


  const effectiveTemplate = spoolDisplayTemplate || DEFAULT_SPOOL_DISPLAY_TEMPLATE;

  // Sync from caller and reset transient state on open. Intentionally not
  // reactive to props while open — once the user starts editing we don't want
  // a parent re-render to clobber their selection / filter / search.
  useEffect(() => {
    if (isOpen) {
      const allowed = new Set(availableSpools.map((s) => s.id));
      setSelectedIds(new Set(initialSelectedIds.filter((id) => allowed.has(id))));
      setSearch('');
      setMaterialFilter('');
      setSortMode('id');
      setMonochrome(false);
      setPending(null);
      setSending(null);
      setRoute(null);
      setSheetId(null);
      setDeviceTemplateId(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen]);

  const sortedSpools = useMemo(() => {
    const copy = [...availableSpools];
    if (sortMode === 'color') {
      copy.sort((a, b) => {
        const ka = colorSortKey(a.rgba);
        const kb = colorSortKey(b.rgba);
        if (ka[0] !== kb[0]) return ka[0] - kb[0];
        if (ka[1] !== kb[1]) return ka[1] - kb[1];
        // Stable tiebreaker on ID so identical colours print in a
        // deterministic order across renders.
        return a.id - b.id;
      });
      return copy;
    }
    copy.sort((a, b) => a.id - b.id);
    return copy;
  }, [availableSpools, sortMode]);

  // Pre-compose the display name per spool once, used for both rendering and search.
  const displayNameById = useMemo(() => {
    const map = new Map<number, string>();
    for (const s of sortedSpools) {
      map.set(s.id, formatSpoolDisplayName(s, effectiveTemplate));
    }
    return map;
  }, [sortedSpools, effectiveTemplate]);

  // Material chips are derived from the *full* available set so they stay
  // stable when search/material filter narrows the visible list.
  const materials = useMemo(() => {
    const set = new Set<string>();
    for (const s of sortedSpools) {
      if (s.material) set.add(s.material.toUpperCase());
    }
    return [...set].sort();
  }, [sortedSpools]);

  const visibleSpools = useMemo(() => {
    const q = search.trim().toLowerCase();
    return sortedSpools.filter((s) => {
      if (materialFilter && (s.material || '').toUpperCase() !== materialFilter) return false;
      if (q && !searchableText(s, displayNameById.get(s.id) || '').includes(q)) return false;
      return true;
    });
  }, [sortedSpools, search, materialFilter, displayNameById]);

  const allVisibleChecked =
    visibleSpools.length > 0 && visibleSpools.every((s) => selectedIds.has(s.id));

  if (!isOpen) return null;

  const selectedCount = selectedIds.size;
  const noSelection = selectedCount === 0;

  function toggleOne(id: number) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function selectAllVisible() {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      for (const s of visibleSpools) next.add(s.id);
      return next;
    });
  }

  function deselectVisible() {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      for (const s of visibleSpools) next.delete(s.id);
      return next;
    });
  }

  function clearAll() {
    setSelectedIds(new Set());
  }

  /**
   * Queue one label per selected spool on a desk printer.
   *
   * ⚠️ No template is named. The server picks the design matching the stock
   * the printer says is loaded, and refuses if nothing matches — which is a
   * better answer than this modal guessing, because only the printer knows
   * what is actually in it.
   */
  async function sendToDevice(device: LabelDevice) {
    if (noSelection || sending !== null) return;
    const ids = sortedSpools.filter((s) => selectedIds.has(s.id)).map((s) => s.id);
    const spools = ids.map((id) => ({ id, display_name: displayNameById.get(id) ?? null }));
    setSending(device.id);
    try {
      const jobs = await send.mutateAsync({
        device_id: device.id,
        spools,
        ...(deviceTemplateId !== null ? { template_id: deviceTemplateId } : {}),
      });
      showToast(t('inventory.labels.queuedOnDevice', { count: jobs.length }), 'success');
      onClose();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      showToast(msg, 'error');
    } finally {
      setSending(null);
    }
  }

  async function handlePick(templateId: number) {
    if (noSelection || pending) return;
    // Order matters: the backend (labels.py) prints labels in the same
    // order we send IDs. Use the sorted list so a "by colour" sort
    // flows through to the PDF instead of being clobbered by an
    // ascending-ID re-sort (upstream Bambuddy #1410).
    const ids = sortedSpools.filter((s) => selectedIds.has(s.id)).map((s) => s.id);
    // Forward the user's composed display name per spool — backend uses it
    // verbatim for the label's bold central line, falling back to the
    // backend-side composer when omitted.
    const spools = ids.map((id) => ({
      id,
      display_name: displayNameById.get(id) ?? null,
    }));
    setPending(templateId);
    const body = {
      spools,
      template_id: templateId,
      ...(sheetId !== null ? { sheet_id: sheetId } : {}),
      monochrome,
    };
    try {
      const blob = spoolmanMode
        ? await api.printSpoolmanSpoolLabels(body)
        : await api.printSpoolLabels(body);
      openBlobInNewTab(blob);
      onClose();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      showToast(
        t('inventory.labels.error', { msg, defaultValue: 'Could not generate labels: {{msg}}' }),
        'error',
      );
    } finally {
      setPending(null);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start sm:items-center justify-center p-4 overflow-y-auto">
      <div
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={onClose}
      />

      <div className="relative w-full max-w-3xl bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl max-h-[90vh] overflow-hidden flex flex-col my-auto">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
          <div className="flex items-center gap-2">
            <Printer className="w-5 h-5 text-bambu-green" />
            <h2 className="text-lg font-semibold text-white">
              {t('inventory.labels.title')}
            </h2>
            {selectedCount > 0 && (
              <span className="text-sm text-bambu-gray">
                ({t('inventory.labels.selectedCount', { count: selectedCount })})
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            className="p-1 text-bambu-gray hover:text-white rounded transition-colors"
            aria-label={t('common.close')}
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Search + material chips */}
        <div className="p-4 space-y-2 border-b border-bambu-dark-tertiary">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-bambu-gray pointer-events-none" />
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t('inventory.labels.searchPlaceholder')}
              className="w-full pl-9 pr-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm placeholder:text-bambu-gray focus:outline-none focus:border-bambu-green"
            />
          </div>
          {materials.length > 1 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs text-bambu-gray mr-1">
                {t('inventory.labels.filterByMaterial')}
              </span>
              <button
                type="button"
                onClick={() => setMaterialFilter('')}
                className={`px-2 py-0.5 text-xs rounded-full border transition ${
                  materialFilter === ''
                    ? 'bg-bambu-green text-bambu-dark border-bambu-green'
                    : 'bg-bambu-dark text-bambu-gray border-bambu-dark-tertiary hover:border-bambu-gray'
                }`}
              >
                {t('inventory.labels.allMaterials')}
              </button>
              {materials.map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => setMaterialFilter(m)}
                  className={`px-2 py-0.5 text-xs rounded-full border transition ${
                    materialFilter === m
                      ? 'bg-bambu-green text-bambu-dark border-bambu-green'
                      : 'bg-bambu-dark text-bambu-gray border-bambu-dark-tertiary hover:border-bambu-gray'
                  }`}
                >
                  {m}
                </button>
              ))}
            </div>
          )}
          {/* #1410: sort toggle. 'id' default keeps the historical
              ascending-ID order; 'color' clusters by hue (rainbow +
              neutrals trailing). Sort order flows through to the PDF
              via ``handlePick`` so multi-colour rolls group physically
              on the printed sheet. */}
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-xs text-bambu-gray mr-1">
              {t('inventory.labels.sortBy.label')}
            </span>
            <button
              type="button"
              onClick={() => setSortMode('id')}
              className={`px-2 py-0.5 text-xs rounded-full border transition ${
                sortMode === 'id'
                  ? 'bg-bambu-green text-bambu-dark border-bambu-green'
                  : 'bg-bambu-dark text-bambu-gray border-bambu-dark-tertiary hover:border-bambu-gray'
              }`}
            >
              {t('inventory.labels.sortBy.id')}
            </button>
            <button
              type="button"
              onClick={() => setSortMode('color')}
              className={`px-2 py-0.5 text-xs rounded-full border transition ${
                sortMode === 'color'
                  ? 'bg-bambu-green text-bambu-dark border-bambu-green'
                  : 'bg-bambu-dark text-bambu-gray border-bambu-dark-tertiary hover:border-bambu-gray'
              }`}
            >
              {t('inventory.labels.sortBy.color')}
            </button>
          </div>
        </div>

        {/* Action bar */}
        <div className="px-4 pt-3 pb-2 flex items-center justify-between gap-3 flex-wrap">
          <span className="text-sm text-bambu-gray">
            {t('inventory.labels.pickSpools')}
          </span>
          <div className="flex items-center gap-3 text-xs">
            <button
              type="button"
              onClick={allVisibleChecked ? deselectVisible : selectAllVisible}
              disabled={visibleSpools.length === 0}
              className="text-bambu-green hover:underline disabled:opacity-50 disabled:no-underline disabled:cursor-not-allowed"
            >
              {allVisibleChecked
                ? t('inventory.labels.deselectVisible')
                : t('inventory.labels.selectVisible', { count: visibleSpools.length })}
            </button>
            <button
              type="button"
              onClick={clearAll}
              disabled={selectedCount === 0}
              className="text-bambu-gray hover:text-white hover:underline disabled:opacity-50 disabled:no-underline disabled:cursor-not-allowed"
            >
              {t('inventory.labels.clearAll')}
            </button>
          </div>
        </div>

        {/* Spool list — ``min-h-0`` overrides the implicit min-height: auto on
            flex items so the list can yield height to keep all 5 templates +
            Cancel visible on tight viewports (upstream #1230 / 61314cf2). */}
        <div className="flex-1 overflow-y-auto px-2 pb-2 min-h-0">
          {visibleSpools.length === 0 ? (
            <div className="text-center text-sm text-bambu-gray py-6">
              {sortedSpools.length === 0
                ? t('inventory.labels.noSpoolsToShow')
                : t('inventory.labels.noMatches')}
            </div>
          ) : (
            <ul className="space-y-0.5">
              {visibleSpools.map((s) => {
                const checked = selectedIds.has(s.id);
                const displayName = displayNameById.get(s.id) || '';
                return (
                  <li key={s.id}>
                    <label className="flex items-center gap-3 px-2 py-1.5 rounded hover:bg-bambu-dark-tertiary/50 cursor-pointer">
                      {checked ? (
                        <CheckSquare className="w-4 h-4 text-bambu-green shrink-0" />
                      ) : (
                        <Square className="w-4 h-4 text-bambu-gray shrink-0" />
                      )}
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggleOne(s.id)}
                        className="sr-only"
                      />
                      <span
                        className="w-4 h-4 rounded border border-black/20 shrink-0"
                        style={swatchStyle(s.rgba)}
                      />
                      <span className="flex-1 min-w-0 truncate text-sm text-white">
                        {displayName || s.material}
                      </span>
                      <span className="text-xs font-mono text-bambu-gray shrink-0">
                        #{s.id}
                      </span>
                    </label>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {/* Print options (#1870) */}
        <div className="px-4 pt-2 pb-1 border-t border-bambu-dark-tertiary">
          <label className="inline-flex items-center gap-2 cursor-pointer select-none">
            {monochrome ? (
              <CheckSquare className="w-4 h-4 text-bambu-green shrink-0" />
            ) : (
              <Square className="w-4 h-4 text-bambu-gray shrink-0" />
            )}
            <input
              type="checkbox"
              checked={monochrome}
              onChange={(e) => setMonochrome(e.target.checked)}
              className="sr-only"
            />
            <span className="text-sm text-white">
              {t('inventory.labels.monochrome', 'Monochrome (black & white printer)')}
            </span>
            <span className="text-xs text-bambu-gray">
              {t('inventory.labels.monochromeHint', 'Drops the colour swatch; the hex line still carries the colour')}
            </span>
          </label>
        </div>

        {/* ── How this batch goes out ──────────────────────────────────
            Asked only when there is something to answer: a desk printer that
            has been adopted and switched on. Otherwise the driver is not a
            choice, it is simply what happens, and a step with one button is a
            click that teaches nothing. */}
        {routeIsAChoice && route === null && (
          <div className="px-3 pt-1 pb-2 space-y-2">
            <div className="text-xs font-medium text-white">{t('inventory.labels.route.title')}</div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              <button
                disabled={noSelection}
                onClick={() => setRoute('driver')}
                className="text-left p-2.5 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark hover:border-bambu-green disabled:opacity-50 flex items-center gap-2"
              >
                <FileText className="w-4 h-4 text-bambu-gray shrink-0" />
                <span className="min-w-0">
                  <span className="block font-medium text-white text-sm">{t('inventory.labels.route.driver')}</span>
                  <span className="block text-xs text-bambu-gray">{t('inventory.labels.route.driverHint')}</span>
                </span>
              </button>
              <button
                disabled={noSelection}
                onClick={() => setRoute('device')}
                className="text-left p-2.5 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark hover:border-bambu-green disabled:opacity-50 flex items-center gap-2"
              >
                <Printer className="w-4 h-4 text-bambu-gray shrink-0" />
                <span className="min-w-0">
                  <span className="block font-medium text-white text-sm">{t('inventory.labels.route.device')}</span>
                  <span className="block text-xs text-bambu-gray">{t('inventory.labels.route.deviceHint')}</span>
                </span>
              </button>
            </div>
          </div>
        )}

        {effectiveRoute !== null && (
          <div className="px-3 pt-1 pb-2 space-y-2">
            {routeIsAChoice && (
              <button
                onClick={() => setRoute(null)}
                disabled={pending !== null || sending !== null}
                className="text-xs text-bambu-gray hover:text-white disabled:opacity-50"
              >
                ← {t('inventory.labels.route.back')}
              </button>
            )}

            {/* ⚠️ Paper is a driver-only question. A desk label printer feeds a
                roll: there is no page to tile, and offering one would be a
                setting that cannot do anything. */}
            {effectiveRoute === 'driver' && (sheets ?? []).length > 0 && (
              <label className="flex flex-wrap items-center gap-2 text-xs text-bambu-gray">
                {t('inventory.labels.sheet.label')}
                <select
                  value={sheetId ?? ''}
                  onChange={(e) => setSheetId(e.target.value === '' ? null : Number(e.target.value))}
                  className="px-2 py-1.5 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                >
                  <option value="">{t('inventory.labels.sheet.none')}</option>
                  {(sheets ?? []).map((row) => (
                    <option key={row.id} value={row.id}>
                      {row.name} · {row.cols}×{row.rows}
                    </option>
                  ))}
                </select>
              </label>
            )}

            {/* The design. This is the catalogue — six hard-coded buttons used
                to sit here while the table they were meant to represent was
                ignored, so adding a design added nothing and renaming one
                renamed nothing. */}
            {designs.length === 0 ? (
              <p className="text-sm text-bambu-gray p-2.5 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark">
                {t('inventory.labels.noDesigns')}
              </p>
            ) : (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                {designs.map((design) => {
                  const complaint = cellComplaint(design);
                  return (
                    <button
                      key={design.id}
                      disabled={noSelection || pending !== null || complaint !== null}
                      onClick={() => handlePick(design.id)}
                      title={complaint ?? [design.name, design.description].filter(Boolean).join(' — ')}
                      className="w-full text-left p-2.5 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark hover:border-bambu-green disabled:opacity-50 disabled:hover:border-bambu-dark-tertiary flex items-center gap-2"
                    >
                      <div className="flex-1 min-w-0">
                        <div className="font-medium text-white text-sm truncate">{design.name}</div>
                        <div className="text-xs text-bambu-gray mt-0.5 truncate">
                          {complaint ?? design.description}
                        </div>
                      </div>
                      <span className="text-xs text-bambu-gray shrink-0">
                        {design.width_mm}×{design.height_mm}
                      </span>
                      {pending === design.id && (
                        <Loader2 className="w-4 h-4 animate-spin text-bambu-green shrink-0" />
                      )}
                    </button>
                  );
                })}
              </div>
            )}

            {/* Which printer, once the design is settled. Sending happens on
                this click — the design above is the batch's shape, the device
                below is where it goes. */}
            {effectiveRoute === 'device' && (
              <div className="p-2.5 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark space-y-2">
                <div className="text-xs font-medium text-white">{t('inventory.labels.sendToDevice')}</div>
                <label className="flex flex-wrap items-center gap-2 text-xs text-bambu-gray">
                  {t('inventory.labels.deviceDesign')}
                  <select
                    value={deviceTemplateId ?? ''}
                    onChange={(e) => setDeviceTemplateId(e.target.value === '' ? null : Number(e.target.value))}
                    className="px-2 py-1.5 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                  >
                    {/* ⚠️ Auto is not "some default": the server picks the design
                        whose size matches the stock the printer says is loaded,
                        and refuses rather than guessing when nothing matches. */}
                    <option value="">{t('inventory.labels.deviceDesignAuto')}</option>
                    {designs.map((design) => (
                      <option key={design.id} value={design.id}>
                        {design.name} · {design.width_mm}×{design.height_mm}
                      </option>
                    ))}
                  </select>
                </label>
                {devices.map((device) => (
                  <button
                    key={device.id}
                    disabled={noSelection || pending !== null || sending !== null}
                    onClick={() => sendToDevice(device)}
                    className="w-full text-left p-2 rounded-lg border border-bambu-dark-tertiary hover:border-bambu-green disabled:opacity-50 flex items-center gap-2"
                  >
                    <Printer className="w-4 h-4 text-bambu-gray shrink-0" />
                    <div className="flex-1 min-w-0">
                      <div className="text-sm text-white truncate">
                        {device.name || device.model || device.installation_id}
                      </div>
                      <div className="text-xs text-bambu-gray truncate">
                        {device.cassette_width_mm && device.cassette_height_mm
                          ? t('inventory.labels.deviceCassette', {
                              width: device.cassette_width_mm,
                              height: device.cassette_height_mm,
                            })
                          : t('inventory.labels.deviceCassetteUnknown')}
                        {!device.printer_reachable && ` — ${t('inventory.labels.deviceOffline')}`}
                      </div>
                    </div>
                    {sending === device.id && (
                      <Loader2 className="w-4 h-4 animate-spin text-bambu-green shrink-0" />
                    )}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        <div className="flex justify-end gap-2 px-5 py-2 border-t border-bambu-dark-tertiary">
          <Button variant="secondary" onClick={onClose} disabled={pending !== null}>
            {t('common.cancel')}
          </Button>
        </div>
      </div>
    </div>
  );
}
