import type { TFunction } from 'i18next';
import type { FirmwareBatchItem } from '../api/client';

export function firmwareBatchLabel(item: FirmwareBatchItem, t: TFunction): string {
  const label = t(`firmware.status.${item.status}`, { defaultValue: item.status });
  return item.status === 'uploading' && item.percent != null ? `${label} ${item.percent}%` : label;
}
