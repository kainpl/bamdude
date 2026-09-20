import { PackageX } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { PlateDefects } from '../hooks/usePlateDefects';
import { DefectsFields } from './DefectsFields';

/**
 * The collapsed «Defects in this print» row drawn above a «Clear plate» /
 * «Repeat print» pair; renders nothing until the print can be graded. The
 * state lives in `hooks/usePlateDefects`, so the pair's mutations can carry
 * the numbers the row collected.
 */
export function PlateDefectsRow({ defects, className = '' }: { defects: PlateDefects; className?: string }) {
  const { t } = useTranslation();
  const { waiting } = defects;
  if (!waiting || !defects.gradable) return null;
  return (
    <div className={className}>
      <button
        type="button"
        onClick={defects.toggle}
        data-testid="plate-defects-toggle"
        className="text-xs text-bambu-gray hover:text-bambu-gray-light dark:hover:text-white inline-flex items-center gap-1"
      >
        <PackageX className="w-3.5 h-3.5" />
        {defects.shown > 0
          ? t('queue.defects.toggleWithCount', { count: defects.shown })
          : t('queue.defects.toggle')}
      </button>
      {defects.open && (
        <div className="mt-2" data-testid="plate-defects-fields">
          <DefectsFields
            parts={waiting.parts}
            values={defects.values}
            onChange={defects.setPart}
            quantity={waiting.quantity}
            flat={defects.flat}
            onFlatChange={defects.setFlat}
          />
        </div>
      )}
    </div>
  );
}
