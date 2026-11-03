import { useTranslation } from 'react-i18next';
import { useAuth } from '../contexts/AuthContext';
import { queueWaitText, type WaitingItem } from '../utils/queueWait';

/** Navigation only: the addressed panel rechecks rights and never starts a preview. */
export function QueueRuntimeLink({ printerId, className, compact = false }: { printerId?: number | null; className?: string; compact?: boolean }) {
  const { t } = useTranslation();
  const { hasAllPermissions } = useAuth();
  if (!hasAllPermissions('system:read', 'printers:read', 'queue:read_all', 'archives:read_all')) {
    return <span className={className} title={t('queueCard.runtimeOperator')}>{compact ? '!' : t('queueCard.runtimeOperator')}</span>;
  }
  return <a className={`text-bambu-green hover:underline ${className ?? ''}`}
    href={`/system${printerId != null ? `?printer_id=${printerId}` : ''}#print-state`}
    title={t('queueCard.runtimeCheck')} aria-label={t('queueCard.runtimeCheck')}
    onClick={event => event.stopPropagation()}>{compact ? '↗' : t('queueCard.runtimeCheck')}</a>;
}

export function QueueWaitReason({ item, printerId }: { item: WaitingItem; printerId?: number | null }) {
  const { t } = useTranslation();
  const text = queueWaitText(item, t);
  return <>{text}{item.waiting_reason_code === 'dispatch_runtime_conflict'
    && <> <QueueRuntimeLink printerId={printerId ?? item.printer_id} /></>}</>;
}
