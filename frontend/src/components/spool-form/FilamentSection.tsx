import { Fragment, useState, useRef, useEffect, useMemo } from 'react';
import { ChevronDown } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { FilamentSectionProps } from './types';
import { KNOWN_VARIANTS } from './constants';

/** Emit a "Suggested" / "All" heading when the ranked list crosses from the
 * catalog-paired entries to the rest. The two groups are a hint about which
 * combinations are known, never a restriction on what can be entered (#1905). */
function groupHeading(
  entries: { suggested: boolean }[],
  index: number,
  t: (key: string, fallback: string) => string,
) {
  const isFirstOfGroup = index === 0 || entries[index - 1].suggested !== entries[index].suggested;
  if (!isFirstOfGroup) return null;
  // A single ungrouped run needs no heading at all.
  if (entries.every((e) => e.suggested === entries[0].suggested)) return null;
  return (
    <div className="px-3 pt-2 pb-1 text-[11px] uppercase tracking-wide text-bambu-gray/70">
      {entries[index].suggested ? t('inventory.suggested', 'Suggested') : t('inventory.allOptions', 'All')}
    </div>
  );
}

/** Rank the entries the catalog knows to pair with the other field's value
 * first, WITHOUT dropping the rest (#1905). Elegoo is catalogued for PLA only,
 * so filtering by the pairing made a real product — Elegoo ASA — look
 * impossible to enter. Suggestion is a hint, not a rule. */
function rankBySuggested(all: string[], suggested: string[], search: string) {
  const suggestedSet = new Set(suggested.map((s) => s.toLowerCase()));
  const needle = search.toLowerCase();
  return all
    .filter((v) => !needle || v.toLowerCase().includes(needle))
    .map((value) => ({ value, suggested: suggestedSet.has(value.toLowerCase()) }))
    .sort((a, b) => {
      const aExact = a.value.toLowerCase() === needle;
      const bExact = b.value.toLowerCase() === needle;
      if (needle && aExact !== bExact) return aExact ? -1 : 1;
      if (a.suggested !== b.suggested) return a.suggested ? -1 : 1;
      return a.value.localeCompare(b.value);
    });
}

export function FilamentSection({
  formData,
  updateField,
  availableBrands,
  availableMaterials,
  suggestedBrands,
  suggestedMaterials,
  detailsRequired,
  quickAdd,
  quantity,
  onQuantityChange,
  errors,
}: FilamentSectionProps) {
  const { t } = useTranslation();
  const [brandDropdownOpen, setBrandDropdownOpen] = useState(false);
  const [subtypeDropdownOpen, setSubtypeDropdownOpen] = useState(false);
  const [materialDropdownOpen, setMaterialDropdownOpen] = useState(false);
  const [brandSearch, setBrandSearch] = useState('');
  const [subtypeSearch, setSubtypeSearch] = useState('');
  const [materialSearch, setMaterialSearch] = useState('');
  const [labelInput, setLabelInput] = useState(String(formData.label_weight));
  const [isLabelFocused, setIsLabelFocused] = useState(false);
  const brandRef = useRef<HTMLDivElement>(null);
  const subtypeRef = useRef<HTMLDivElement>(null);
  const materialRef = useRef<HTMLDivElement>(null);

  // Close dropdowns on outside click
  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (materialRef.current && !materialRef.current.contains(e.target as Node)) {
        setMaterialDropdownOpen(false);
      }
      if (brandRef.current && !brandRef.current.contains(e.target as Node)) {
        setBrandDropdownOpen(false);
      }
      if (subtypeRef.current && !subtypeRef.current.contains(e.target as Node)) {
        setSubtypeDropdownOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  const filteredBrands = useMemo(
    () => rankBySuggested(availableBrands, suggestedBrands, brandSearch),
    [availableBrands, suggestedBrands, brandSearch],
  );

  const filteredVariants = useMemo(() => {
    if (!subtypeSearch) return KNOWN_VARIANTS;
    const search = subtypeSearch.toLowerCase();
    return KNOWN_VARIANTS.filter(v => v.toLowerCase().includes(search));
  }, [subtypeSearch]);

  const filteredMaterials = useMemo(
    () => rankBySuggested(availableMaterials, suggestedMaterials, materialSearch),
    [availableMaterials, suggestedMaterials, materialSearch],
  );

  useEffect(() => {
    if (!isLabelFocused) {
      setLabelInput(String(formData.label_weight));
    }
  }, [formData.label_weight, isLabelFocused]);

  return (
    <div className="space-y-4">
      {/* Material */}
      <div>
        <label className="block text-sm font-medium text-bambu-gray mb-1">{t('inventory.material')} *</label>
        <div className="relative" ref={materialRef}>
          <input
            type="text"
            className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm placeholder:text-bambu-gray/50 focus:outline-none focus:border-bambu-green"
            placeholder={t('inventory.selectMaterial')}
            value={materialDropdownOpen ? materialSearch : formData.material}
            onChange={(e) => {
              setMaterialSearch(e.target.value);
              setMaterialDropdownOpen(true);
            }}
            onFocus={() => {
              setMaterialDropdownOpen(true);
              setMaterialSearch('');
            }}
          />
          <ChevronDown className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-bambu-gray/50 pointer-events-none" />
          {materialDropdownOpen && (
            <div className="absolute z-50 w-full mt-1 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-lg max-h-48 overflow-y-auto">
              {filteredMaterials.length === 0 ? (
                <div className="px-3 py-2 text-sm text-bambu-gray">{t('inventory.noResults')}</div>
              ) : (
                filteredMaterials.map((entry, i) => (
                  <Fragment key={entry.value}>
                    {groupHeading(filteredMaterials, i, t)}
                    <button
                      type="button"
                      className={`w-full px-3 py-2 text-left text-sm hover:bg-bambu-dark-tertiary ${
                        formData.material === entry.value ? 'bg-bambu-green/10 text-bambu-green' : 'text-white'
                      }`}
                      onClick={() => {
                        updateField('material', entry.value);
                        setMaterialDropdownOpen(false);
                        setMaterialSearch('');
                      }}
                    >
                      {entry.value}
                    </button>
                  </Fragment>
                ))
              )}
              {/* Allow custom material */}
              {materialSearch && !filteredMaterials.some((e) => e.value === materialSearch) && (
                <button
                  type="button"
                  className="w-full px-3 py-2 text-left text-sm hover:bg-bambu-dark-tertiary text-bambu-green border-t border-bambu-dark-tertiary"
                  onClick={() => {
                    updateField('material', materialSearch);
                    setMaterialDropdownOpen(false);
                    setMaterialSearch('');
                  }}
                >
                  {t('inventory.useCustomMaterial', { material: materialSearch })}
                </button>
              )}
            </div>
          )}
        </div>
        {errors?.material && (
          <p className="mt-1 text-xs text-red-700 dark:text-red-400">{errors.material}</p>
        )}
      </div>

      {/* Brand (dropdown with search) */}
      <div>
        <label className="block text-sm font-medium text-bambu-gray mb-1">
          {t('inventory.brand')}{detailsRequired && ' *'}
        </label>
          <div className="relative" ref={brandRef}>
            <input
              type="text"
              className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm placeholder:text-bambu-gray/50 focus:outline-none focus:border-bambu-green"
              placeholder={t('inventory.searchBrand')}
              value={brandDropdownOpen ? brandSearch : formData.brand}
              onChange={(e) => {
                setBrandSearch(e.target.value);
                setBrandDropdownOpen(true);
              }}
              onFocus={() => {
                setBrandDropdownOpen(true);
                setBrandSearch('');
              }}
            />
            <ChevronDown className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-bambu-gray/50 pointer-events-none" />
            {brandDropdownOpen && (
              <div className="absolute z-50 w-full mt-1 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-lg max-h-48 overflow-y-auto">
                {filteredBrands.length === 0 ? (
                  <div className="px-3 py-2 text-sm text-bambu-gray">{t('inventory.noResults')}</div>
                ) : (
                  filteredBrands.map((entry, i) => (
                    <Fragment key={entry.value}>
                      {groupHeading(filteredBrands, i, t)}
                      <button
                        type="button"
                        className={`w-full px-3 py-2 text-left text-sm hover:bg-bambu-dark-tertiary ${
                          formData.brand === entry.value ? 'bg-bambu-green/10 text-bambu-green' : 'text-white'
                        }`}
                        onClick={() => {
                          updateField('brand', entry.value);
                          setBrandDropdownOpen(false);
                          setBrandSearch('');
                        }}
                      >
                        {entry.value}
                      </button>
                    </Fragment>
                  ))
                )}
                {/* Allow custom brand */}
                {brandSearch && !filteredBrands.some((e) => e.value === brandSearch) && (
                  <button
                    type="button"
                    className="w-full px-3 py-2 text-left text-sm hover:bg-bambu-dark-tertiary text-bambu-green border-t border-bambu-dark-tertiary"
                    onClick={() => {
                      updateField('brand', brandSearch);
                      setBrandDropdownOpen(false);
                      setBrandSearch('');
                    }}
                  >
                    {t('inventory.useCustomBrand', { brand: brandSearch })}
                  </button>
                )}
              </div>
            )}
          </div>
          {errors?.brand && (
            <p className="mt-1 text-xs text-red-700 dark:text-red-400">{errors.brand}</p>
          )}
      </div>

      {/* Variant / Subtype */}
      <div>
        <label className="block text-sm font-medium text-bambu-gray mb-1">
          {t('inventory.subtype')}{detailsRequired && ' *'}
        </label>
          <div className="relative" ref={subtypeRef}>
            <input
              type="text"
              value={subtypeDropdownOpen ? subtypeSearch : formData.subtype}
              onChange={(e) => {
                setSubtypeSearch(e.target.value);
                setSubtypeDropdownOpen(true);
              }}
              onFocus={() => {
                setSubtypeDropdownOpen(true);
                setSubtypeSearch('');
              }}
              placeholder="Basic, Matte, Silk..."
              className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm placeholder:text-bambu-gray/50 focus:outline-none focus:border-bambu-green"
            />
            <ChevronDown className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-bambu-gray/50 pointer-events-none" />
            {subtypeDropdownOpen && (
              <div className="absolute z-50 w-full mt-1 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-lg max-h-48 overflow-y-auto">
                {filteredVariants.length === 0 ? (
                  <div className="px-3 py-2 text-sm text-bambu-gray">{t('inventory.noResults')}</div>
                ) : (
                  filteredVariants.map(variant => (
                    <button
                      key={variant}
                      type="button"
                      className={`w-full px-3 py-2 text-left text-sm hover:bg-bambu-dark-tertiary ${
                        formData.subtype === variant ? 'bg-bambu-green/10 text-bambu-green' : 'text-white'
                      }`}
                      onClick={() => {
                        updateField('subtype', variant);
                        setSubtypeDropdownOpen(false);
                        setSubtypeSearch('');
                      }}
                    >
                      {variant}
                    </button>
                  ))
                )}
                {subtypeSearch && !KNOWN_VARIANTS.some(v => v.toLowerCase() === subtypeSearch.toLowerCase().trim()) && (
                  <button
                    type="button"
                    className="w-full px-3 py-2 text-left text-sm hover:bg-bambu-dark-tertiary text-bambu-green border-t border-bambu-dark-tertiary"
                    onClick={() => {
                      updateField('subtype', subtypeSearch);
                      setSubtypeDropdownOpen(false);
                      setSubtypeSearch('');
                    }}
                  >
                    {t('inventory.useCustomBrand', { brand: subtypeSearch })}
                  </button>
                )}
              </div>
            )}
          </div>
          {errors?.subtype && (
            <p className="mt-1 text-xs text-red-700 dark:text-red-400">{errors.subtype}</p>
          )}
      </div>

      {/* Label Weight */}
      <div>
        <label className="block text-sm font-medium text-bambu-gray mb-1">{t('inventory.labelWeight')}</label>
        <div className="relative">
          <input
            type="number"
            className="w-full px-3 py-2 pr-7 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:outline-none focus:border-bambu-green"
            value={labelInput}
            min={0}
            onFocus={() => setIsLabelFocused(true)}
            onChange={(e) => setLabelInput(e.target.value)}
            onBlur={() => {
              setIsLabelFocused(false);
              const raw = labelInput.trim();
              const next = Number(raw);
              if (!raw || !Number.isFinite(next) || next < 0) {
                setLabelInput(String(formData.label_weight));
                return;
              }
              const rounded = Math.round(next);
              updateField('label_weight', rounded);
              setLabelInput(String(rounded));
            }}
          />
          <span className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-bambu-gray">g</span>
        </div>
      </div>

      {/* Quantity - only in quick-add mode */}
      {quickAdd && (
        <div>
          <label className="block text-sm font-medium text-bambu-gray mb-1">{t('inventory.quantity')}</label>
          <input
            type="number"
            className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:outline-none focus:border-bambu-green"
            value={quantity}
            min={1}
            max={100}
            onChange={(e) => {
              const val = Math.max(1, Math.min(100, parseInt(e.target.value) || 1));
              onQuantityChange(val);
            }}
          />
        </div>
      )}

    </div>
  );
}
