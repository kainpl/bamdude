import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';
import type { PlateRecipe } from '../../api/client';
import { getColorName } from '../../utils/colors';

const CHIP_CLASS = 'inline-flex items-center px-2 py-0.5 rounded text-xs';

/**
 * Print time as `h:mm`.
 *
 * `utils/date.ts::formatDuration` spells the same seconds "1h 30m", which is
 * right for a running job and wrong in a dense list of plates: the width of
 * the value changes with the words in it. Minutes are rounded, so a 90-second
 * plate reads `0:02` rather than `0:01` plus a hidden remainder. Same rule as
 * `OrderFigures`.
 */
function hoursMinutes(seconds: number): string {
  const minutes = Math.max(0, Math.round(seconds / 60));
  return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}`;
}

/**
 * Plates grouped per library FILE, labelled with that file's name.
 *
 * ⚠️ **The key is `library_file_id`, never `filename`.** Two library files in
 * different folders may carry the same basename — a `lids.3mf` under `v1` and
 * another under `v2` is the normal way a design gets revised — and grouping on
 * the name silently welds their plates into one block, so plate 1 appears
 * twice under one heading and the operator prints from the wrong revision.
 *
 * File order is the order the server sent, not alphabetical: the plates come
 * back grouped already, and re-sorting would shuffle a product's files on
 * every rename for no gain.
 */
function groupByFile(plates: PlateRecipe[]): { fileId: number; filename: string; plates: PlateRecipe[] }[] {
  const groups = new Map<number, { fileId: number; filename: string; plates: PlateRecipe[] }>();
  for (const plate of plates) {
    const existing = groups.get(plate.library_file_id);
    if (existing) existing.plates.push(plate);
    else groups.set(plate.library_file_id, { fileId: plate.library_file_id, filename: plate.filename, plates: [plate] });
  }
  return [...groups.values()];
}

/**
 * What this product's files actually print, one block per file.
 *
 * Read-only by design: a plate's recipe is derived from the file and the
 * composition, so it is changed by editing a part or re-slicing — never by
 * typing over the result here.
 *
 * ⚠️ **`plate_index === 0` is not "plate zero".** It means the file IS the
 * recipe — a `.gcode`, a single-plate export — and printing it as "Plate #0"
 * sends the operator looking through a slicer for a plate that does not exist.
 *
 * ⚠️ **An unassigned object is shown, not hidden.** It is a real object on the
 * plate that no part claims, so the product under-counts what it prints until
 * somebody adds a part (or an alias) with that name — which is exactly what
 * the muted chip's title says.
 *
 * Colour names come from the colour catalog (`getColorName`), never a table
 * spelled out here: suffix-matched fallbacks mislabel structurally.
 */
export function PlatesByFile({ productId }: { productId: number }) {
  const { t } = useTranslation();

  const { data: plates = [], isLoading } = useQuery({
    queryKey: ['product-plates', productId],
    queryFn: () => api.getProductPlates(productId),
    enabled: Number.isFinite(productId),
  });

  const files = groupByFile(plates);

  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold text-white">{t('products.plates.title')}</h2>

      {!isLoading && files.length === 0 && <p className="text-sm text-bambu-gray">{t('products.plates.empty')}</p>}

      {files.map((file) => (
        <div key={file.fileId} className="rounded-xl border border-bambu-dark-tertiary p-3 space-y-3">
          <h3 className="text-sm font-medium text-white truncate">{file.filename}</h3>

          {file.plates.map((plate) => (
            <div key={plate.id} className="space-y-2 border-t border-bambu-dark-tertiary pt-2 first:border-0 first:pt-0">
              <div className="flex items-center gap-2 flex-wrap text-sm">
                <span className="text-white">
                  {plate.plate_index === 0
                    ? t('products.plates.wholeFile')
                    : `${t('products.plates.plate')} #${plate.plate_index}`}
                </span>

                {plate.sliced ? (
                  <>
                    {plate.print_time_seconds != null && (
                      <span className="text-bambu-gray tabular-nums" title={t('products.plates.time')}>
                        {hoursMinutes(plate.print_time_seconds)}
                      </span>
                    )}
                    {plate.filament_used_grams != null && (
                      <span className="text-bambu-gray tabular-nums" title={t('products.plates.grams')}>
                        {`${plate.filament_used_grams.toFixed(1)} ${t('common.gramShort')}`}
                      </span>
                    )}
                  </>
                ) : (
                  <span className={`${CHIP_CLASS} bg-amber-500/20 text-amber-400`}>
                    {t('products.plates.notSliced')}
                  </span>
                )}

                {plate.materials.length > 0 && (
                  <span className="flex items-center gap-1" aria-label={t('products.plates.materials')}>
                    {plate.materials.map((material) => (
                      <span key={material} className={`${CHIP_CLASS} bg-bambu-dark-tertiary text-bambu-gray`}>
                        {material}
                      </span>
                    ))}
                  </span>
                )}

                {plate.colors.length > 0 && (
                  <span className="flex items-center gap-1" aria-label={t('products.plates.colors')}>
                    {plate.colors.map((color) => (
                      <span
                        key={color}
                        title={getColorName(color)}
                        style={{ backgroundColor: color }}
                        className="w-4 h-4 rounded-full border border-bambu-dark-tertiary"
                      />
                    ))}
                  </span>
                )}
              </div>

              {(plate.yield.length > 0 || plate.unassigned.length > 0) && (
                <div className="flex items-center gap-1 flex-wrap">
                  {plate.yield.map((entry) => (
                    <span
                      key={`y-${entry.part_id}-${entry.name}`}
                      className={`${CHIP_CLASS} bg-bambu-green/20 text-bambu-green`}
                    >
                      {`${entry.name} × ${entry.count}`}
                    </span>
                  ))}
                  {plate.unassigned.map((entry) => (
                    <span
                      key={`u-${entry.name_key}`}
                      title={t('products.plates.notInComposition')}
                      className={`${CHIP_CLASS} bg-bambu-dark-tertiary text-bambu-gray/60 border border-dashed border-bambu-gray/40`}
                    >
                      {`${entry.name_key} × ${entry.count}`}
                    </span>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      ))}
    </section>
  );
}
