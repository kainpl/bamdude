
import type { Printer, SpoolKProfile } from '../../api/client';

// Catalog color display type (moved from component)
export interface CatalogDisplayColor {
  name: string;
  hex: string;
  manufacturer?: string;
  material?: string;
  // #1340: catalog presets carry the full visual look — gradient stops
  // and/or visual effect (sparkle / wood / marble / glow / matte). Without
  // these the catalog-swatch click silently degraded a multi-color preset
  // to a flat solid swatch.
  extra_colors?: string | null;
  effect_type?: string | null;
}

// Form data structure
export interface SpoolFormData {
  material: string;
  subtype: string;
  brand: string;
  color_name: string;
  rgba: string;
  label_weight: number;
  core_weight: number;
  core_weight_catalog_id: number | null;
  weight_used: number;
  slicer_filament: string;
  note: string;
  cost_per_kg: number | null;
  // ISO yyyy-mm-dd string or empty. Stored separately from ``created_at``
  // (the row's import timestamp) — see m020_spool_purchase_date.
  purchase_date: string;
  // "1.75" | "2.85"; never empty — defaulted to "1.75" on create.
  filament_diameter: string;
  // Empty string = NULL on the wire. 0 is explicitly disallowed by the
  // validator so the user doesn't accidentally encode "unset" as zero.
  lot: string;
  // Quick-add-only toggle: when on, the bulk-create endpoint replaces
  // per-row lot with 1..N. The ``lot`` field above is hidden behind this
  // checkbox in quick-add mode.
  auto_increment_lot: boolean;
  // B.1 — comma-separated 6/8-char hex tokens (no leading `#`), up to 8.
  // Empty string = solid (use `rgba`).
  extra_colors: string;
  // B.1 — visual effect overlay; empty string = no effect.
  effect_type: string;
  // B.8 — free-text category; empty string = NULL on the wire.
  category: string;
  // B.8 — per-spool override of the global low-stock threshold (1..99).
  // Empty string = NULL (use global).
  low_stock_threshold_pct: string;
  // Spoolman inventory UI (upstream PR #1241): free-form storage label.
  storage_location: string;
  purchase_location: string;
  // Spoolman inventory UI: when set the spool links to a specific Spoolman
  // filament catalog entry; backend skips find_or_create_filament() and uses
  // this ID directly.
  spoolman_filament_id: number | null;
}

export const defaultFormData: SpoolFormData = {
  material: '',
  subtype: '',
  brand: '',
  color_name: '',
  rgba: '808080FF',
  label_weight: 1000,
  core_weight: 250,
  core_weight_catalog_id: null,
  weight_used: 0,
  slicer_filament: '',
  note: '',
  cost_per_kg: null,
  purchase_date: '',
  filament_diameter: '1.75',
  lot: '',
  auto_increment_lot: false,
  extra_colors: '',
  effect_type: '',
  category: '',
  low_stock_threshold_pct: '',
  storage_location: '',
  purchase_location: '',
  spoolman_filament_id: null,
};

// Printer with calibrations type
export interface PrinterWithCalibrations {
  printer: Printer & { connected?: boolean };
  calibrations: CalibrationProfile[];
}

// Calibration profile from printer status
export interface CalibrationProfile {
  cali_idx: number;
  filament_id: string;
  setting_id: string;
  name: string;
  k_value: number;
  n_coef: number;
  extruder_id?: number | null;
  nozzle_diameter?: string;
}

// Filament option from presets
export interface FilamentOption {
  code: string;
  name: string;
  displayName: string;
  isCustom: boolean;
  allCodes: string[];
}

// Color preset
export interface ColorPreset {
  name: string;
  hex: string;
}

// Section props base
export interface SectionProps {
  formData: SpoolFormData;
  updateField: <K extends keyof SpoolFormData>(key: K, value: SpoolFormData[K]) => void;
}

// Filament section props
export interface FilamentSectionProps extends SectionProps {
  cloudAuthenticated: boolean;
  loadingCloudPresets: boolean;
  presetInputValue: string;
  setPresetInputValue: (value: string) => void;
  selectedPresetOption?: FilamentOption;
  filamentOptions: FilamentOption[];
  availableBrands: string[];
  availableMaterials: string[];
  quickAdd: boolean;
  quantity: number;
  onQuantityChange: (value: number) => void;
  errors?: Partial<Record<keyof SpoolFormData, string>>;
}

// Color section props
export interface ColorSectionProps extends SectionProps {
  recentColors: ColorPreset[];
  onColorUsed: (color: ColorPreset) => void;
  catalogColors: {
    manufacturer: string;
    color_name: string;
    hex_color: string;
    material: string | null;
    // #1340: optional gradient stops + visual effect carried alongside
    // the base hex so picking a catalog swatch applies the full preset
    // look. Backward-compatible (older callers don't set them); falls
    // through to a plain hex pick when absent.
    extra_colors?: string | null;
    effect_type?: string | null;
  }[];
}

// Additional section props
export interface AdditionalSectionProps extends SectionProps {
  spoolCatalog: { id: number; name: string; weight: number }[];
  currencySymbol: string;
  // Quick-add toggle replaces the per-spool ``lot`` number field with an
  // "auto-increment lots" checkbox — sequential 1..N numbering is cheap
  // to do server-side, and the raw field wouldn't make sense for N copies.
  quickAdd?: boolean;
  // B.8 — categories already in use across the inventory; the form
  // autocompletes from this list so users converge on consistent labels.
  categories?: string[];
  errors?: Partial<Record<keyof SpoolFormData, string>>;
  // Spoolman inventory UI (upstream PR #1241): when true the empty-spool
  // weight is managed by Spoolman on the filament object, so
  // SpoolWeightPicker is hidden and an info notice is shown instead.
  spoolmanMode?: boolean;
}

// PA Profile section props
export interface PAProfileSectionProps extends SectionProps {
  printersWithCalibrations: PrinterWithCalibrations[];
  loading?: boolean;
  selectedProfiles: Set<string>;
  setSelectedProfiles: React.Dispatch<React.SetStateAction<Set<string>>>;
  expandedPrinters: Set<string>;
  setExpandedPrinters: React.Dispatch<React.SetStateAction<Set<string>>>;
}

// Fields that are prefilled by SpoolmanFilamentPicker. A manual edit to any of
// these breaks the Spoolman catalog link (clears spoolman_filament_id).
// Defined at module scope to avoid stale-closure issues if handlers are memoised.
export const SPOOLMAN_LINKED_FIELDS = new Set<keyof SpoolFormData>([
  'material',
  'subtype',
  'brand',
  'rgba',
  'color_name',
  'label_weight',
]);

// Validation result
export interface ValidationResult {
  isValid: boolean;
  errors: Partial<Record<keyof SpoolFormData, string>>;
}

export function validateForm(
  formData: SpoolFormData,
  quickAdd = false,
  spoolmanMode = false,
): ValidationResult {
  const errors: Partial<Record<keyof SpoolFormData, string>> = {};

  // Quick-add and Spoolman mode only require material (unless a catalog entry is pre-selected)
  if (quickAdd || spoolmanMode) {
    if (!formData.material && !formData.spoolman_filament_id) {
      errors.material = 'Material is required';
    }
    return {
      isValid: Object.keys(errors).length === 0,
      errors,
    };
  }

  if (!formData.slicer_filament) {
    errors.slicer_filament = 'Slicer preset is required';
  }

  if (!formData.material) {
    errors.material = 'Material is required';
  }

  if (!formData.brand) {
    errors.brand = 'Brand is required';
  }

  if (!formData.subtype) {
    errors.subtype = 'Subtype is required';
  }

  // B.8 — low-stock threshold override must be 1..99 if provided.
  if (formData.low_stock_threshold_pct) {
    const n = Number(formData.low_stock_threshold_pct);
    if (!Number.isInteger(n) || n < 1 || n > 99) {
      errors.low_stock_threshold_pct = 'Must be an integer 1..99';
    }
  }

  // B.1 — extra colours must be comma-separated 6/8-char hex tokens, ≤8.
  if (formData.extra_colors) {
    const stops = formData.extra_colors.split(',').map((s) => s.trim()).filter(Boolean);
    if (stops.length > 8) {
      errors.extra_colors = 'Up to 8 stops';
    } else if (stops.some((s) => !/^[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$/.test(s))) {
      errors.extra_colors = 'Invalid hex stop (use RRGGBB or RRGGBBAA)';
    }
  }

  return {
    isValid: Object.keys(errors).length === 0,
    errors,
  };
}

// Existing K-profile for a spool (from saved data)
export interface SavedKProfile extends SpoolKProfile {
  printer_serial?: string;
}
