import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';

/**
 * Modal that lets the user pick "apply to all N copies" vs "apply only
 * to this copy" when acting on a queue item that's part of a batch.
 *
 * Used by Cancel / Clone / Edit flows — the action label changes
 * via the three action props.  Keep semantics simple: parent owns
 * mutation logic, this component only collects the choice.
 */
export interface BatchActionDialogProps {
  open: boolean;
  onClose: () => void;
  batchSize: number;
  title: string;
  applyAllLabel: string;
  applyOneLabel: string;
  onApplyAll: () => void;
  onApplyOne: () => void;
  applyAllDanger?: boolean;  // red styling for destructive "all" (e.g. cancel)
}

export function BatchActionDialog({
  open,
  onClose,
  batchSize,
  title,
  applyAllLabel,
  applyOneLabel,
  onApplyAll,
  onApplyOne,
  applyAllDanger,
}: BatchActionDialogProps) {
  const { t } = useTranslation();
  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl w-full max-w-md"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
          <h3 className="text-white font-medium">{title}</h3>
          <button onClick={onClose} className="text-bambu-gray hover:text-white">
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="p-4 space-y-3">
          <p className="text-sm text-bambu-gray">
            {t('queueCard.batch.sizeHint', { count: batchSize })}
          </p>
          <button
            onClick={onApplyAll}
            className={
              applyAllDanger
                ? 'w-full py-2 px-3 rounded bg-red-500/20 hover:bg-red-500/30 text-red-400 text-sm font-medium transition-colors'
                : 'w-full py-2 px-3 rounded bg-bambu-green/20 hover:bg-bambu-green/30 text-bambu-green text-sm font-medium transition-colors'
            }
          >
            {applyAllLabel}
          </button>
          <button
            onClick={onApplyOne}
            className="w-full py-2 px-3 rounded bg-bambu-dark-tertiary hover:bg-bambu-dark text-white text-sm font-medium transition-colors"
          >
            {applyOneLabel}
          </button>
          <button
            onClick={onClose}
            className="w-full py-1.5 px-3 rounded text-bambu-gray hover:text-white text-sm transition-colors"
          >
            {t('common.cancel')}
          </button>
        </div>
      </div>
    </div>
  );
}
