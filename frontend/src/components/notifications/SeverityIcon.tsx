import { AlertTriangle, Info, OctagonAlert } from 'lucide-react';
import type { InboxSeverity } from '../../api/client';

const STYLES: Record<InboxSeverity, { Icon: typeof Info; className: string }> = {
  error: { Icon: OctagonAlert, className: 'text-red-400' },
  warning: { Icon: AlertTriangle, className: 'text-yellow-400' },
  info: { Icon: Info, className: 'text-bambu-gray' },
};

export function SeverityIcon({ severity, className = 'w-5 h-5' }: { severity: InboxSeverity; className?: string }) {
  const { Icon, className: colour } = STYLES[severity] ?? STYLES.info;
  return <Icon className={`${className} flex-shrink-0 ${colour}`} aria-hidden="true" />;
}
