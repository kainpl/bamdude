/** Read-only plate preview for a library file or an archive.
 *
 * Deliberately has no skip action. Skipping needs a live printer, an MQTT
 * client and a confirmation step — that is SkipObjectsModal, which already
 * exists. What this adds is the answer to "what is on this plate" before the
 * print starts, and an explanation of whether skipping will work at all.
 *
 * The banner is the point of the design: hiding the button when skipping is
 * unsupported would teach the operator nothing, while naming the slicer setting
 * that turned it off is actionable. Presence of an object list and permission
 * to skip are independent axes — OrcaSlicer ships `exclude_object=false` by
 * default while still labelling every instance in the gcode.
 *
 * Geometry and marker maths come from ./plateDialogLayout, shared with
 * SkipObjectsModal so the two dialogs cannot drift apart.
 */

import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { X, Loader2, Box, Maximize2, AlertTriangle, CheckCircle2 } from 'lucide-react';
import { api } from '../api/client';
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

interface PlateObjectsPreviewModalProps {
  source: 'library' | 'archive';
  id: number;
  isOpen: boolean;
  onClose: () => void;
}
// No `isMultiPlate` prop: the modal fetches /plates for library files anyway,
// to choose its opening plate, so it already knows. A prop would be a second
// source for a question the component can answer itself — and callers that got
// it wrong would silently lose the plate strip.

export function PlateObjectsPreviewModal({ source, id, isOpen, onClose }: PlateObjectsPreviewModalProps) {
  const { t } = useTranslation();
  const [plate, setPlate] = useState(1);
  const [enlarged, setEnlarged] = useState(false);

  // Library files open on the FIRST PLATE THAT HAS OBJECTS, not blindly on
  // plate 1: a file whose parts sit on plate 3 would otherwise greet you with an
  // empty plate and no hint that the interesting one is two clicks away.
  // Fetched unconditionally, not only for multi-plate files — this is a LAN
  // request against a cache LibraryPlateGallery usually warmed already, and the
  // right opening plate is worth more than the round trip.
  const { data: plateList } = useQuery({
    queryKey: ['library-plates', id],
    queryFn: () => api.getLibraryFilePlates(id),
    enabled: isOpen && source === 'library',
  });

  const autoPlated = useRef(false);
  useEffect(() => {
    autoPlated.current = false;
    setPlate(1);
    setEnlarged(false);
  }, [id, source]);
  useEffect(() => {
    // Once, on first arrival. Re-running would yank the plate back from under
    // anyone who has since clicked the strip.
    if (autoPlated.current || !plateList?.plates?.length) return;
    autoPlated.current = true;
    const first = plateList.plates.find((p) => (p.object_count ?? 0) > 0);
    if (first) setPlate(first.index);
  }, [plateList]);

  const { data } = useQuery({
    queryKey: ['plate-objects', source, id, plate],
    queryFn: () => api.getPlateObjects(source, id, plate),
    enabled: isOpen,
  });

  if (!isOpen) return null;

  // PlateMarkers wants PlateObject, whose `skipped` drives the red/green
  // colouring. Nothing here is ever skipped — this dialog cannot skip.
  const objects: PlateObject[] = (data?.objects ?? []).map((o) => ({
    id: o.id,
    name: o.name,
    x: o.x,
    y: o.y,
    norm: o.norm,
    marker: o.marker,
    skipped: false,
  }));

  const plateIndex = data?.plate_index ?? plate;
  const imageUrl =
    source === 'library'
      ? `/api/v1/library/files/${id}/plate-thumbnail/${plateIndex}?view=top`
      : `/api/v1/archives/${id}/plate-thumbnail/${plateIndex}?view=top`;
  // Both thumbnail routes are unauthenticated — an <img> cannot send headers —
  // so unlike the live camera path there is no stream token to thread here.

  const showStrip = source === 'library' && (plateList?.plates.length ?? 0) > 1;

  return (
    <>
      <div
        className="fixed inset-0 z-50 flex items-center justify-center p-4"
        onClick={onClose}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onClose();
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
              <Box className="w-4 h-4 text-bambu-green" />
              <span className="text-sm font-medium text-gray-900 dark:text-white">
                {t('library.plateObjects.title')}
              </span>
              {objects.length > 0 && (
                <span className="text-xs text-gray-500 dark:text-bambu-gray">
                  {t('library.plateObjects.objectCount', { count: objects.length })}
                </span>
              )}
            </div>
            <button
              onClick={onClose}
              className="p-1 text-gray-500 dark:text-bambu-gray hover:text-gray-900 dark:hover:text-white rounded transition-colors"
            >
              <X className="w-4 h-4" />
            </button>
          </div>

          {!data ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="w-5 h-5 animate-spin text-bambu-gray" />
            </div>
          ) : (
            // min-h-0 matters: a flex child defaults to min-height:auto, which
            // refuses to shrink below its content and would push the list's own
            // scroll container past the bottom of a fixed-height dialog.
            <div className="flex flex-col flex-1 min-h-0 overflow-hidden">
              {/* Skip-availability banner. Never hidden, whichever way it reads —
                  a missing button explains nothing, "Exclude objects was off"
                  points at the switch to flip. */}
              <div
                className={`flex items-start gap-2 px-4 py-2.5 border-b border-gray-200 dark:border-bambu-dark-tertiary ${
                  data.skip_objects_supported
                    ? 'bg-green-50 dark:bg-green-500/10'
                    : 'bg-amber-50 dark:bg-amber-500/10'
                }`}
              >
                {data.skip_objects_supported ? (
                  <CheckCircle2 className="w-3.5 h-3.5 text-green-600 dark:text-bambu-green flex-shrink-0 mt-0.5" />
                ) : (
                  <AlertTriangle className="w-3.5 h-3.5 text-amber-600 dark:text-amber-400 flex-shrink-0 mt-0.5" />
                )}
                <p
                  className={`text-[11px] ${
                    data.skip_objects_supported
                      ? 'text-green-700 dark:text-green-300/90'
                      : 'text-amber-700 dark:text-amber-300/90'
                  }`}
                >
                  {data.skip_objects_supported
                    ? t('library.plateObjects.skipSupported')
                    : t('library.plateObjects.skipUnsupported')}
                </p>
              </div>

              {/* Nothing on this plate could be located in the file's object map,
                  so every marker came from markerPosition's grid fallback. The
                  picture looks like a real layout and is not one. */}
              {data.positions_approximate && objects.length > 0 && (
                <div className="flex items-start gap-2 px-4 py-2 bg-blue-50 dark:bg-blue-500/10 border-b border-gray-200 dark:border-bambu-dark-tertiary">
                  <AlertTriangle className="w-3.5 h-3.5 text-blue-500 dark:text-blue-400 flex-shrink-0 mt-0.5" />
                  <p className="text-[11px] text-blue-600 dark:text-blue-300/90">
                    {t('library.plateObjects.approximate')}
                  </p>
                </div>
              )}

              {/* Plate strip — library only, and only when there is a choice. */}
              {showStrip && (
                <div className="flex items-center gap-1.5 px-4 py-2 border-b border-gray-200 dark:border-bambu-dark-tertiary overflow-x-auto">
                  {plateList?.plates.map((p) => (
                    <button
                      key={p.index}
                      type="button"
                      onClick={() => setPlate(p.index)}
                      className={`px-2.5 py-1 text-[11px] rounded-md whitespace-nowrap transition-colors ${
                        p.index === plateIndex
                          ? 'bg-bambu-green/20 text-bambu-green border border-bambu-green/40'
                          : 'bg-gray-100 dark:bg-bambu-dark-secondary text-gray-600 dark:text-bambu-gray border border-transparent hover:bg-gray-200 dark:hover:bg-bambu-dark'
                      }`}
                    >
                      {t('library.plateObjects.plate', { index: p.index })}
                      {(p.object_count ?? 0) > 0 && (
                        <span className="ml-1.5 opacity-60">{p.object_count}</span>
                      )}
                    </button>
                  ))}
                </div>
              )}

              {/* Content: image + list side by side */}
              <div className="flex flex-1 min-h-0 overflow-hidden">
                <div
                  style={{ width: COLUMN_PX }}
                  className="flex-shrink-0 p-4 border-r border-gray-200 dark:border-bambu-dark-tertiary bg-gray-50 dark:bg-bambu-dark-secondary overflow-y-auto"
                >
                  {/* No top view means no image at all — never markers over the
                      3/4 plate_N.png render, where they would sit convincingly
                      on the wrong parts. */}
                  {data.has_top_view ? (
                    <div className="relative cursor-pointer group" onClick={() => setEnlarged(true)}>
                      <img
                        src={imageUrl}
                        alt={t('library.plateObjects.title')}
                        className="w-full aspect-square object-contain rounded-lg bg-gray-900 dark:bg-gray-900 border border-gray-300 dark:border-gray-600"
                      />
                      <div className="absolute top-2 right-2 p-1 bg-black/60 rounded opacity-0 group-hover:opacity-100 transition-opacity">
                        <Maximize2 className="w-3.5 h-3.5 text-white" />
                      </div>
                      {/* Markers with no canSkip/onSkip render disabled — the
                          read-only branch PlateMarkers already had. */}
                      <PlateMarkers objects={objects} t={t} />
                    </div>
                  ) : (
                    <div className="w-full aspect-square rounded-lg bg-gray-100 dark:bg-bambu-dark flex flex-col items-center justify-center gap-2 px-4 text-center">
                      <Box className="w-8 h-8 text-gray-300 dark:text-bambu-gray/30" />
                      <p className="text-[11px] text-gray-500 dark:text-bambu-gray">
                        {t('library.plateObjects.noImage')}
                      </p>
                    </div>
                  )}
                </div>

                {/* Object list. Fills DOWN a column then wraps to the NEXT column
                    to the right — see plateDialogLayout for why grid-flow-col and
                    an auto-fill row template produce that order. */}
                {objects.length === 0 ? (
                  <div className="flex-1 flex items-center justify-center px-4 text-center">
                    <p className="text-sm text-bambu-gray">{t('library.plateObjects.empty')}</p>
                  </div>
                ) : (
                  <div
                    style={{ width: LIST_COLUMN_PX }}
                    className={`flex-shrink-0 grid grid-flow-col ${LIST_ROW_HEIGHT} ${LIST_COLUMN} overflow-x-auto overflow-y-hidden`}
                  >
                    {objects.map((obj) => (
                      <div
                        key={obj.id}
                        className="flex items-center gap-3 px-4 border-b border-r border-gray-200 dark:border-bambu-dark-tertiary/50 hover:bg-gray-50 dark:hover:bg-bambu-dark/50"
                      >
                        <div className="w-12 h-12 flex-shrink-0 rounded-lg flex flex-col items-center justify-center bg-green-100 dark:bg-bambu-green/20 border border-green-300 dark:border-bambu-green/40">
                          <span className="text-lg font-mono font-bold text-green-600 dark:text-bambu-green">
                            {obj.id}
                          </span>
                          <span className="text-[8px] uppercase tracking-wider text-green-500/60 dark:text-bambu-green/60">
                            ID
                          </span>
                        </div>
                        <div className="flex-1 min-w-0">
                          <span className="block text-sm truncate text-gray-900 dark:text-white">{obj.name}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Enlarged lightbox. Square is load-bearing, not cosmetic: PlateMarkers
          positions every marker as a percentage of THIS box while the image
          inside is object-contain. A rectangular box letterboxes the image while
          the markers keep using the full box, and they drift off the plate.
          Hence aspect-square with only a max-WIDTH cap in vmin — a max-height
          would squash one axis independently and break exactly that. */}
      {enlarged && data?.has_top_view && (
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
          <div
            style={{ width: PLATE_IMAGE_PX * LIGHTBOX_SCALE }}
            className="relative aspect-square max-w-[90vmin]"
            onClick={(e) => e.stopPropagation()}
          >
            <img
              src={imageUrl}
              alt={t('library.plateObjects.title')}
              className="w-full h-full object-contain rounded-lg bg-gray-900"
            />
            <PlateMarkers objects={objects} t={t} />
          </div>
        </div>
      )}
    </>
  );
}
