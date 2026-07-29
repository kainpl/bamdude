/** Object-ID markers laid over a plate image.
 *
 * Component only — the geometry knobs and ``markerPosition`` live in
 * ./plateDialogLayout so this file keeps Fast Refresh
 * (``react-refresh/only-export-components``).
 */

import { markerPosition, type PlateObject } from './plateDialogLayout';

/** Clickable object-ID markers laid over a plate image.
 *
 * Size is deliberately independent of the plate: markers are a fixed ``w-6 h-6``
 * placed by percentage, so a bigger plate spreads them further apart instead of
 * making them bulkier. Never scale them with the image — the readability win is
 * the gap between them.
 *
 * The overlay is ``pointer-events-none`` so a click on bare plate still reaches
 * whatever the parent does with it (enlarge, in the inline preview); each marker
 * opts back in and stops propagation so skipping never doubles as that action.
 *
 * ``canSkip``/``onSkip`` are optional. Omitting them — which the read-only
 * preview does — lands on the existing non-skippable branch: a disabled button
 * with the plain-name tooltip. Same render path, no second styling to keep in
 * step.
 */
export function PlateMarkers({
  objects,
  bboxAll,
  canSkip = () => false,
  onSkip = () => {},
  t,
}: {
  objects: PlateObject[];
  bboxAll?: number[] | null;
  canSkip?: (obj: PlateObject) => boolean;
  onSkip?: (target: { id: number; name: string }) => void;
  t: (key: string, options?: Record<string, unknown>) => string;
}) {
  if (objects.length === 0) return null;

  return (
    <div className="absolute inset-0 pointer-events-none">
      {objects.map((obj, idx) => {
        const { x, y } = markerPosition(obj, idx, objects.length, bboxAll);
        const skippable = canSkip(obj);

        return (
          <button
            key={obj.id}
            type="button"
            disabled={!skippable}
            onClick={(e) => {
              // Keep the click off the parent: in the inline preview that would
              // open the lightbox, in the lightbox it would close it.
              e.stopPropagation();
              onSkip({ id: obj.id, name: obj.name });
            }}
            className={`absolute flex items-center justify-center w-6 h-6 rounded-full text-[10px] font-bold shadow-lg transition-transform ${
              obj.skipped ? 'bg-red-500 text-white line-through' : 'bg-bambu-green text-black'
            } ${
              skippable
                ? 'pointer-events-auto cursor-pointer hover:scale-125 focus:outline-none focus:ring-2 focus:ring-white/80'
                : 'cursor-default'
            }`}
            style={{ left: `${x}%`, top: `${y}%`, transform: 'translate(-50%, -50%)' }}
            title={
              obj.skipped
                ? `${obj.name} — ${t('printers.willBeSkipped')}`
                : skippable
                  ? `${obj.name} — ${t('printers.skipObjects.skip')}`
                  : obj.name
            }
            aria-label={obj.name}
          >
            {obj.id}
          </button>
        );
      })}
    </div>
  );
}
