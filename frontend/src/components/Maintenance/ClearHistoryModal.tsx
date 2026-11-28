import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Trash2 } from 'lucide-react';
import { Modal } from '../Modal';
import { Button } from '../Button';

// The server's bounds for «older than N days» (DELETE /maintenance/history).
const MIN_DAYS = 1;
const MAX_DAYS = 36500;

interface ClearHistoryModalProps {
  /** The printer the History tab is filtered to; none = every printer in reach. */
  printerName?: string;
  busy?: boolean;
  onConfirm: (olderThanDays: number) => void;
  onCancel: () => void;
}

/** Clear maintenance history older than N days (N09, upstream 8afcb3fbd). The tasks stay reset. */
export function ClearHistoryModal({ printerName, busy = false, onConfirm, onCancel }: ClearHistoryModalProps) {
  const { t } = useTranslation();
  const [days, setDays] = useState('90');
  const parsed = Number(days);
  const valid = Number.isInteger(parsed) && parsed >= MIN_DAYS && parsed <= MAX_DAYS;

  return (
    <Modal
      onClose={onCancel}
      title={t('maintenance.clearHistory.title')}
      icon={<Trash2 className="h-5 w-5 text-red-400" />}
      closeDisabled={busy}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onCancel} disabled={busy}>
            {t('common.cancel')}
          </Button>
          <Button variant="danger" onClick={() => onConfirm(parsed)} disabled={busy || !valid}>
            {t('maintenance.clearHistory.confirm')}
          </Button>
        </div>
      }
    >
      <div className="space-y-3">
        <label className="block text-sm text-bambu-gray">
          {t('maintenance.clearHistory.olderThan')}
          <input
            type="number"
            min={MIN_DAYS}
            max={MAX_DAYS}
            step={1}
            value={days}
            onChange={(event) => setDays(event.target.value)}
            className="mt-1 block w-32 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark p-2 text-white focus:border-bambu-green focus:outline-none"
          />
        </label>
        <p className="text-sm text-bambu-gray">
          {printerName
            ? t('maintenance.clearHistory.scopePrinter', { printer: printerName })
            : t('maintenance.clearHistory.scopeAll')}
        </p>
        <p className="text-xs text-bambu-gray">{t('maintenance.clearHistory.tasksStay')}</p>
      </div>
    </Modal>
  );
}
