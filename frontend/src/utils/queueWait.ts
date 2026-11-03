import type { TFunction } from 'i18next';

export type WaitingItem = {
  waiting_reason: string | null;
  waiting_reason_code?: string | null;
  printer_id?: number | null;
};

export function queueWaitText(item: WaitingItem, t: TFunction): string | null {
  return item.waiting_reason_code === 'dispatch_runtime_conflict'
    ? t('monitor.waits.dispatch_runtime_conflict')
    : item.waiting_reason;
}
