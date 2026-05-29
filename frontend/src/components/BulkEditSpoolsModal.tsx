import { useState, useMemo } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { X, Loader2, Layers } from 'lucide-react';
import { api, type InventorySpool, type SpoolCatalogEntry } from '../api/client';
import { useToast } from '../contexts/ToastContext';
import { MATERIALS } from './spool-form/constants';
import { buildFilamentOptions } from './spool-form/utils';
import { FILAMENT_EFFECT_OPTIONS } from './filamentSwatchHelpers';

interface Props {
  isOpen: boolean;
  /** Candidate spools to pick from (the currently filtered inventory). */
  spools: InventorySpool[];
  /** Whole inventory — source for autocomplete suggestions (so you can pick a
   *  brand/material/etc. that none of the filtered candidates currently use). */
  allSpools: InventorySpool[];
  catalogEntries: SpoolCatalogEntry[];
  onClose: () => void;
  onSaved: () => void;
}

type FieldType = 'datalist' | 'text' | 'textarea' | 'number' | 'date' | 'diameter' | 'effect' | 'preset' | 'catalog' | 'color';
interface FieldDef {
  key: string; // spool column (or 'color' pseudo)
  labelKey: string;
  type: FieldType;
}

// Order roughly mirrors the single-spool form.
const FIELDS: FieldDef[] = [
  { key: 'slicer_filament', labelKey: 'inventory.slicerPreset', type: 'preset' },
  { key: 'material', labelKey: 'inventory.material', type: 'datalist' },
  { key: 'brand', labelKey: 'inventory.brand', type: 'datalist' },
  { key: 'subtype', labelKey: 'inventory.subtype', type: 'text' },
  { key: 'label_weight', labelKey: 'inventory.labelWeight', type: 'number' },
  { key: 'color', labelKey: 'inventory.color', type: 'color' },
  { key: 'core_weight_catalog_id', labelKey: 'inventory.coreWeight', type: 'catalog' },
  { key: 'purchase_date', labelKey: 'inventory.purchaseDate', type: 'date' },
  { key: 'filament_diameter', labelKey: 'inventory.filamentDiameter', type: 'diameter' },
  { key: 'cost_per_kg', labelKey: 'inventory.costPerKg', type: 'number' },
  { key: 'note', labelKey: 'inventory.note', type: 'textarea' },
  { key: 'category', labelKey: 'inventory.category', type: 'datalist' },
  { key: 'low_stock_threshold_pct', labelKey: 'inventory.bulkEdit.lowStockThreshold', type: 'number' },
  { key: 'extra_colors', labelKey: 'inventory.spoolForm.extraColors', type: 'text' },
  { key: 'effect_type', labelKey: 'inventory.spoolForm.effectType', type: 'effect' },
  { key: 'storage_location', labelKey: 'inventory.storageLocation', type: 'datalist' },
];

const NUMERIC = new Set(['label_weight', 'cost_per_kg', 'low_stock_threshold_pct']);

/** Bulk-edit selected spools. Pick which spools (default: all filtered), then
 *  tick which fields to change. Inputs mirror the single-spool form (selects /
 *  autocomplete from existing data, not plain text). A field pre-fills when the
 *  selection shares one value, else shows "— varies —"; only ticked fields are
 *  sent. Consumed weight + RFID are never touched. Internal inventory only. */
export function BulkEditSpoolsModal({ isOpen, spools, allSpools, catalogEntries, onClose, onSaved }: Props) {
  const { t } = useTranslation();
  const { showToast } = useToast();

  const [selectedIds, setSelectedIds] = useState<Set<number>>(() => new Set(spools.map((s) => s.id)));
  const [enabled, setEnabled] = useState<Record<string, boolean>>({});
  const [values, setValues] = useState<Record<string, string>>({});

  const selected = useMemo(() => spools.filter((s) => selectedIds.has(s.id)), [spools, selectedIds]);

  // Slicer-preset options (cloud + local + builtin), same source as the form.
  const { data: cloudPresets } = useQuery({
    queryKey: ['filamentPresets'],
    queryFn: () => api.getFilamentPresets().catch(() => []),
    enabled: isOpen,
  });
  const { data: localPresets } = useQuery({
    queryKey: ['localPresets'],
    queryFn: () => api.getLocalPresets().then((r) => r.filament).catch(() => []),
    enabled: isOpen,
  });
  const { data: builtinFilaments } = useQuery({
    queryKey: ['builtinFilaments'],
    queryFn: () => api.getBuiltinFilaments().catch(() => []),
    enabled: isOpen,
  });
  const presetOptions = useMemo(
    () => buildFilamentOptions(cloudPresets ?? [], new Set(), localPresets ?? [], builtinFilaments ?? []),
    [cloudPresets, localPresets, builtinFilaments],
  );

  // Autocomplete suggestions drawn from the WHOLE inventory (not just the
  // filtered candidates) — otherwise filtering to one brand would hide every
  // other brand from the picker.
  const suggestions = useMemo(() => {
    const distinct = (get: (s: InventorySpool) => string | null | undefined) =>
      [...new Set(allSpools.map((s) => (get(s) ?? '').trim()).filter(Boolean))].sort();
    return {
      material: [...new Set([...MATERIALS, ...distinct((s) => s.material)])],
      brand: distinct((s) => s.brand),
      category: distinct((s) => s.category),
      storage_location: distinct((s) => s.storage_location),
    } as Record<string, string[]>;
  }, [allSpools]);

  // Colour catalog — the available named colours (mirrors the single-spool
  // form, which fills colour from the catalog). Picking a name sets the hex.
  const { data: colorCatalog } = useQuery({
    queryKey: ['colorCatalog'],
    queryFn: () => api.getColorCatalog().catch(() => []),
    enabled: isOpen,
  });
  const colorByName = useMemo(() => {
    const m = new Map<string, string>(); // lower(name) -> hex (6)
    for (const c of colorCatalog ?? []) {
      const hex = (c.hex_color ?? '').replace('#', '').slice(0, 6);
      if (c.color_name && hex) m.set(c.color_name.toLowerCase(), hex);
    }
    return m;
  }, [colorCatalog]);
  const colorNameOptions = useMemo(
    () => [...new Set((colorCatalog ?? []).map((c) => c.color_name).filter(Boolean))].sort(),
    [colorCatalog],
  );

  // Shared value across the SELECTED spools (string form), or null when it varies.
  const shared = useMemo(() => {
    const out: Record<string, string | null> = {};
    const sv = (key: string, get: (s: InventorySpool) => unknown) => {
      const d = new Set(selected.map((s) => { const v = get(s); return v == null ? '' : String(v); }));
      out[key] = d.size === 1 ? ([...d][0] as string) : null;
    };
    for (const f of FIELDS) {
      if (f.type === 'color') continue;
      sv(f.key, (s) => (s as unknown as Record<string, unknown>)[f.key]);
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
          [key]: key === 'color' ? (shared.color ?? '#000000') : (shared[key] ?? ''),
          ...(key === 'color' ? { color_name: shared.color_name ?? '' } : {}),
        }));
      }
      return next;
    });

  const bulkMutation = useMutation({
    mutationFn: () => {
      const fields: Record<string, unknown> = {};
      for (const f of FIELDS) {
        if (!enabled[f.key]) continue;
        const raw = (values[f.key] ?? '').trim();
        if (f.type === 'color') {
          const hex = (values.color ?? '').replace('#', '').toUpperCase();
          if (hex.length === 6) fields.rgba = `${hex}FF`;
          fields.color_name = (values.color_name ?? '').trim() || null;
        } else if (NUMERIC.has(f.key)) {
          fields[f.key] = raw === '' ? null : Number(raw);
        } else if (f.type === 'catalog') {
          const id = raw === '' ? null : Number(raw);
          fields.core_weight_catalog_id = id;
          const entry = catalogEntries.find((c) => c.id === id);
          if (entry) fields.core_weight = entry.weight; // keep core weight in sync with the picked spool
        } else if (f.key === 'material') {
          if (raw) fields.material = raw; // non-null column
        } else {
          fields[f.key] = raw || null;
        }
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
  const placeholder = (key: string) => (shared[key] == null ? t('inventory.bulkEdit.varies') : (shared[key] as string));
  const set = (key: string, val: string) => setValues((v) => ({ ...v, [key]: val }));

  const renderInput = (f: FieldDef) => {
    const on = !!enabled[f.key];
    const val = on ? values[f.key] ?? '' : '';
    switch (f.type) {
      case 'datalist': {
        const listId = `bulk-dl-${f.key}`;
        return (
          <>
            <input type="text" list={listId} disabled={!on} value={val} placeholder={placeholder(f.key)}
              onChange={(e) => set(f.key, e.target.value)} className={inputCls} />
            <datalist id={listId}>{(suggestions[f.key] ?? []).map((o) => <option key={o} value={o} />)}</datalist>
          </>
        );
      }
      case 'textarea':
        return <textarea rows={2} disabled={!on} value={val} placeholder={placeholder(f.key)}
          onChange={(e) => set(f.key, e.target.value)} className={inputCls} />;
      case 'number':
        return <input type="number" step="any" disabled={!on} value={val} placeholder={placeholder(f.key)}
          onChange={(e) => set(f.key, e.target.value)} className={inputCls} />;
      case 'date':
        return <input type="date" disabled={!on} value={val} onChange={(e) => set(f.key, e.target.value)} className={inputCls} />;
      case 'diameter':
        return (
          <select disabled={!on} value={val || '1.75'} onChange={(e) => set(f.key, e.target.value)} className={inputCls}>
            <option value="1.75">1.75 mm</option>
            <option value="2.85">2.85 mm</option>
          </select>
        );
      case 'effect':
        return (
          <select disabled={!on} value={val} onChange={(e) => set(f.key, e.target.value)} className={inputCls}>
            {FILAMENT_EFFECT_OPTIONS.map((o) => <option key={o.value} value={o.value}>{t(o.labelKey)}</option>)}
          </select>
        );
      case 'preset':
        return (
          <select disabled={!on} value={val} onChange={(e) => set(f.key, e.target.value)} className={inputCls}>
            <option value="">{shared.slicer_filament == null ? t('inventory.bulkEdit.varies') : '—'}</option>
            {presetOptions.map((o) => <option key={o.code} value={o.code}>{o.displayName}</option>)}
          </select>
        );
      case 'catalog':
        return (
          <select disabled={!on} value={val} onChange={(e) => set(f.key, e.target.value)} className={inputCls}>
            <option value="">{shared.core_weight_catalog_id == null ? t('inventory.bulkEdit.varies') : '—'}</option>
            {catalogEntries.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.weight}g)</option>)}
          </select>
        );
      case 'color':
        return (
          <div className="flex items-center gap-2">
            <input type="color" disabled={!on} value={on ? values.color || '#000000' : (shared.color ?? '#000000')}
              onChange={(e) => set('color', e.target.value)}
              className="w-9 h-8 rounded border border-bambu-dark-tertiary bg-bambu-dark disabled:opacity-40 flex-shrink-0" />
            <input type="text" list="bulk-dl-color" disabled={!on} value={on ? values.color_name ?? '' : ''}
              placeholder={shared.color_name == null ? t('inventory.bulkEdit.varies') : t('inventory.colorName')}
              onChange={(e) => {
                const name = e.target.value;
                // Picking a catalog colour fills the hex from the catalog.
                const hex = colorByName.get(name.toLowerCase());
                setValues((v) => ({ ...v, color_name: name, ...(hex ? { color: `#${hex}` } : {}) }));
              }}
              className={inputCls} />
            <datalist id="bulk-dl-color">{colorNameOptions.map((n) => <option key={n} value={n} />)}</datalist>
          </div>
        );
      default:
        return <input type="text" disabled={!on} value={val} placeholder={placeholder(f.key)}
          onChange={(e) => set(f.key, e.target.value)} className={inputCls} />;
    }
  };

  const spoolLabel = (s: InventorySpool) => [s.brand, s.material, s.color_name].filter(Boolean).join(' ') || `#${s.id}`;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" onClick={onClose}>
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
      <div
        className="relative w-full max-w-3xl mx-4 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl max-h-[90vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary flex-shrink-0">
          <div className="flex items-center gap-2">
            <Layers className="w-4 h-4 text-bambu-green" />
            <h2 className="text-lg font-semibold text-white">{t('inventory.bulkEdit.title')}</h2>
            <span className="text-sm text-bambu-gray">{t('inventory.bulkEdit.selectedCount', { count: selected.length })}</span>
          </div>
          <button onClick={onClose} className="p-1 text-bambu-gray hover:text-white rounded"><X className="w-4 h-4" /></button>
        </div>

        <div className="flex flex-1 min-h-0">
          {/* Selection pane */}
          <div className="w-56 flex-shrink-0 border-r border-bambu-dark-tertiary flex flex-col">
            <button
              onClick={() => setSelectedIds(allSelected ? new Set() : new Set(spools.map((s) => s.id)))}
              className="text-left px-3 py-2 text-xs text-bambu-green hover:bg-bambu-dark/50 border-b border-bambu-dark-tertiary flex-shrink-0"
            >
              {allSelected ? t('inventory.labels.deselectVisible') : t('inventory.labels.selectVisible', { count: spools.length })}
            </button>
            <div className="overflow-y-auto flex-1">
              {spools.map((s) => (
                <label key={s.id} className="flex items-center gap-2 px-3 py-1.5 text-xs cursor-pointer hover:bg-bambu-dark/40">
                  <input type="checkbox" checked={selectedIds.has(s.id)} onChange={() => toggleSpool(s.id)}
                    className="w-3.5 h-3.5 accent-bambu-green flex-shrink-0" />
                  <span className="w-3 h-3 rounded-full flex-shrink-0 border border-bambu-dark-tertiary"
                    style={{ background: s.rgba ? `#${s.rgba.slice(0, 6)}` : '#666' }} />
                  <span className="truncate text-bambu-gray">{spoolLabel(s)}</span>
                </label>
              ))}
            </div>
          </div>

          {/* Field editor */}
          <div className="flex-1 min-w-0 p-4 overflow-y-auto">
            <p className="text-xs text-bambu-gray mb-3">{t('inventory.bulkEdit.hint')}</p>
            {FIELDS.map((f) => (
              <div key={f.key} className="flex items-center gap-3 py-1.5">
                <input type="checkbox" checked={!!enabled[f.key]} onChange={() => toggle(f.key)}
                  className="w-4 h-4 accent-bambu-green flex-shrink-0" aria-label={t(f.labelKey)} />
                <span className={`text-sm w-40 flex-shrink-0 ${enabled[f.key] ? 'text-white' : 'text-bambu-gray'}`}>{t(f.labelKey)}</span>
                <div className="flex-1 min-w-0">{renderInput(f)}</div>
              </div>
            ))}
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 p-4 border-t border-bambu-dark-tertiary flex-shrink-0">
          <button onClick={onClose} className="px-4 py-2 text-sm text-bambu-gray hover:text-white">{t('common.cancel')}</button>
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
