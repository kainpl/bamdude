import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { Layers, Clock, Weight, Box, Loader2, X } from 'lucide-react';
import { api } from '../api/client';
import type { PlateMetadata } from '../types/plates';
import { resolveSpoolColorName } from '../utils/colors';
import { formatDuration } from '../utils/date';

interface Props {
  fileId: number;
}

/**
 * Per-plate gallery for multi-plate 3MF library files.
 *
 * Layout mirrors ``PrintModal/PlateSelector``: a tall vertical paginator strip
 * on the LEFT (one button per plate, with selection-style highlight for the
 * active one) plus one big card on the RIGHT showing the active plate's
 * thumbnail, name, stats (print time, total weight, instance count) and
 * per-filament breakdown.
 */
export function LibraryPlateGallery({ fileId }: Props) {
  const { t } = useTranslation();
  const { data, isLoading } = useQuery({
    queryKey: ['library-file-plates', fileId],
    queryFn: () => api.getLibraryFilePlates(fileId),
    staleTime: 5 * 60_000,
  });

  const plates: PlateMetadata[] = useMemo(() => data?.plates ?? [], [data]);

  const [activeIdx, setActiveIdx] = useState<number | null>(null);
  useEffect(() => {
    if (plates.length === 0) return;
    if (activeIdx !== null && plates.find((p) => p.index === activeIdx)) return;
    setActiveIdx(plates[0].index);
  }, [plates, activeIdx]);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-8">
        <Loader2 className="w-5 h-5 animate-spin text-bambu-gray" />
      </div>
    );
  }
  if (plates.length === 0) return null;

  const active = plates.find((p) => p.index === activeIdx);

  return (
    <div className="flex gap-2 items-stretch">
      {/* Vertical paginator strip — one button per plate, clicking
          switches the visible card. */}
      <div className="flex flex-col gap-1 flex-shrink-0">
        {plates.map((p) => {
          const isActive = p.index === activeIdx;
          return (
            <button
              key={p.index}
              type="button"
              onClick={() => setActiveIdx(p.index)}
              className={`relative w-10 h-10 rounded-lg border text-xs font-medium transition-colors ${
                isActive
                  ? 'border-bambu-green bg-bambu-green/10 text-bambu-green'
                  : 'border-bambu-dark-tertiary bg-bambu-dark text-bambu-gray hover:border-bambu-gray'
              }`}
              title={p.name || `${t('fileManager.plate')} ${p.index}`}
            >
              {p.index}
            </button>
          );
        })}
      </div>

      {/* Active-plate card — full remaining width. */}
      {active && (
        <div className="flex-1 flex flex-col gap-3 p-3 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark">
          <div className="flex items-start gap-3 h-32">
            {active.has_thumbnail && active.thumbnail_url != null ? (
              <img
                src={active.thumbnail_url}
                alt={`${t('fileManager.plate')} ${active.index}`}
                className="w-32 h-32 rounded object-contain bg-bambu-dark-tertiary flex-shrink-0"
              />
            ) : (
              <div className="w-32 h-32 rounded bg-bambu-dark-tertiary flex items-center justify-center flex-shrink-0">
                <Layers className="w-10 h-10 text-bambu-gray" />
              </div>
            )}

            <div className="min-w-0 flex-1 flex flex-col h-full min-h-0">
              <p className="text-sm text-white font-medium truncate mb-1.5">
                {active.name || `${t('fileManager.plate')} ${active.index}`}
                {plates.length > 1 && (
                  <span className="text-bambu-gray ml-1 font-normal">
                    ({active.index}/{plates.length})
                  </span>
                )}
              </p>

              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-bambu-gray">
                {active.print_time_seconds != null && (
                  <span className="flex items-center gap-1">
                    <Clock className="w-3 h-3" />
                    {formatDuration(active.print_time_seconds)}
                  </span>
                )}
                {active.filament_used_grams != null && (
                  <span className="flex items-center gap-1">
                    <Weight className="w-3 h-3" />
                    {active.filament_used_grams.toFixed(1)} {t('common.gramShort')}
                  </span>
                )}
                {(active.object_count ?? active.objects.length) > 0 && (
                  <span className="flex items-center gap-1">
                    <Box className="w-3 h-3" />
                    {active.object_count ?? active.objects.length}
                  </span>
                )}
              </div>

              {active.objects.length > 0 && (
                <ul className="mt-1.5 text-xs text-bambu-gray/70 space-y-0.5 list-disc list-inside flex-1 min-h-0 overflow-y-auto">
                  {active.objects.map((name, i) => (
                    <li key={`${name}-${i}`} className="truncate">
                      {name}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>

          {active.filaments.length > 0 && (
            <div className="border-t border-bambu-dark-tertiary pt-2 space-y-1">
              {active.filaments.map((f, i) => (
                <div key={f.slot_id ?? i} className="flex items-center gap-2 text-xs">
                  <span
                    className="inline-block w-3 h-3 rounded-sm border border-bambu-dark-tertiary flex-shrink-0"
                    style={{ backgroundColor: f.color || '#888' }}
                  />
                  {f.slot_id != null && (
                    <span className="text-white">
                      {t('fileManager.plateSlot')} {f.slot_id}
                    </span>
                  )}
                  <span className="text-bambu-gray">{f.type}</span>
                  <span className="text-bambu-gray">{resolveSpoolColorName(null, f.color) || f.color}</span>
                  <span className="ml-auto text-bambu-gray">
                    {f.used_grams.toFixed(1)} {t('common.gramShort')}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

interface ModalProps {
  fileId: number;
  filename: string;
  onClose: () => void;
}

/** Full-screen modal wrapping ``LibraryPlateGallery`` for list-mode rows. */
export function LibraryPlateGalleryModal({ fileId, filename, onClose }: ModalProps) {
  const { t } = useTranslation();

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl w-full max-w-3xl shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-bambu-dark-tertiary">
          <h3 className="text-sm font-semibold text-white truncate">
            {t('fileManager.plateGallery')}: <span className="font-normal text-bambu-gray">{filename}</span>
          </h3>
          <button
            type="button"
            onClick={onClose}
            className="text-bambu-gray hover:text-white"
            aria-label={t('common.close')}
          >
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="p-4">
          <LibraryPlateGallery fileId={fileId} />
        </div>
      </div>
    </div>
  );
}
