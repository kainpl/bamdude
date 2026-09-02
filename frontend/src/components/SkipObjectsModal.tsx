import { useState } from 'react';
import { useQuery, useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { X, Loader2, Monitor, Box, Maximize2, AlertTriangle } from 'lucide-react';
import { api, withStreamToken } from '../api/client';
import { useToast } from '../contexts/ToastContext';
import { useAuth } from '../contexts/AuthContext';
import { ConfirmModal } from './ConfirmModal';
import { PlateMarkers } from './PlateObjectMarkers';
import {
  COLUMN_PX,
  DIALOG_FRAME,
  DIALOG_WIDTH_PX,
  LIGHTBOX_SCALE,
  LIST_COLUMN,
  LIST_COLUMN_PX,
  LIST_ROW_HEIGHT,
  PLATE_IMAGE_PX,
  type PlateObject,
} from './plateDialogLayout';

// Custom Skip Objects icon - arrow jumping over boxes
export const SkipObjectsIcon = ({ className }: { className?: string }) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}>
    {/* Three boxes at the bottom */}
    <rect x="2" y="15" width="5" height="5" rx="0.5" />
    <rect x="9.5" y="15" width="5" height="5" rx="0.5" fill="currentColor" opacity="0.3" />
    <rect x="17" y="15" width="5" height="5" rx="0.5" />
    {/* Curved arrow jumping over first box */}
    <path d="M4 12 C4 6, 14 6, 14 12" />
    <polyline points="12,10 14,12 12,14" />
  </svg>
);

interface SkipObjectsModalProps {
  printerId: number;
  isOpen: boolean;
  onClose: () => void;
}

export function SkipObjectsModal({ printerId, isOpen, onClose }: SkipObjectsModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const [pendingSkip, setPendingSkip] = useState<{ id: number; name: string } | null>(null);
  const [enlarged, setEnlarged] = useState(false);

  const { data: status } = useQuery({
    queryKey: ['printerStatus', printerId],
    queryFn: () => api.getPrinterStatus(printerId),
    refetchInterval: 30000,
    enabled: isOpen,
  });

  const { data: objectsData, refetch: refetchObjects } = useQuery({
    queryKey: ['printableObjects', printerId],
    queryFn: () => api.getPrintableObjects(printerId),
    enabled: isOpen,
    refetchInterval: isOpen ? 5000 : false,
  });

  const skipObjectsMutation = useMutation({
    mutationFn: (objectIds: number[]) => api.skipObjects(printerId, objectIds),
    onSuccess: (data) => {
      showToast(data.message || t('printers.skipObjects.objectsSkipped'));
      setPendingSkip(null);
      refetchObjects();
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToSkipObjects'), 'error'),
  });

  // Skipping the wrong object ruins the print, so a marker is only live when
  // the object isn't already skipped, the user may control printers, and no
  // skip is mid-flight. Shared by both plate views.
  const canSkipObject = (obj: PlateObject) =>
    !obj.skipped && hasPermission('printers:control') && !skipObjectsMutation.isPending;

  if (!isOpen) return null;

  return (
    <>
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      onClick={onClose}
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          if (enlarged) setEnlarged(false);
          else onClose();
        }
      }}
      tabIndex={-1}
      ref={(el) => el?.focus()}
    >
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm z-0" />
      {/* Modal */}
      <div
        style={{ width: DIALOG_WIDTH_PX }}
        className={`relative z-10 bg-white dark:bg-bambu-dark border border-gray-200 dark:border-bambu-dark-tertiary rounded-xl shadow-2xl ${DIALOG_FRAME} flex flex-col overflow-hidden`}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200 dark:border-bambu-dark-tertiary bg-gray-50 dark:bg-bambu-dark">
          <div className="flex items-center gap-2">
            <SkipObjectsIcon className="w-4 h-4 text-bambu-green" />
            <span className="text-sm font-medium text-gray-900 dark:text-white">{t('printers.skipObjects.title')}</span>
          </div>
          <button
            onClick={onClose}
            className="p-1 text-gray-500 dark:text-bambu-gray hover:text-gray-900 dark:hover:text-white rounded transition-colors"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {!objectsData ? (
          <div className="flex items-center justify-center py-12">
            <Loader2 className="w-5 h-5 animate-spin text-bambu-gray" />
          </div>
        ) : objectsData.objects.length === 0 ? (
          <div className="text-center py-8 px-4 text-bambu-gray">
            <p className="text-sm">{t('printers.noObjectsFound')}</p>
            <p className="text-xs mt-1 opacity-70">{t('printers.objectsLoadedOnPrintStart')}</p>
          </div>
        ) : (
          // min-h-0 matters: a flex child defaults to min-height:auto, which
          // refuses to shrink below its content and would push the list's own
          // scroll container past the bottom of a fixed-height dialog.
          <div className="flex flex-col flex-1 min-h-0 overflow-hidden">
            {/* Info Banner */}
            <div className="flex items-center gap-3 px-4 py-2.5 bg-blue-50 dark:bg-blue-500/10 border-b border-gray-200 dark:border-bambu-dark-tertiary">
              <div className="flex-shrink-0 w-8 h-8 rounded-lg bg-blue-100 dark:bg-blue-500/20 flex items-center justify-center">
                <Monitor className="w-4 h-4 text-blue-500 dark:text-blue-400" />
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-xs text-blue-600 dark:text-blue-300">{t('printers.skipObjects.matchIdsInfo')}</p>
                <p className="text-[10px] text-blue-500/70 dark:text-blue-300/60">{t('printers.skipObjects.printerShowsIds')}</p>
              </div>
              <div className="flex-shrink-0 text-xs text-gray-500 dark:text-bambu-gray">
                {objectsData.skipped_count}/{objectsData.total} {t('printers.skipObjects.skipped')}
              </div>
            </div>

            {/* Nothing on this plate could be located in the file's object map,
                so every marker below came from markerPosition's grid fallback.
                The picture looks like a real layout and is not one — saying so
                is better than implying a precision we do not have. */}
            {objectsData.positions_approximate && (
              <div className="flex items-start gap-2 px-4 py-2 bg-amber-50 dark:bg-amber-500/10 border-b border-gray-200 dark:border-bambu-dark-tertiary">
                <AlertTriangle className="w-3.5 h-3.5 text-amber-600 dark:text-amber-400 flex-shrink-0 mt-0.5" />
                <p className="text-[11px] text-amber-700 dark:text-amber-300/90">
                  {t('printers.skipObjects.approximatePositions')}
                </p>
              </div>
            )}

            {/* Content: Image + List side by side */}
            <div className="flex flex-1 min-h-0 overflow-hidden">
              {/* Left: Preview Image with object markers.
                  Column is w-96 so the square plate renders at 352px (384 minus
                  the p-4 gutters) — double the old 176px. The markers are
                  deliberately NOT scaled with it: they're fixed w-6 h-6 and
                  placed by percentage, so doubling the plate doubles the gap
                  between them while each stays the same size. That's the whole
                  point — on a busy plate the IDs used to sit on top of each
                  other. Keep them fixed-size if this ever gets resized again. */}
              <div
                style={{ width: COLUMN_PX }}
                className="flex-shrink-0 p-4 border-r border-gray-200 dark:border-bambu-dark-tertiary bg-gray-50 dark:bg-bambu-dark-secondary overflow-y-auto"
              >
                <div className="relative cursor-pointer group" onClick={() => setEnlarged(true)}>
                  {status?.cover_url ? (
                    <img
                      src={withStreamToken(`${status.cover_url}?view=top`)}
                      alt={t('printers.printPreview')}
                      className="w-full aspect-square object-contain rounded-lg bg-gray-900 dark:bg-gray-900 border border-gray-300 dark:border-gray-600"
                    />
                  ) : (
                    <div className="w-full aspect-square rounded-lg bg-gray-100 dark:bg-bambu-dark flex items-center justify-center">
                      <Box className="w-8 h-8 text-gray-300 dark:text-bambu-gray/30" />
                    </div>
                  )}
                  {/* Enlarge hint */}
                  <div className="absolute top-2 right-2 p-1 bg-black/60 rounded opacity-0 group-hover:opacity-100 transition-opacity">
                    <Maximize2 className="w-3.5 h-3.5 text-white" />
                  </div>
                  {/* Object ID markers — see PlateMarkers for why they are a
                      fixed size and how a click reaches the confirm dialog. */}
                  <PlateMarkers
                    objects={objectsData.objects}
                    canSkip={canSkipObject}
                    onSkip={setPendingSkip}
                    t={t}
                  />
                  {/* Object count overlay */}
                  <div className="absolute bottom-2 right-2 px-2 py-1 bg-white/90 dark:bg-black/80 rounded text-[10px] text-gray-700 dark:text-white shadow-sm">
                    {t('printers.skipObjects.activeCount', { count: objectsData.objects.filter(o => !o.skipped).length })}
                  </div>
                </div>
              </div>

              {/* Right: Object List with prominent IDs.
                  Fills DOWN a column and then wraps to the NEXT column to the
                  right, so a long plate scrolls sideways instead of turning
                  into one endless strip. `grid-flow-col` plus a row template
                  is what produces that order — a plain multi-column grid would
                  read left-to-right and put object 2 beside object 1.
                  Row count is implicit: `auto-fill` fits as many LIST_ROW_HEIGHT
                  rows as the dialog's height allows, so taller dialog = longer
                  columns = less sideways scrolling, with no JS measuring. */}
              <div
                style={{ width: LIST_COLUMN_PX }}
                className={`flex-shrink-0 grid grid-flow-col ${LIST_ROW_HEIGHT} ${LIST_COLUMN} overflow-x-auto overflow-y-hidden`}
              >
                {objectsData.objects.map((obj) => (
                  <div
                    key={obj.id}
                    className={`
                      flex items-center gap-3 px-4 border-b border-r border-gray-200 dark:border-bambu-dark-tertiary/50
                      ${obj.skipped ? 'bg-red-50 dark:bg-red-500/10' : 'hover:bg-gray-50 dark:hover:bg-bambu-dark/50'}
                    `}
                  >
                    {/* Large prominent ID badge */}
                    <div className={`
                      w-12 h-12 flex-shrink-0 rounded-lg flex flex-col items-center justify-center
                      ${obj.skipped
                        ? 'bg-red-100 dark:bg-red-500/20 border border-red-300 dark:border-red-500/40'
                        : 'bg-green-100 dark:bg-bambu-green/20 border border-green-300 dark:border-bambu-green/40'}
                    `}>
                      <span className={`text-lg font-mono font-bold ${obj.skipped ? 'text-red-500 dark:text-red-400' : 'text-green-600 dark:text-bambu-green'}`}>
                        {obj.id}
                      </span>
                      <span className={`text-[8px] uppercase tracking-wider ${obj.skipped ? 'text-red-700/70 dark:text-red-400/60' : 'text-green-500/60 dark:text-bambu-green/60'}`}>
                        ID
                      </span>
                    </div>

                    {/* Object name and status */}
                    <div className="flex-1 min-w-0">
                      <span className={`block text-sm truncate ${obj.skipped ? 'text-red-500 dark:text-red-400 line-through' : 'text-gray-900 dark:text-white'}`}>
                        {obj.name}
                      </span>
                      {obj.skipped && (
                        <span className="text-[10px] text-red-700/70 dark:text-red-400/60">{t('printers.willBeSkipped')}</span>
                      )}
                    </div>

                    {/* Skip button */}
                    {!obj.skipped ? (
                      <button
                        onClick={() => setPendingSkip({ id: obj.id, name: obj.name })}
                        disabled={skipObjectsMutation.isPending || !hasPermission('printers:control')}
                        className={`px-4 py-2 text-xs font-medium rounded-lg transition-colors ${
                          !hasPermission('printers:control')
                            ? 'bg-gray-100 dark:bg-bambu-dark text-gray-400 dark:text-bambu-gray/50 cursor-not-allowed'
                            : 'bg-red-100 dark:bg-red-500/20 text-red-600 dark:text-red-400 hover:bg-red-200 dark:hover:bg-red-500/30 border border-red-300 dark:border-red-500/30'
                        }`}
                        title={!hasPermission('printers:control') ? t('printers.permission.noControl') : t('printers.skipObjects.skip')}
                      >
                        {t('printers.skipObjects.skip')}
                      </button>
                    ) : (
                      <span className="px-4 py-2 text-xs text-red-500 dark:text-red-400/70 bg-red-100 dark:bg-red-500/10 rounded-lg">
                        {t('printers.skipObjects.skipped')}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
    {pendingSkip && (
      <ConfirmModal
        variant="warning"
        title={t('printers.skipObjects.confirmTitle')}
        message={t('printers.skipObjects.confirmMessage', { name: pendingSkip.name })}
        confirmText={t('printers.skipObjects.skip')}
        isLoading={skipObjectsMutation.isPending}
        // The lightbox sits at z-60, above ConfirmModal's default z-50 — a
        // confirm raised from a marker there would render *behind* it and be
        // unreachable. Lift it over the lightbox while that view is open.
        overlayZIndex={enlarged ? 'z-[70]' : undefined}
        onConfirm={() => skipObjectsMutation.mutate([pendingSkip.id])}
        onCancel={() => setPendingSkip(null)}
      />
    )}
    {/* Enlarged lightbox overlay */}
    {enlarged && objectsData && (
      <div
        className="fixed inset-0 bg-black/90 backdrop-blur-sm flex items-center justify-center z-60"
        onClick={() => setEnlarged(false)}
      >
        <button
          onClick={() => setEnlarged(false)}
          className="absolute top-4 right-4 p-2 text-white/70 hover:text-white transition-colors"
        >
          <X className="w-6 h-6" />
        </button>
        {/* Square is load-bearing, not cosmetic: PlateMarkers positions every
            marker as a percentage of THIS box while the image inside is
            object-contain. Let the box go rectangular and the image letterboxes
            inside it while the markers keep using the full box — they drift off
            the plate. Hence `aspect-square` with only a max-WIDTH cap in vmin
            (the smaller viewport axis), so a clamp shrinks the width and the
            aspect ratio pulls the height down with it. A max-height cap would
            squash one axis independently and break exactly that. */}
        <div
          style={{ width: PLATE_IMAGE_PX * LIGHTBOX_SCALE }}
          className="relative aspect-square max-w-[90vmin]"
          onClick={(e) => e.stopPropagation()}
        >
          {status?.cover_url ? (
            <img
              src={withStreamToken(`${status.cover_url}?view=top`)}
              alt={t('printers.printPreview')}
              className="w-full h-full object-contain rounded-lg bg-gray-900"
            />
          ) : (
            <div className="w-full h-full rounded-lg bg-gray-800 flex items-center justify-center">
              <Box className="w-16 h-16 text-gray-500" />
            </div>
          )}
          {/* Same interactive markers as the inline preview — see PlateMarkers. */}
          <PlateMarkers
            objects={objectsData.objects}
            canSkip={canSkipObject}
            onSkip={setPendingSkip}
            t={t}
          />
          {/* Active count badge */}
          <div className="absolute bottom-2 right-2 px-2 py-1 bg-white/90 dark:bg-black/80 rounded text-[10px] text-gray-700 dark:text-white shadow-sm">
            {t('printers.skipObjects.activeCount', { count: objectsData.objects.filter(o => !o.skipped).length })}
          </div>
        </div>
      </div>
    )}
  </>
  );
}
