import { useQuery } from '@tanstack/react-query';
import { HelpCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { api, type AutoStockSpoolPolicy, type StockSpoolGroup } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { Select } from './Select';

const groupKey = (g: StockSpoolGroup) => JSON.stringify([
  g.material, g.rgba, g.brand, g.subtype, g.filament_family_id, g.label_weight,
]);

export function AutoStockSpoolFields({ value, onChange }: {
  value: AutoStockSpoolPolicy;
  onChange: (value: AutoStockSpoolPolicy) => void;
}) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const canEdit = hasPermission('inventory:update');
  const { data = [], isLoading, isError } = useQuery({
    queryKey: ['inventory-spools', 'auto-stock-groups'],
    queryFn: api.getAutoStockSpoolGroups,
    enabled: hasPermission('inventory:read'),
  });
  const groups = [...data];
  // An exhausted group stays selected. Choosing a different group behind the
  // operator's back would silently change the printer's physical assumption.
  if (value.group && !groups.some(g => groupKey(g) === groupKey(value.group!))) {
    groups.push({ ...value.group, available_count: 0 });
  }
  const label = (g: StockSpoolGroup & { available_count: number }) =>
    `${[g.brand, g.material, g.subtype].filter(Boolean).join(' ')} · #${g.rgba.slice(0, 6)} · ${g.filament_family_id || '—'} · ${g.label_weight} g · ${t('printers.autoStock.available', { count: g.available_count })}`;

  return (
    <div className="border-t border-bambu-dark-tertiary pt-3 space-y-2">
      <label className="flex items-center gap-2 cursor-pointer">
        <input type="checkbox" checked={value.enabled} disabled={!canEdit}
          onChange={e => onChange({ ...value, enabled: e.target.checked })}
          className="accent-bambu-green w-4 h-4 rounded border-bambu-dark-tertiary bg-bambu-dark text-bambu-green focus:ring-bambu-green" />
        <span className="text-sm text-white">{t('printers.autoStock.title')}</span>
      </label>
      <p className="text-xs text-bambu-gray ml-6">{t('printers.autoStock.description')}</p>
      <div className="ml-6 space-y-2">
        <label className="block text-xs text-bambu-gray">
          {t('printers.autoStock.group')}
          <Select className="w-full mt-1 min-w-0" disabled={!canEdit || isLoading}
            required={value.enabled} value={value.group ? groupKey(value.group) : ''}
            onChange={e => {
              const selected = groups.find(g => groupKey(g) === e.target.value);
              const group = selected ? {
                material: selected.material, rgba: selected.rgba, brand: selected.brand,
                subtype: selected.subtype, filament_family_id: selected.filament_family_id,
                label_weight: selected.label_weight,
              } : null;
              onChange({ ...value, group });
            }}>
            <option value="">{isLoading ? t('common.loading') : t('printers.autoStock.chooseGroup')}</option>
            {groups.map(g => <option key={groupKey(g)} value={groupKey(g)}>{label(g)}</option>)}
          </Select>
        </label>
        {isError && <p role="alert" className="text-xs text-red-400">{t('printers.autoStock.loadFailed')}</p>}
        {!isLoading && !isError && data.length === 0 && (
          <p className="text-xs text-bambu-gray">{t('printers.autoStock.noStock')}</p>
        )}
        {!canEdit && <p className="text-xs text-bambu-gray">{t('printers.autoStock.permission')}</p>}
        <details className="text-xs text-bambu-gray">
          <summary className="cursor-pointer flex items-center gap-1 text-bambu-green">
            <HelpCircle className="w-3.5 h-3.5" />{t('printers.autoStock.helpTitle')}
          </summary>
          <p className="mt-2">{t('printers.autoStock.help')}</p>
          <p className="mt-2">{t('printers.autoStock.externalHelp')}</p>
        </details>
      </div>
    </div>
  );
}
