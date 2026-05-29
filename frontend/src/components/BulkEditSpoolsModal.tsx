import { useState, useMemo, useRef, useEffect } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { X, Loader2, Layers, ChevronDown } from 'lucide-react';
import { api, type InventorySpool, type SpoolCatalogEntry } from '../api/client';
import { useToast } from '../contexts/ToastContext';
import { MATERIALS, KNOWN_VARIANTS } from './spool-form/constants';
import { buildFilamentOptions, extractBrandsFromPresets } from './spool-form/utils';
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
  { key: 'subtype', labelKey: 'inventory.subtype', type: 'datalist' },
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
  { key: 'purchase_location', labelKey: 'inventory.purchaseLocation', type: 'datalist' },
];

const NUMERIC = new Set(['label_weight', 'cost_per_kg', 'low_stock_threshold_pct']);

/** Small combobox matching the spool-edit dialog: a text input with a compact,
 *  scrollable dropdown under it (not a native full-width datalist). Free text is
 *  allowed (the input IS the value), so custom brands/variants work. */
function Combobox({ value, options, onChange, disabled, placeholder }: {
  value: string;
  options: string[];
  onChange: (v: string) => void;
  disabled?: boolean;
  placeholder?: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);
  const q = value.trim().toLowerCase();
  const filtered = q ? options.filter((o) => o.toLowerCase().includes(q)) : options;
  return (
    <div className="relative" ref={ref}>
      <input
        type="text"
        disabled={disabled}
        value={value}
        placeholder={placeholder}
        onChange={(e) => { onChange(e.target.value); setOpen(true); }}
        onFocus={() => setOpen(true)}
        className="w-full px-3 py-1.5 pr-8 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm placeholder:text-bambu-gray/50 focus:outline-none focus:border-bambu-green disabled:opacity-40"
      />
      <ChevronDown className="absolute right-2 top-1/2 -translate-y-1/2 w-4 h-4 text-bambu-gray/50 pointer-events-none" />
      {open && !disabled && filtered.length > 0 && (
        <div className="absolute z-50 w-full mt-1 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-lg max-h-48 overflow-y-auto">
          {filtered.map((o) => (
            <button
              key={o}
              type="button"
              className={`w-full px-3 py-1.5 text-left text-sm hover:bg-bambu-dark-tertiary ${value === o ? 'bg-bambu-green/10 text-bambu-green' : 'text-white'}`}
              onClick={() => { onChange(o); setOpen(false); }}
            >
              {o}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

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

  // Colour catalog — available named colours (mirrors the single-spool form).
  const { data: colorCatalog } = useQuery({
    queryKey: ['colorCatalog'],
    queryFn: () => api.getColorCatalog().catch(() => []),
    enabled: isOpen,
  });

  // Colour options are FILTERED by the effective brand AND material — the
  // values being applied (if those fields are enabled) else the selection's
  // shared value — and recompute when either changes, like the single-spool
  // form. Catalog entries with no material are generic (kept for any material).
  // Picking a name fills the hex.
  const colorPicker = useMemo(() => {
    const sharedOf = (get: (s: InventorySpool) => string | null | undefined) => {
      const set = new Set(selected.map((s) => (get(s) ?? '').trim().toLowerCase()).filter(Boolean));
      return set.size === 1 ? [...set][0] : '';
    };
    const effBrand = (enabled.brand ? (values.brand ?? '') : sharedOf((s) => s.brand)).trim().toLowerCase();
    const effMaterial = (enabled.material ? (values.material ?? '') : sharedOf((s) => s.material)).trim().toLowerCase();
    const loose = (a: string, b: string) => a === b || a.includes(b) || b.includes(a);
    const brandOk = (m?: string | null) => !effBrand || loose((m ?? '').trim().toLowerCase(), effBrand);
    const materialOk = (m?: string | null) => {
      const mm = (m ?? '').trim().toLowerCase();
      return !effMaterial || !mm || loose(mm, effMaterial); // empty material = generic
    };
    const entries = (colorCatalog ?? []).filter((c) => brandOk(c.manufacturer) && materialOk(c.material));
    const byName = new Map<string, string>();
    for (const c of entries) {
      const hex = (c.hex_color ?? '').replace('#', '').slice(0, 6);
      if (c.color_name && hex) byName.set(c.color_name.toLowerCase(), hex);
    }
    const names = [...new Set(entries.map((c) => c.color_name).filter(Boolean))].sort((a, b) => a.localeCompare(b));
    return { byName, names };
  }, [colorCatalog, selected, enabled.brand, values.brand, enabled.material, values.material]);

  // Autocomplete suggestions — from the system at large (known brands/variants
  // from slicer presets + the colour catalog) unioned with values already in
  // inventory, NOT just the filtered candidates. So you can switch to any brand
  // the system knows, even one no selected spool uses yet.
  const suggestions = useMemo(() => {
    const distinct = (get: (s: InventorySpool) => string | null | undefined) =>
      allSpools.map((s) => (get(s) ?? '').trim()).filter(Boolean);
    const uniq = (arr: string[]) => [...new Set(arr.filter(Boolean))].sort((a, b) => a.localeCompare(b));
    const presetBrands = extractBrandsFromPresets(cloudPresets ?? [], localPresets ?? []);
    const catalogBrands = (colorCatalog ?? []).map((c) => (c.manufacturer ?? '').trim()).filter(Boolean);
    return {
      material: uniq([...MATERIALS, ...distinct((s) => s.material)]),
      brand: uniq([...presetBrands, ...catalogBrands, ...distinct((s) => s.brand)]),
      subtype: uniq([...KNOWN_VARIANTS, ...distinct((s) => s.subtype)]),
      category: uniq(distinct((s) => s.category)),
      storage_location: uniq(distinct((s) => s.storage_location)),
      purchase_location: uniq(distinct((s) => s.purchase_location)),
    } as Record<string, string[]>;
  }, [allSpools, cloudPresets, localPresets, colorCatalog]);

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
        // Datalist fields start EMPTY (shared value shown as placeholder) so the
        // dropdown offers every option — a pre-filled value makes the browser
        // filter the list down to just that value, hiding the alternatives.
        const ty = FIELDS.find((f) => f.key === key)?.type;
        setValues((v) => ({
          ...v,
          [key]: key === 'color' ? (shared.color ?? '#000000') : ty === 'datalist' ? '' : (shared[key] ?? ''),
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
        } else if (f.type === 'catalog') {
          const id = raw === '' ? null : Number(raw);
          fields.core_weight_catalog_id = id;
          const entry = catalogEntries.find((c) => c.id === id);
          if (entry) fields.core_weight = entry.weight; // keep core weight in sync with the picked spool
        } else if (f.type === 'datalist') {
          // Not pre-filled → empty means "leave unchanged" (skip), so an enabled
          // brand/material/etc. with no pick doesn't wipe the column.
          if (raw) fields[f.key] = raw;
        } else if (NUMERIC.has(f.key)) {
          fields[f.key] = raw === '' ? null : Number(raw);
        } else if (f.type === 'diameter') {
          fields[f.key] = raw || '1.75';
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
      case 'datalist':
        return (
          <Combobox value={val} options={suggestions[f.key] ?? []} disabled={!on}
            placeholder={placeholder(f.key)} onChange={(x) => set(f.key, x)} />
        );
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
            <div className="flex-1 min-w-0">
              <Combobox
                value={on ? values.color_name ?? '' : ''}
                options={colorPicker.names}
                disabled={!on}
                placeholder={shared.color_name == null ? t('inventory.bulkEdit.varies') : t('inventory.colorName')}
                onChange={(name) => {
                  // Picking a catalog colour fills the hex from the catalog.
                  const hex = colorPicker.byName.get(name.toLowerCase());
                  setValues((v) => ({ ...v, color_name: name, ...(hex ? { color: `#${hex}` } : {}) }));
                }}
              />
            </div>
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
