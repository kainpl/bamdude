import { useTranslation } from 'react-i18next';
import { AlertTriangle, Loader2 } from 'lucide-react';
import { Button } from './Button';
import { Modal } from './Modal';

interface ConfirmModalProps {
  title: string;
  message: string;
  confirmText?: string;
  cancelText?: string;
  cancelVariant?: 'primary' | 'secondary' | 'danger' | 'ghost';
  cardClassName?: string;
  variant?: 'danger' | 'warning' | 'default';
  isLoading?: boolean;
  loadingText?: string;
  children?: React.ReactNode;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Confirm / cancel. Closes ONLY through its two buttons or Esc — the shell's
 * backdrop does nothing, and Esc is swallowed while `isLoading` so a purge
 * cannot be cancelled mid-flight. No header X: the Cancel button is the exit.
 * Stacking over another modal needs nothing — the shell orders by mount.
 */
export function ConfirmModal({
  title,
  message,
  confirmText,
  cancelText,
  cancelVariant,
  cardClassName,
  variant = 'default',
  isLoading = false,
  loadingText,
  children,
  onConfirm,
  onCancel,
}: ConfirmModalProps) {
  const { t } = useTranslation();
  const resolvedConfirmText = confirmText ?? t('common.confirm');
  const resolvedCancelText = cancelText ?? t('common.cancel');
  const resolvedLoadingText = loadingText ?? t('common.loading');

  const variantStyles = {
    danger: {
      icon: 'text-red-600 dark:text-red-400',
      button: 'bg-red-500 hover:bg-red-600',
    },
    warning: {
      icon: 'text-yellow-600 dark:text-yellow-400',
      button: 'bg-yellow-500 hover:bg-yellow-600 text-black',
    },
    default: {
      icon: 'text-bambu-green',
      button: 'bg-bambu-green hover:bg-bambu-green-dark',
    },
  };

  const styles = variantStyles[variant];

  return (
    <Modal onClose={onCancel} hideClose ariaLabel={title} closeDisabled={isLoading} size="md" panelClassName={cardClassName}>
      <div className="p-4">
        <div className="flex items-start gap-4">
          <div className={`p-2 rounded-full bg-bambu-dark ${styles.icon}`}>
            <AlertTriangle className="w-6 h-6" />
          </div>
          <div className="flex-1">
            <h3 className="text-lg font-semibold text-white mb-2">{title}</h3>
            <p className="text-bambu-gray text-sm whitespace-pre-line">{message}</p>
            {children && <div className="mt-3">{children}</div>}
          </div>
        </div>
        <div className="flex gap-3 mt-4">
          <Button
            variant={cancelVariant ?? 'secondary'}
            onClick={onCancel}
            className="flex-1"
            disabled={isLoading}
          >
            {resolvedCancelText}
          </Button>
          <Button
            onClick={onConfirm}
            className={`flex-1 ${styles.button}`}
            disabled={isLoading}
          >
            {isLoading ? (
              <>
                <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                {resolvedLoadingText}
              </>
            ) : (
              resolvedConfirmText
            )}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
