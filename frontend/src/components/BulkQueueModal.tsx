import { useState, useMemo } from 'react';
import { useQuery, useQueries, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2, X } from 'lucide-react';

import { api, type LibraryFileListItem } from '../api/client';
import { Button } from './Button';
import { useToast } from '../contexts/ToastContext';
import { isSliced } from '../lib/fileTags';

interface BulkQueueModalProps {
  /** The whole selection, sliced and not — the dialog shows both. */
  files: LibraryFileListItem[];
  onClose: () => void;
}

/**
 * Queue a selection: onto one printer, several, or the auto-queue.
 *
 * The number on the button is the point of this dialog. A bulk action that
 * quietly creates three times the work it appeared to is a ruined evening on a
 * farm, so the count is shown before the click — and it is DERIVED on every
 * render rather than stored, because a stored copy would be a second source of
 * truth for the one number the dialog exists to promise.
 *
 * Unsliced files are listed greyed rather than rejected after the click: the
 * selection is made in the library, where STLs sit beside .gcode.3mf, so a
 * mixed selection is a normal state and not a user error.
 */
export function BulkQueueModal({ files, onClose }: BulkQueueModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [kind, setKind] = useState<'printers' | 'auto'>('printers');
  const [mode, setMode] = useState<'each' | 'spread'>('each');
  const [printerIds, setPrinterIds] = useState<number[]>([]);
  /** fileId → the plate ids still ticked. Absent until the plates arrive. */
  const [unticked, setUnticked] = useState<Record<number, number[]>>({});

  const sliced = useMemo(() => files.filter((f) => isSliced(f)), [files]);
  const unsliced = useMemo(() => files.filter((f) => !isSliced(f)), [files]);
  const multiPlate = useMemo(() => sliced.filter((f) => f.is_multi_plate), [sliced]);

  const { data: printers = [] } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
    enabled: kind === 'printers',
  });

  // Only the multi-plate files are asked about — the listing already carries
  // is_multi_plate, so an ordinary selection makes no requests at all.
  const plateQueries = useQueries({
    queries: multiPlate.map((f) => ({
      queryKey: ['library-file-plates', f.id],
      queryFn: () => api.getLibraryFilePlates(f.id),
    })),
  });

  const platesOf = (fileId: number): number[] => {
    const idx = multiPlate.findIndex((f) => f.id === fileId);
    if (idx < 0) return [];
    return (plateQueries[idx]?.data?.plates ?? []).map((p) => p.index);
  };

  /** Ticked plates for a file — everything, minus what the user unticked. */
  const tickedOf = (fileId: number): number[] => {
    const all = platesOf(fileId);
    if (all.length === 0) return [];
    const off = unticked[fileId] ?? [];
    return all.filter((p) => !off.includes(p));
  };

  const togglePlate = (fileId: number, plate: number) =>
    setUnticked((prev) => {
      const off = prev[fileId] ?? [];
      return { ...prev, [fileId]: off.includes(plate) ? off.filter((p) => p !== plate) : [...off, plate] };
    });

  const togglePrinter = (id: number) =>
    setPrinterIds((prev) => (prev.includes(id) ? prev.filter((p) => p !== id) : [...prev, id]));

  const items = useMemo(
    () =>
      sliced.map((f) => (f.is_multi_plate ? { file_id: f.id, plate_ids: tickedOf(f.id) } : { file_id: f.id })),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sliced, unticked, plateQueries.map((q) => q.data).join('|')],
  );

  const plateCount = items.reduce((n, i) => n + ('plate_ids' in i ? (i.plate_ids?.length ?? 0) : 1), 0);
  // "each" puts a copy on every printer; "spread" and the auto-queue produce
  // one print per plate however many machines are involved.
  const printCount = kind === 'printers' && mode === 'each' ? plateCount * Math.max(printerIds.length, 1) : plateCount;

  const submit = useMutation({
    mutationFn: () =>
      api.queueLibraryFiles(
        items,
        kind === 'auto' ? { kind: 'auto' } : { kind: 'printers', printer_ids: printerIds, mode },
      ),
    onSuccess: (outcome) => {
      void queryClient.invalidateQueries({ queryKey: ['print-queue'] });
      void queryClient.invalidateQueries({ queryKey: ['auto-queue'] });
      showToast(t('fileManager.bulkQueue.queued', { count: outcome.added.length }), 'success');
      // Errors are listed, not summarised: "3 failed" without saying which is
      // not something an operator can act on.
      for (const e of outcome.errors) showToast(`${e.filename}: ${e.error}`, 'error');
      if (outcome.errors.length === 0) onClose();
    },
    onError: (err: Error) => showToast(err.message, 'error'),
  });

  const loadingPlates = plateQueries.some((q) => q.isLoading);
  const titleId = 'bulk-queue-title';

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="relative w-full max-w-lg mx-4 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl max-h-[90vh] flex flex-col"
      >
        <div className="flex items-center justify-between px-5 py-3 border-b border-bambu-dark-tertiary">
          <h2 id={titleId} className="text-lg font-semibold text-white">
            {t('fileManager.bulkQueue.title')}
          </h2>
          <button type="button" onClick={onClose} aria-label={t('common.close')} className="p-1.5 text-bambu-gray hover:text-white">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="overflow-y-auto px-5 py-3 space-y-1">
          {sliced.map((f) => (
            <div key={f.id} data-bulk-row className="py-1">
              <div className="flex items-center gap-2 text-sm text-white">
                <span className="truncate">{f.print_name || f.filename}</span>
                {f.is_multi_plate && (
                  <span className="text-xs text-bambu-gray">
                    {t('fileManager.bulkQueue.plates', { count: platesOf(f.id).length })}
                  </span>
                )}
              </div>
              {f.is_multi_plate && (
                <div className="flex items-center gap-3 mt-1 ml-4">
                  {platesOf(f.id).map((plate) => (
                    <label key={plate} className="flex items-center gap-1 text-xs text-bambu-gray">
                      <input
                        type="checkbox"
                        aria-label={String(plate)}
                        checked={tickedOf(f.id).includes(plate)}
                        onChange={() => togglePlate(f.id, plate)}
                        className="w-3.5 h-3.5 accent-bambu-green"
                      />
                      {plate}
                    </label>
                  ))}
                </div>
              )}
            </div>
          ))}
          {unsliced.map((f) => (
            <div key={f.id} data-bulk-row className="flex items-center gap-2 py-1 text-sm text-bambu-gray/60">
              <span className="truncate">{f.print_name || f.filename}</span>
              <span className="text-xs">{t('fileManager.bulkQueue.notSliced')}</span>
            </div>
          ))}
        </div>

        <div className="px-5 py-3 border-t border-bambu-dark-tertiary space-y-2">
          <div className="flex items-center gap-4 text-sm">
            <label className="flex items-center gap-1.5 text-white">
              <input type="radio" aria-label={t('fileManager.bulkQueue.toPrinters')} checked={kind === 'printers'} onChange={() => setKind('printers')} className="accent-bambu-green" />
              {t('fileManager.bulkQueue.toPrinters')}
            </label>
            <label className="flex items-center gap-1.5 text-white">
              <input type="radio" aria-label={t('fileManager.bulkQueue.toAuto')} checked={kind === 'auto'} onChange={() => setKind('auto')} className="accent-bambu-green" />
              {t('fileManager.bulkQueue.toAuto')}
            </label>
          </div>

          {/* Hidden, not disabled, for the auto-queue: not choosing is its
              entire point. */}
          {kind === 'printers' && (
            <>
              <div className="flex items-center gap-1.5 flex-wrap">
                {printers.map((p) => (
                  <button
                    key={p.id}
                    type="button"
                    onClick={() => togglePrinter(p.id)}
                    className={`text-xs px-2 py-0.5 rounded-full border transition-colors ${
                      printerIds.includes(p.id)
                        ? 'bg-bambu-green text-white border-bambu-green'
                        : 'bg-bambu-dark border-bambu-dark-tertiary text-bambu-gray hover:text-white'
                    }`}
                  >
                    {p.name}
                  </button>
                ))}
              </div>
              <div className="flex items-center gap-4 text-sm">
                <label className="flex items-center gap-1.5 text-white">
                  <input type="radio" aria-label={t('fileManager.bulkQueue.modeEach')} checked={mode === 'each'} onChange={() => setMode('each')} className="accent-bambu-green" />
                  {t('fileManager.bulkQueue.modeEach')}
                </label>
                <label className="flex items-center gap-1.5 text-white">
                  <input type="radio" aria-label={t('fileManager.bulkQueue.modeSpread')} checked={mode === 'spread'} onChange={() => setMode('spread')} className="accent-bambu-green" />
                  {t('fileManager.bulkQueue.modeSpread')}
                </label>
              </div>
              <p className="text-xs text-bambu-gray/70">{t('fileManager.bulkQueue.spreadHint')}</p>
            </>
          )}
        </div>

        <div className="px-5 py-3 border-t border-bambu-dark-tertiary flex items-center justify-end gap-2">
          <Button
            data-submit
            onClick={() => submit.mutate()}
            disabled={printCount === 0 || submit.isPending || loadingPlates || (kind === 'printers' && printerIds.length === 0)}
          >
            {submit.isPending && <Loader2 className="w-4 h-4 animate-spin" />}
            {t('fileManager.bulkQueue.submit', { count: printCount })}
          </Button>
        </div>
      </div>
    </div>
  );
}
