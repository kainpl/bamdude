/**
 * FamilyPicker — the identity control of the filament family catalog
 * (spec A §5.1): a searchable dropdown over `GET /filament-families`,
 * system families and the user's custom ones alike. The picked value is a
 * bare `filament_id` string ("GFG99" / "P122e532") — the same identity the
 * printer, the K-profiles and Bambu Studio use.
 *
 * Portal pattern mirrors labels/CardSelect: the panel renders into
 * document.body so a scrolling dialog cannot clip it, and it always opens
 * downward from the trigger.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useQuery } from '@tanstack/react-query';
import { ChevronDown, X } from 'lucide-react';
import { api, type FilamentFamily } from '../api/client';
import { useTranslation } from 'react-i18next';

interface FamilyPickerProps {
  value: string | null;
  onChange: (id: string | null, family: FilamentFamily | null) => void;
  disabled?: boolean;
  /** Muted hint shown when no family is linked (e.g. the legacy preset name). */
  legacyHint?: string;
}

const PANEL_MAX_PX = 288;
const GAP_PX = 4;
const EDGE_PX = 8;
const MIN_PANEL_PX = 140;

export function FamilyPicker({ value, onChange, disabled, legacyHint }: FamilyPickerProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [debounced, setDebounced] = useState('');
  const trigger = useRef<HTMLButtonElement | null>(null);
  const panel = useRef<HTMLDivElement | null>(null);
  const [box, setBox] = useState<{ left: number; top: number; width: number; maxHeight: number } | null>(null);

  useEffect(() => {
    const handle = setTimeout(() => setDebounced(query), 250);
    return () => clearTimeout(handle);
  }, [query]);

  // Poke the mirror sync once per mount (server-side debounced) so a preset
  // just created in BS/Orca shows up without waiting for the 5-min loop.
  useEffect(() => {
    api.triggerFilamentPresetSync().catch(() => undefined);
  }, []);

  const { data: families } = useQuery({
    queryKey: ['filamentFamilies', debounced],
    queryFn: () => api.getFilamentFamilies(debounced),
    staleTime: 30_000,
  });

  const selected = useMemo(() => {
    if (!value) return null;
    return (families || []).find((f) => f.filament_id === value) || null;
  }, [families, value]);

  const measure = useCallback(() => {
    const rect = trigger.current?.getBoundingClientRect();
    if (!rect) return;
    const below = window.innerHeight - rect.bottom - GAP_PX - EDGE_PX;
    setBox({
      left: rect.left,
      top: rect.bottom + GAP_PX,
      width: rect.width,
      maxHeight: Math.max(MIN_PANEL_PX, Math.min(PANEL_MAX_PX, below)),
    });
  }, []);

  useEffect(() => {
    if (!open) return;
    measure();
    const onScroll = () => measure();
    window.addEventListener('resize', onScroll);
    window.addEventListener('scroll', onScroll, true);
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (trigger.current?.contains(target) || panel.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => {
      window.removeEventListener('resize', onScroll);
      window.removeEventListener('scroll', onScroll, true);
      document.removeEventListener('mousedown', onDown);
    };
  }, [open, measure]);

  const rows = families || [];

  const list = box ? (
    <div
      ref={panel}
      role="listbox"
      className="fixed z-[100] rounded-lg border border-gray-600 bg-bambu-dark shadow-xl overflow-hidden flex flex-col"
      style={{ left: box.left, top: box.top, width: box.width }}
    >
      <input
        autoFocus
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={t('familyPicker.search')}
        className="m-2 px-2 py-1.5 rounded bg-bambu-darker border border-gray-600 text-sm text-white outline-none"
      />
      <div className="overflow-y-auto" style={{ maxHeight: box.maxHeight }}>
        {rows.length === 0 && <div className="p-3 text-sm text-gray-400">{t('familyPicker.empty')}</div>}
        {rows.map((fam) => (
          <button
            key={fam.filament_id}
            type="button"
            role="option"
            aria-selected={fam.filament_id === value}
            onClick={() => {
              onChange(fam.filament_id, fam);
              setOpen(false);
            }}
            className={`w-full text-left px-3 py-2 hover:bg-bambu-darker flex flex-col gap-0.5 ${
              fam.filament_id === value ? 'bg-bambu-darker' : ''
            }`}
          >
            <span className="text-sm text-white font-medium flex items-center gap-2">
              {fam.alias}
              {fam.origin !== 'system' && (
                <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-bambu-green/20 text-bambu-green">
                  {t('familyPicker.custom')}
                </span>
              )}
            </span>
            <span className="text-xs text-gray-400">
              {[fam.vendor, fam.filament_type].filter(Boolean).join(' · ')}
            </span>
          </button>
        ))}
      </div>
    </div>
  ) : null;

  return (
    <div className="relative">
      <button
        ref={trigger}
        type="button"
        role="combobox"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        className="w-full text-left p-2.5 rounded-lg border border-gray-600 bg-bambu-dark flex items-center gap-2 disabled:opacity-50"
      >
        <span className="flex-1 min-w-0">
          {selected ? (
            <>
              <span className="block text-sm text-white truncate">{selected.alias}</span>
              <span className="block text-xs text-gray-400 truncate">
                {[selected.vendor, selected.filament_type].filter(Boolean).join(' · ')}
              </span>
            </>
          ) : value ? (
            <span className="block text-sm text-white truncate">{value}</span>
          ) : (
            <>
              <span className="block text-sm text-gray-400">{t('familyPicker.placeholder')}</span>
              {legacyHint && <span className="block text-xs text-gray-500 truncate">{legacyHint}</span>}
            </>
          )}
        </span>
        {value && !disabled && (
          <X
            className="w-4 h-4 text-gray-400 hover:text-white shrink-0"
            onClick={(e) => {
              e.stopPropagation();
              onChange(null, null);
            }}
          />
        )}
        <ChevronDown className="w-4 h-4 text-gray-400 shrink-0" />
      </button>
      {open && createPortal(list, document.body)}
    </div>
  );
}
