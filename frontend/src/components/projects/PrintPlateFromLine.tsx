import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2, X } from 'lucide-react';
import { api } from '../../api/client';
import type { Order, PlateRecipe, ProjectLine } from '../../api/client';
import { getColorName } from '../../utils/colors';
import { formatDuration } from '../../utils/date';
import { Card, CardContent } from '../Card';
import { Button } from '../Button';
import { PrintModal } from '../PrintModal';

/**
 * Pick one plate of the line's product and print it.
 *
 * The minimal form of "print for this line" (design decision 2): without it
 * nothing in the UI stamps a print with the line it belongs to, and the
 * pass-1 release notes already tell operators to name the line. Pass 3
 * replaces this picker with the plan block; the `PrintModal` wiring below is
 * what it reuses.
 *
 * ⚠️ **An unsliced plate cannot be printed and its row says so** rather than
 * being hidden: a `.stl` linked to the product is a real recipe row, and
 * dropping it from this list would read as "the product has no such plate".
 * `sliced` is the server's own verdict, computed from the file's content flag
 * and not from its extension.
 */
export function PrintPlateFromLine({
  order,
  line,
  onClose,
}: {
  order: Order;
  line: ProjectLine;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [printing, setPrinting] = useState<PlateRecipe | null>(null);

  const { data: plates = [], isLoading } = useQuery({
    queryKey: ['product-plates', line.product_id],
    queryFn: () => api.getProductPlates(line.product_id),
  });

  // The line's material as the plates spell theirs — `plate_materials()`
  // upper-cases its tokens, so a lower-case "petg" typed on the line has to be
  // folded the same way or it would never highlight anything.
  const wanted = line.material?.trim().toUpperCase() ?? null;

  return (
    <>
      <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50 p-4">
        <Card className="w-full max-w-2xl max-h-[90vh] overflow-y-auto">
          <CardContent className="p-0">
            <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
              <h2 className="text-xl font-semibold text-white">{t('orders.printPlate.title')}</h2>
              <button
                type="button"
                onClick={onClose}
                aria-label={t('common.close')}
                className="text-bambu-gray hover:text-white transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="p-4 space-y-2">
              {isLoading && (
                <div className="flex items-center gap-2 text-bambu-gray text-sm">
                  <Loader2 className="w-4 h-4 animate-spin" />
                  {t('common.loading')}
                </div>
              )}

              {!isLoading && plates.length === 0 && (
                <p className="text-sm text-bambu-gray">{t('orders.printPlate.noPlates')}</p>
              )}

              {plates.map((plate) => (
                <div
                  key={plate.id}
                  className="flex items-start justify-between gap-4 flex-wrap rounded-lg border border-bambu-dark-tertiary bg-bambu-dark p-3"
                >
                  <div className="min-w-0 space-y-1">
                    <p className="text-white font-medium truncate">{plate.filename}</p>
                    <p className="text-xs text-bambu-gray">
                      {plate.plate_index === 0
                        ? t('orders.printPlate.wholeFile')
                        : t('orders.printPlate.plate', { n: plate.plate_index })}
                      {!plate.sliced && <span className="text-amber-500"> · {t('orders.printPlate.notSliced')}</span>}
                    </p>

                    {plate.materials.length > 0 && (
                      <p className="text-xs text-bambu-gray flex items-center gap-1.5 flex-wrap">
                        <span>{t('orders.printPlate.materials')}</span>
                        {plate.materials.map((material) => (
                          <span
                            key={material}
                            className={
                              material === wanted ? 'text-bambu-green font-medium' : 'text-bambu-gray-light'
                            }
                          >
                            {material}
                          </span>
                        ))}
                      </p>
                    )}

                    {plate.colors.length > 0 && (
                      <div className="flex items-center gap-1">
                        {plate.colors.map((hex) => (
                          <span
                            key={hex}
                            title={getColorName(hex)}
                            className="w-3.5 h-3.5 rounded-full border border-bambu-dark-tertiary"
                            style={{ backgroundColor: hex.startsWith('#') ? hex : `#${hex}` }}
                          />
                        ))}
                      </div>
                    )}

                    {plate.yield.length > 0 && (
                      <p className="text-xs text-bambu-gray">
                        {plate.yield.map((entry) => `${entry.name} × ${entry.count}`).join(' · ')}
                      </p>
                    )}

                    {plate.print_time_seconds != null && (
                      <p className="text-xs text-bambu-gray">{formatDuration(plate.print_time_seconds)}</p>
                    )}
                  </div>

                  <Button
                    size="sm"
                    data-testid={`plate-${plate.id}-print`}
                    disabled={!plate.sliced}
                    onClick={() => setPrinting(plate)}
                  >
                    {t('orders.printPlate.print')}
                  </Button>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>

      {printing && (
        <PrintModal
          mode="add-to-queue"
          libraryFileId={printing.library_file_id}
          archiveName={printing.filename}
          // Plate 0 is "the whole file", which is the absence of a pick — and
          // this caller HAS read the file's plates, so it may pin one.
          preselectedPlateId={printing.plate_index || undefined}
          projectId={order.id}
          projectLineId={line.id}
          onClose={() => setPrinting(null)}
          onSuccess={() => {
            setPrinting(null);
            onClose();
          }}
        />
      )}
    </>
  );
}
