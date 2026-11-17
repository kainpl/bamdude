import { useTranslation } from 'react-i18next';
import { Button } from '../Button';
import { Modal } from '../Modal';

interface DownloadCloseChoiceProps {
  /** Close the dialog and let the download go on; reopening the dialog finds it again. */
  onKeep: () => void;
  /** Ask the server to stop the download, then close. */
  onCancelDownload: () => void;
  /** Do not close. */
  onStay: () => void;
}

/**
 * Closing a dialog is not a hidden cancel: while a download is being prepared
 * the operator says which they mean.
 */
export function DownloadCloseChoice({ onKeep, onCancelDownload, onStay }: DownloadCloseChoiceProps) {
  const { t } = useTranslation();
  return (
    <Modal
      onClose={onStay}
      title={t('downloads.close.title')}
      size="sm"
      footer={
        <div className="flex flex-wrap justify-end gap-2">
          <Button variant="secondary" onClick={onStay}>
            {t('downloads.close.stay')}
          </Button>
          <Button variant="secondary" onClick={onCancelDownload}>
            {t('downloads.close.cancel')}
          </Button>
          <Button onClick={onKeep}>{t('downloads.close.keep')}</Button>
        </div>
      }
    >
      <p className="text-sm text-bambu-gray">{t('downloads.close.message')}</p>
    </Modal>
  );
}
