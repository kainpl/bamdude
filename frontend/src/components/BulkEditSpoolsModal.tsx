import { useState, useMemo } from 'react';
import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { X, Loader2, Layers } from 'lucide-react';
import { api, type InventorySpool } from '../api/client';
import { useToast } from '../contexts/ToastContext';

interface Props {
  isOpen: boolean;
  /** Candidate spools (the currently filtered inventory). */
  spools: InventorySpool[];
  onClose: () => void;
  onSaved: () => void;
}

type TextKey = 'brand' | 'material' | 'subtype' | 'category' | 'storage_location';
type NumKey = 'cost_per_kg' | 'low_stock_threshold_pct';

const TEXT_FIELDS: { key: TextKey; labelKey: string }[] = [
  { key: 'brand', labelKey: 'inventory.brand' },
  { key: 'material', labelKey: 'inventory.material' },
  { key: 'subtype', labelKey: 'inventory.subtype' },
  { key: 'category', labelKey: 'inventory.category' },
  { key: 'storage_location', labelKey: 'inventory.storageLocation' },
];
const NUM_FIELDS: { key: NumKey; labelKey: string }[] = [
  { key: 'cost_per_kg', labelKey: 'inventory.costPerKg' },
  { key: 'low_stock_threshold_pct', labelKey: 'inventory.bulkEdit.lowStockThreshold' },
];

/** Bulk-edit selected spools. Pick which spools (default: all filtered), then
 *  tick which fields to change. A field is pre-filled only when the selection
 *  shares one value (otherwise "— varies —"). Consumed weight is never touched —
 *  the backend strips usage/identity columns too. Internal inventory only. */
export function BulkEditSpoolsModal({ isOpen, spools, onClose, onSaved }: Props) {
  const { t } = useTranslation();
  const { showToast } = useToast();

  const [selectedIds, setSelectedIds] = useState<Set<number>>(() => new Set(spools.map((s) => s.id)));
  const [enabled, setEnabled] = useState<Record<string, boolean>>({});
  const [values, setValues] = useState<Record<string, string>>({});

  const selected = useMemo(() => spools.filter((s) => selectedIds.has(s.id)), [spools, selectedIds]);

  // Shared value across the SELECTED spools for a field, or null when it varies.
  const sharedText = useMemo(() => {
    const out: Record<string, string | null> = {};
    for (const { key } of TEXT_FIELDS) {
      const distinct = new Set(selected.map((s) => (s[key] ?? '') as string));
      out[key] = distinct.size === 1 ? ([...distinct][0] as string) : null;
    }
    for (const { key } of NUM_FIELDS) {
      const distinct = new Set(selected.map((s) => (s[key] == null ? '' : String(s[key]))));
      out[key] = distinct.size === 1 ? ([...distinct][0] as string) : null;
    }
    const rgbas = new Set(selected.map((s) => (s.rgba ?? '').slice(0, 6).toUpperCase()));
    out.color = rgbas.size === 1 && [...rgbas][0] ? `#${[...rgbas][0]}` : null;
    const names = new Set(selected.map((s) => s.color_name ?? ''));
    out.color_name = names.size === 1 ? ([...names][0] as string) : null;
    return out;
  }, [selected]);

  const toggleSpool = (id: number) =>
    setSelectedIds((p) => {
      const next = new Set(p);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const allSelected = selected.length === spools.length && spools.length > 0;

  const toggle = (key: string) =>
    setEnabled((p) => {
      const next = { ...p, [key]: !p[key] };
      if (next[key]) {
        setValues((v) => ({
          ...v,
          [key]: key === 'color' ? (sharedText.color ?? '#000000') : (sharedText[key] ?? ''),
          ...(key === 'color' ? { color_name: sharedText.color_name ?? '' } : {}),
        }));
      }
      return next;
    });

  const placeholder = (key: string) =>
    sharedText[key] == null ? t('inventory.bulkEdit.varies') : (sharedText[key] as string);

  const bulkMutation = useMutation({
    mutationFn: () => {
      const fields: Record<string, unknown> = {};
      for (const { key } of TEXT_FIELDS) {
        if (!enabled[key]) continue;
        const v = (values[key] ?? '').trim();
        if (key === 'material') {
          if (v) fields[key] = v; // material is non-null; skip when blank
        } else {
          fields[key] = v || null;
        }
      }
      for (const { key } of NUM_FIELDS) {
        if (!enabled[key]) continue;
        const raw = (values[key] ?? '').trim();
        fields[key] = raw === '' ? null : Number(raw);
      }
      if (enabled.color) {
        const hex = (values.color ?? '').replace('#', '').toUpperCase();
        if (hex.length === 6) fields.rgba = `${hex}FF`;
        fields.color_name = (values.color_name ?? '').trim() || null;
      }
      return api.bulkUpdateSpools([...selectedIds], fields);
    },
    onSuccess: (updated) => {
      showToast(t('inventory.bulkEdit.saved', { count: updated.length }));
      onSaved();
      onClose();
    },
    onError: (e: Error) => showToast(e.message || t('common.error'), 'error'),
  });

  if (!isOpen) return null;

  const anyEnabled = Object.values(enabled).some(Boolean);
  const inputCls =
    'w-full px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:outline-none focus:border-bambu-green disabled:opacity-40';

  const fieldRow = (key: string, label: string, input: React.ReactNode) => (
    <div key={key} className="flex items-center gap-3 py-1.5">
      <input
        type="checkbox"
        checked={!!enabled[key]}
        onChange={() => toggle(key)}
        className="w-4 h-4 accent-bambu-green flex-shrink-0"
        aria-label={label}
      />
      <span className={`text-sm w-36 flex-shrink-0 ${enabled[key] ? 'text-white' : 'text-bambu-gray'}`}>{label}</span>
      <div className="flex-1 min-w-0">{input}</div>
    </div>
  );

  const spoolLabel = (s: InventorySpool) =>
    [s.brand, s.material, s.color_name].filter(Boolean).join(' ') || `#${s.id}`;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" onClick={onClose}>
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
      <div
        className="relative w-full max-w-2xl mx-4 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl max-h-[90vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary flex-shrink-0">
          <div className="flex items-center gap-2">
            <Layers className="w-4 h-4 text-bambu-green" />
            <h2 className="text-lg font-semibold text-white">{t('inventory.bulkEdit.title')}</h2>
            <span className="text-sm text-bambu-gray">
              {t('inventory.bulkEdit.selectedCount', { count: selected.length })}
            </span>
          </div>
          <button onClick={onClose} className="p-1 text-bambu-gray hover:text-white rounded">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="flex flex-1 min-h-0">
          {/* Selection pane */}
          <div className="w-56 flex-shrink-0 border-r border-bambu-dark-tertiary flex flex-col">
            <button
              onClick={() => setSelectedIds(allSelected ? new Set() : new Set(spools.map((s) => s.id)))}
              className="text-left px-3 py-2 text-xs text-bambu-green hover:bg-bambu-dark/50 border-b border-bambu-dark-tertiary flex-shrink-0"
            >
              {allSelected
                ? t('inventory.labels.deselectVisible')
                : t('inventory.labels.selectVisible', { count: spools.length })}
            </button>
            <div className="overflow-y-auto flex-1">
              {spools.map((s) => (
                <label
                  key={s.id}
                  className="flex items-center gap-2 px-3 py-1.5 text-xs cursor-pointer hover:bg-bambu-dark/40"
                >
                  <input
                    type="checkbox"
                    checked={selectedIds.has(s.id)}
                    onChange={() => toggleSpool(s.id)}
                    className="w-3.5 h-3.5 accent-bambu-green flex-shrink-0"
                  />
                  <span
                    className="w-3 h-3 rounded-full flex-shrink-0 border border-bambu-dark-tertiary"
                    style={{ background: s.rgba ? `#${s.rgba.slice(0, 6)}` : '#666' }}
                  />
                  <span className="truncate text-bambu-gray">{spoolLabel(s)}</span>
                </label>
              ))}
            </div>
          </div>

          {/* Field editor */}
          <div className="flex-1 min-w-0 p-4 overflow-y-auto">
            <p className="text-xs text-bambu-gray mb-3">{t('inventory.bulkEdit.hint')}</p>
            {TEXT_FIELDS.map(({ key, labelKey }) =>
              fieldRow(
                key,
                t(labelKey),
                <input
                  type="text"
                  disabled={!enabled[key]}
                  value={enabled[key] ? values[key] ?? '' : ''}
                  placeholder={placeholder(key)}
                  onChange={(e) => setValues((v) => ({ ...v, [key]: e.target.value }))}
                  className={inputCls}
                />,
              ),
            )}
            {NUM_FIELDS.map(({ key, labelKey }) =>
              fieldRow(
                key,
                t(labelKey),
                <input
                  type="number"
                  step="any"
                  disabled={!enabled[key]}
                  value={enabled[key] ? values[key] ?? '' : ''}
                  placeholder={placeholder(key)}
                  onChange={(e) => setValues((v) => ({ ...v, [key]: e.target.value }))}
                  className={inputCls}
                />,
              ),
            )}
            {fieldRow(
              'color',
              t('inventory.color'),
              <div className="flex items-center gap-2">
                <input
                  type="color"
                  disabled={!enabled.color}
                  value={enabled.color ? values.color || '#000000' : (sharedText.color ?? '#000000')}
                  onChange={(e) => setValues((v) => ({ ...v, color: e.target.value }))}
                  className="w-9 h-8 rounded border border-bambu-dark-tertiary bg-bambu-dark disabled:opacity-40 flex-shrink-0"
                />
                <input
                  type="text"
                  disabled={!enabled.color}
                  value={enabled.color ? values.color_name ?? '' : ''}
                  placeholder={sharedText.color_name == null ? t('inventory.bulkEdit.varies') : t('inventory.colorName')}
                  onChange={(e) => setValues((v) => ({ ...v, color_name: e.target.value }))}
                  className={inputCls}
                />
              </div>,
            )}
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 p-4 border-t border-bambu-dark-tertiary flex-shrink-0">
          <button onClick={onClose} className="px-4 py-2 text-sm text-bambu-gray hover:text-white">
            {t('common.cancel')}
          </button>
          <button
            onClick={() => bulkMutation.mutate()}
            disabled={!anyEnabled || selected.length === 0 || bulkMutation.isPending}
            className="px-4 py-2 text-sm font-medium rounded-lg bg-bambu-green text-black hover:bg-bambu-green/90 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-2"
          >
            {bulkMutation.isPending && <Loader2 className="w-4 h-4 animate-spin" />}
            {t('inventory.bulkEdit.apply', { count: selected.length })}
          </button>
        </div>
      </div>
    </div>
  );
}
