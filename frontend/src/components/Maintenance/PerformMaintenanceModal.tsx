import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Wrench } from 'lucide-react';
import { Modal } from '../Modal';
import { Button } from '../Button';

// The server's limit (PerformMaintenanceRequest.notes); spaces around the text do not count.
export const MAINTENANCE_NOTES_MAX = 10000;

interface PerformMaintenanceModalProps {
  taskName: string;
  printerName: string;
  busy?: boolean;
  onConfirm: (notes: string | undefined) => void;
  onCancel: () => void;
}

/**
 * A reset asks first and takes an optional comment (N09, upstream 8afcb3fbd). Esc closes
 * the empty dialog, and is held while a comment is typed so the text is not lost; a click
 * outside never closes it (our modal rule).
 */
export function PerformMaintenanceModal({ taskName, printerName, busy = false, onConfirm, onCancel }: PerformMaintenanceModalProps) {
  const { t } = useTranslation();
  const [notes, setNotes] = useState('');
  const typed = notes.trim() !== '';

  return (
    <Modal
      onClose={onCancel}
      title={t('maintenance.performDialog.title')}
      icon={<Wrench className="h-5 w-5 text-bambu-green" />}
      closeDisabled={busy || typed}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onCancel} disabled={busy}>
            {t('common.cancel')}
          </Button>
          <Button onClick={() => onConfirm(notes.trim() || undefined)} disabled={busy}>
            {t('maintenance.performDialog.confirm')}
          </Button>
        </div>
      }
    >
      <div className="space-y-3">
        <p className="text-sm text-bambu-gray">
          {t('maintenance.performDialog.message', { task: taskName, printer: printerName })}
        </p>
        <label className="block text-sm text-bambu-gray">
          {t('maintenance.performDialog.notes')}
          <textarea
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
            maxLength={MAINTENANCE_NOTES_MAX}
            rows={4}
            placeholder={t('maintenance.performDialog.notesPlaceholder')}
            className="mt-1 block w-full rounded-lg border border-bambu-dark-tertiary bg-bambu-dark p-2 text-white focus:border-bambu-green focus:outline-none"
          />
        </label>
      </div>
    </Modal>
  );
}
