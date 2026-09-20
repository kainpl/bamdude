import { useTranslation } from 'react-i18next';
import { AlertTriangle } from 'lucide-react';
import { Button } from './Button';
import { Modal } from './Modal';

interface AlertModalProps {
  title: string;
  message: string;
  /** Optional secondary line shown above the message — e.g. the file the
   *  alert is about. */
  subtitle?: string;
  closeText?: string;
  variant?: 'error' | 'warning';
  onClose: () => void;
}

/**
 * A small acknowledge-only modal: title, message, single Close button.
 *
 * Use it to surface something the user must read and act on, where a toast
 * would auto-dismiss before it can be read (e.g. a slicer rejection message).
 * For confirm/cancel decisions use ConfirmModal instead. Closes only through
 * its button or Esc; mounted last, it sits above whatever raised it.
 */
export function AlertModal({
  title,
  message,
  subtitle,
  closeText,
  variant = 'error',
  onClose,
}: AlertModalProps) {
  const { t } = useTranslation();
  const resolvedCloseText = closeText ?? t('common.close');

  const iconColor = variant === 'warning' ? 'text-yellow-600 dark:text-yellow-400' : 'text-red-600 dark:text-red-400';

  return (
    <Modal onClose={onClose} hideClose ariaLabel={title} size="md">
      <div className="p-4">
        <div className="flex items-start gap-4">
          <div className={`p-2 rounded-full bg-bambu-dark ${iconColor}`}>
            <AlertTriangle className="w-6 h-6" />
          </div>
          <div className="flex-1 min-w-0">
            <h3 className="text-lg font-semibold text-white mb-1">{title}</h3>
            {subtitle && <p className="text-xs text-bambu-gray mb-2 truncate">{subtitle}</p>}
            <p className="text-bambu-gray text-sm whitespace-pre-line break-words">{message}</p>
          </div>
        </div>
        <div className="flex mt-4">
          <Button onClick={onClose} className="flex-1">
            {resolvedCloseText}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
