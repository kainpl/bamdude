import { useState, useMemo, useEffect, useRef, useCallback, type CSSProperties, type ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';
import { buildFilamentBackground } from '../components/filamentSwatchHelpers';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  Plus, Loader2, Trash2, Archive, RotateCcw, Edit2, Package,
  Search,
  TrendingDown, Layers, Printer, AlertTriangle, X, Clock, LayoutGrid, TableProperties, Columns,
  ArrowUp, ArrowDown, ArrowUpDown, Group, ChevronDown, Check, RefreshCw, Disc3, Copy, Eraser,
  TrendingUp, Lock, Sparkles, Upload, Download, MapPin,
} from 'lucide-react';
import { ForecastPanel } from '../components/ForecastPanel';
import { api, ApiError } from '../api/client';
import type { InventorySpool, SpoolAssignment, SpoolCatalogEntry } from '../api/client';
import { Button } from '../components/Button';
import { PaginationBar } from '../components/PaginationBar';
import { SpoolFormModal, type SpoolFormMode } from '../components/SpoolFormModal';
import { SpoolCsvImportModal } from '../components/SpoolCsvImportModal';
import { ConfirmModal } from '../components/ConfirmModal';
import { ColumnConfigModal, type ColumnConfig } from '../components/ColumnConfigModal';
import { LabelTemplatePickerModal } from '../components/LabelTemplatePickerModal';
import { BulkEditSpoolsModal } from '../components/BulkEditSpoolsModal';
import { LocationsModal } from '../components/LocationsModal';
import { useToast } from '../contexts/ToastContext';
import { useAuth } from '../contexts/AuthContext';
import { resolveSpoolColorName,
  colorSortKey,
} from '../utils/colors';
import { useColorCatalogVersion } from '../hooks/useColorCatalogVersion';
import { getCurrencySymbol } from '../utils/currency';
import { formatDateInput, parseUTCDate, type DateFormat } from '../utils/date';
import { formatSlotLabel } from '../utils/amsHelpers';
import { aggregateGroupSpool } from '../utils/inventoryGrouping';
import {
  loadFilters,
  saveFilters,
  withCurrentValue,
  withCurrentId,
  type ArchiveFilter,
  type UsageFilter,
  type StockFilter,
  type AssignedFilter,
  type ViewMode,
} from '../utils/inventoryFilters';
import {
  inventoryLocationsQueryKey,
  invalidateSpoolAndLocationQueries,
} from '../utils/inventoryQueries';
import {
  DEFAULT_SPOOL_DISPLAY_TEMPLATE,
  formatSpoolDisplayName,
  spoolDisplayNameMatches,
} from '../utils/spoolName';
// filterSpoolsByQuery — imported from utils/inventorySearch by Spoolman-mode upstream PR #1241; reserved for future Spoolman-side filtering paths.

type SortDirection = 'asc' | 'desc';
type SortState = { column: string; direction: SortDirection } | null;

type DisplayItem =
  | { type: 'single'; spool: InventorySpool }
  | { type: 'group'; key: string; spools: InventorySpool[]; representative: InventorySpool };

/**
 * B.1 / A.17 — render the swatch as a layered CSS background composed by the
 * shared `buildFilamentBackground` helper: effect-overlay (sparkle / glow /
 * etc.) on top of the colour layer (solid / 135° gradient / hard-split bars
 * for dual-color · tri-color / conic for multicolor), with a fixed-tile
 * checkerboard underlayer so transparent spools render visibly. Same
 * helper drives the form preview and the `<FilamentSwatch>` component, so
 * the inventory list, group banner, and edit form can never disagree.
 */
function spoolSwatchStyle(s: InventorySpool): CSSProperties {
  return buildFilamentBackground({
    rgba: s.rgba,
    extraColors: s.extra_colors,
    effectType: s.effect_type,
    subtype: s.subtype,
  });
}

function spoolGroupKey(s: InventorySpool): string {
  // Lot is part of the key so sequential-lot copies (a purchase bundle) stay
  // distinct cards instead of collapsing into one aggregate.
  return `${s.material}|${s.subtype || ''}|${s.brand || ''}|${s.color_name || ''}|${s.rgba || ''}|${s.label_weight}|${s.lot ?? ''}`;
}

// Column definitions for the inventory table
const COLUMN_CONFIG_KEY = 'bamdude-inventory-columns';

const DEFAULT_COLUMNS: ColumnConfig[] = [
  { id: 'id', label: '#', visible: true },
  { id: 'display_name', label: 'Name', visible: true },
  { id: 'purchase_date', label: 'Date of purchase', visible: true },
  { id: 'added_time', label: 'Added', visible: false },
  { id: 'encode_time', label: 'Encoded', visible: false },
  { id: 'last_used_time', label: 'Last Used', visible: false },
  { id: 'rgba', label: 'Color', visible: true },
  { id: 'material', label: 'Material', visible: true },
  { id: 'subtype', label: 'Subtype', visible: true },
  { id: 'color_name', label: 'Color Name', visible: false },
  { id: 'brand', label: 'Brand', visible: true },
  { id: 'slicer_filament', label: 'Slicer Filament', visible: false },
  { id: 'location', label: 'Location', visible: true },
  { id: 'storage_location', label: 'Storage Location', visible: false },
  { id: 'purchase_location', label: 'Purchase Location', visible: false },
  { id: 'label_weight', label: 'Label', visible: true },
  { id: 'net', label: 'Net', visible: true },
  { id: 'gross', label: 'Gross', visible: false },
  { id: 'added_full', label: 'Full', visible: false },
  { id: 'used', label: 'Used', visible: false },
  { id: 'printed_total', label: 'Printed Total', visible: false },
  { id: 'printed_since_weight', label: 'Printed Since Weight', visible: false },
  { id: 'note', label: 'Note', visible: false },
  { id: 'pa_k', label: 'PA(K)', visible: true },
  { id: 'tag_id', label: 'Tag ID', visible: false },
  { id: 'data_origin', label: 'Data Origin', visible: false },
  { id: 'tag_type', label: 'Linked Tag Type', visible: false },
  { id: 'stock', label: 'Stock', visible: false },
  { id: 'remaining', label: 'Remaining', visible: true },
  { id: 'spool_name', label: 'Spool', visible: false },
  { id: 'cost_per_kg', label: 'Cost/kg', visible: false },
  { id: 'weight_check', label: 'Weight Check', visible: false },
  { id: 'filament_diameter', label: 'Diameter', visible: false },
  { id: 'lot', label: 'Lot', visible: false },
];

function loadColumnConfig(): ColumnConfig[] {
  try {
    const stored = localStorage.getItem(COLUMN_CONFIG_KEY);
    if (stored) {
      const parsed = JSON.parse(stored) as ColumnConfig[];
      const defaultIds = new Set(DEFAULT_COLUMNS.map((c) => c.id));
      const storedIds = new Set(parsed.map((c) => c.id));
      // Keep stored columns that still exist in defaults — preserves user's
      // own column order.
      const validStored = parsed.filter((c) => defaultIds.has(c.id));
      // Splice NEW default columns back at their default positions instead
      // of appending to the end (old behaviour buried newly-added defaults
      // like ``display_name`` past the right edge for existing users).
      const result = [...validStored];
      DEFAULT_COLUMNS.forEach((def, idx) => {
        if (!storedIds.has(def.id)) {
          const insertAt = Math.min(idx, result.length);
          result.splice(insertAt, 0, { ...def });
        }
      });
      return result;
    }
  } catch {
    // Ignore errors
  }
  return DEFAULT_COLUMNS.map((c) => ({ ...c }));
}

function saveColumnConfig(config: ColumnConfig[]) {
  try {
    localStorage.setItem(COLUMN_CONFIG_KEY, JSON.stringify(config));
  } catch {
    // Ignore errors
  }
}

/**
 * Translate the user-visible column list into what the table actually
 * renders. When both ``rgba`` (swatch) and ``color_name`` are visible,
 * they collapse into one ``color_combined`` pseudo-column at whichever
 * slot comes first in the user's order. The visibility/order settings
 * panel keeps the two separate so the user can toggle them independently.
 */
function toRenderColumns(visible: string[]): string[] {
  const idxRgba = visible.indexOf('rgba');
  const idxName = visible.indexOf('color_name');
  if (idxRgba === -1 || idxName === -1) return visible;
  const firstIdx = Math.min(idxRgba, idxName);
  const out: string[] = [];
  visible.forEach((c, i) => {
    if (c === 'rgba' || c === 'color_name') {
      if (i === firstIdx) out.push('color_combined');
    } else {
      out.push(c);
    }
  });
  return out;
}

function formatWeight(g: number, useKg = false): string {
  if (useKg && g >= 1000) return `${(g / 1000).toFixed(1)}kg`;
  return `${Math.round(g)}g`;
}

// Material color mapping for pills
const MATERIAL_COLORS: Record<string, string> = {
  PLA: 'bg-green-100 dark:bg-green-500/20 text-green-700 dark:text-green-400',
  ABS: 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400',
  PETG: 'bg-blue-100 dark:bg-blue-500/20 text-blue-700 dark:text-blue-400',
  TPU: 'bg-purple-100 dark:bg-purple-500/20 text-purple-700 dark:text-purple-400',
  ASA: 'bg-orange-100 dark:bg-orange-500/20 text-orange-700 dark:text-orange-400',
  PA: 'bg-yellow-100 dark:bg-yellow-500/20 text-yellow-700 dark:text-yellow-400',
  PC: 'bg-cyan-100 dark:bg-cyan-500/20 text-cyan-700 dark:text-cyan-400',
  PET: 'bg-sky-100 dark:bg-sky-500/20 text-sky-700 dark:text-sky-400',
};

type TFn = (key: string, opts?: Record<string, unknown>) => string;

function formatInventoryDate(dateStr: string | null, dateFormat: DateFormat = 'system'): string {
  if (!dateStr) return '-';
  const date = parseUTCDate(dateStr);
  if (!date) return '-';
  return formatDateInput(date, dateFormat);
}

// Slim shape for the LOCATION column — only the fields actually rendered.
// Sourced from either local SpoolAssignment (lokal) or SpoolmanSlotAssignment
// (Spoolman mode), so we can't reuse SpoolAssignment without dummy values.
type LocationDisplay = {
  printer_id: number;
  printer_name: string | null;
  ams_id: number;
  tray_id: number;
  ams_label: string | null;
};

type CellCtx = {
  spool: InventorySpool;
  remaining: number;
  pct: number;
  assignmentMap: Record<number, LocationDisplay>;
  catalogMap: Record<number, SpoolCatalogEntry>;
  currencySymbol: string;
  dateFormat: DateFormat;
  t: TFn;
  onSyncWeight?: (spool: InventorySpool) => void;
  spoolDisplayTemplate?: string;
};

// Column header labels (25 columns)
const columnHeaders: Record<string, (t: TFn) => string> = {
  id: (t) => t('inventory.columns.id'),
  display_name: (t) => t('inventory.columns.display_name'),
  purchase_date: (t) => t('inventory.columns.purchase_date'),
  added_time: (t) => t('inventory.columns.added_time'),
  encode_time: (t) => t('inventory.columns.encode_time'),
  last_used_time: (t) => t('inventory.columns.last_used_time'),
  rgba: (t) => t('inventory.columns.rgba'),
  material: (t) => t('inventory.columns.material'),
  subtype: (t) => t('inventory.columns.subtype'),
  color_name: (t) => t('inventory.columns.color_name'),
  // Same label as ``rgba`` — pseudo-column inserted by toRenderColumns()
  // when both swatch and name are visible.
  color_combined: (t) => t('inventory.columns.rgba'),
  brand: (t) => t('inventory.columns.brand'),
  slicer_filament: (t) => t('inventory.columns.slicer_filament'),
  location: (t) => t('inventory.columns.location'),
  storage_location: (t) => t('inventory.storageLocation'),
  purchase_location: (t) => t('inventory.purchaseLocation'),
  label_weight: (t) => t('inventory.columns.label_weight'),
  net: (t) => t('inventory.columns.net'),
  gross: (t) => t('inventory.columns.gross'),
  added_full: (t) => t('inventory.columns.added_full'),
  used: (t) => t('inventory.columns.used'),
  printed_total: (t) => t('inventory.columns.printed_total'),
  printed_since_weight: (t) => t('inventory.columns.printed_since_weight'),
  note: (t) => t('inventory.columns.note'),
  pa_k: (t) => t('inventory.columns.pa_k'),
  tag_id: (t) => t('inventory.columns.tag_id'),
  data_origin: (t) => t('inventory.columns.data_origin'),
  tag_type: (t) => t('inventory.columns.tag_type'),
  stock: (t) => t('inventory.columns.stock'),
  remaining: (t) => t('inventory.columns.remaining'),
  spool_name: (t) => t('inventory.columns.spool_name'),
  cost_per_kg: (t) => t('inventory.columns.cost_per_kg'),
  weight_check: (t) => t('inventory.columns.weight_check'),
  filament_diameter: (t) => t('inventory.columns.filament_diameter'),
  lot: (t) => t('inventory.columns.lot'),
};

// Column cell renderers (25 columns)
const columnCells: Record<string, (ctx: CellCtx) => ReactNode> = {
  id: ({ spool }) => (
    <span className="text-sm font-medium text-white">{spool.id}</span>
  ),
  display_name: ({ spool, spoolDisplayTemplate }) => (
    <span className="text-sm text-white truncate">
      {formatSpoolDisplayName(spool, spoolDisplayTemplate)}
    </span>
  ),
  added_time: ({ spool, dateFormat }) => (
    <span className="text-sm text-bambu-gray">{formatInventoryDate(spool.created_at, dateFormat)}</span>
  ),
  purchase_date: ({ spool, dateFormat }) => (
    <span className="text-sm text-bambu-gray">{spool.purchase_date ? formatInventoryDate(spool.purchase_date, dateFormat) : '-'}</span>
  ),
  encode_time: ({ spool, dateFormat }) => (
    <span className="text-sm text-bambu-gray">{formatInventoryDate(spool.encode_time, dateFormat)}</span>
  ),
  last_used_time: ({ spool, dateFormat }) => (
    <span className="text-sm text-bambu-gray">{spool.last_used ? formatInventoryDate(spool.last_used, dateFormat) : 'Never'}</span>
  ),
  rgba: ({ spool }) => (
    <div className="flex items-center justify-center">
      <span
        className="w-5 h-5 rounded-full border border-black/20 flex-shrink-0"
        style={spoolSwatchStyle(spool)}
        title={spool.rgba ? `#${spool.rgba.substring(0, 6)}` : undefined}
      />
    </div>
  ),
  material: ({ spool }) => (
    <span className="text-sm text-white">{spool.material}</span>
  ),
  subtype: ({ spool }) => (
    <span className="text-sm text-bambu-gray">{spool.subtype || '-'}</span>
  ),
  color_name: ({ spool }) => (
    <span className="text-sm text-bambu-gray">{resolveSpoolColorName(spool.color_name, spool.rgba) || '-'}</span>
  ),
  // Merged cell for when both "rgba" swatch and "color_name" are visible —
  // see toRenderColumns(). Settings panel keeps the two separate entries so
  // the user can still hide one independently.
  color_combined: ({ spool }) => (
    <div className="flex items-center gap-2">
      <span
        className="w-5 h-5 rounded-full border border-black/20 flex-shrink-0"
        style={spoolSwatchStyle(spool)}
        title={spool.rgba ? `#${spool.rgba.substring(0, 6)}` : undefined}
      />
      <span className="text-sm text-bambu-gray truncate">
        {resolveSpoolColorName(spool.color_name, spool.rgba) || '-'}
      </span>
    </div>
  ),
  brand: ({ spool }) => (
    <span className="text-sm text-bambu-gray">{spool.brand || '-'}</span>
  ),
  slicer_filament: ({ spool }) => (
    <span className="text-sm text-bambu-gray" title={spool.slicer_filament || undefined}>
      {spool.slicer_filament_name || spool.slicer_filament || '-'}
    </span>
  ),
  location: ({ spool, assignmentMap }) => {
    const assignment = assignmentMap[spool.id];
    if (!assignment) return <span className="text-sm text-bambu-gray">-</span>;
    const printerLabel = assignment.printer_name || `Printer ${assignment.printer_id}`;
    const isExternal = assignment.ams_id === 254 || assignment.ams_id === 255;
    const isHt = !isExternal && assignment.ams_id >= 128;
    const slotLabel = formatSlotLabel(assignment.ams_id, assignment.tray_id, isHt, isExternal);
    return (
      <span className="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-purple-100 dark:bg-purple-500/20 text-purple-700 dark:text-purple-400">
        {printerLabel} {slotLabel}{assignment.ams_label ? ` (${assignment.ams_label})` : ''}
      </span>
    );
  },
  storage_location: ({ spool }) => {
    if (!spool.storage_location) return <span className="text-sm text-bambu-gray">-</span>;
    return (
      <span className="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-blue-100 dark:bg-blue-500/20 text-blue-700 dark:text-blue-400">
        {spool.storage_location}
      </span>
    );
  },
  purchase_location: ({ spool }) => {
    if (!spool.purchase_location) return <span className="text-sm text-bambu-gray">-</span>;
    return (
      <span className="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-purple-100 dark:bg-purple-500/20 text-purple-700 dark:text-purple-400">
        {spool.purchase_location}
      </span>
    );
  },
  label_weight: ({ spool }) => (
    <span className="text-sm text-white">{formatWeight(spool.label_weight)}</span>
  ),
  net: ({ remaining }) => (
    <span className="text-sm text-white">{formatWeight(remaining)}</span>
  ),
  gross: ({ spool, remaining }) => (
    <span className="text-sm text-bambu-gray">{formatWeight(remaining + spool.core_weight)}</span>
  ),
  added_full: ({ spool }) => (
    <span className="text-sm text-bambu-gray">{spool.added_full == null ? '-' : spool.added_full ? 'Yes' : 'No'}</span>
  ),
  used: ({ spool }) => (
    <span className="text-sm text-bambu-gray">{spool.weight_used > 0 ? formatWeight(spool.weight_used) : '-'}</span>
  ),
  printed_total: () => (
    <span className="text-sm text-bambu-gray/50">-</span>
  ),
  printed_since_weight: () => (
    <span className="text-sm text-bambu-gray/50">-</span>
  ),
  note: ({ spool }) => (
    <span className="text-sm text-bambu-gray max-w-[150px] truncate block" title={spool.note || undefined}>{spool.note || '-'}</span>
  ),
  pa_k: ({ spool }) => {
    const count = spool.k_profiles?.length ?? 0;
    if (count === 0) return <span className="text-sm text-bambu-gray">-</span>;
    return (
      <span className="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-bambu-green/20 text-bambu-green">
        K
      </span>
    );
  },
  tag_id: ({ spool }) => {
    const tag = spool.tag_uid || spool.tray_uuid;
    if (!tag) return <span className="text-sm text-bambu-gray/50">-</span>;
    return (
      <span className="text-sm text-bambu-gray font-mono" title={tag}>
        {tag.length > 12 ? `${tag.slice(0, 6)}...${tag.slice(-4)}` : tag}
      </span>
    );
  },
  data_origin: ({ spool }) => (
    <span className="text-sm text-bambu-gray">{spool.data_origin || '-'}</span>
  ),
  tag_type: ({ spool }) => (
    <span className="text-sm text-bambu-gray">{spool.tag_type || '-'}</span>
  ),
  stock: ({ spool, t }) => {
    if (!spool.slicer_filament) {
      return (
        <span className="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400">
          {t('inventory.stock')}
        </span>
      );
    }
    return <span className="text-sm text-bambu-gray">-</span>;
  },
  remaining: ({ remaining, pct }) => (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-2 bg-bambu-dark-tertiary rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full ${pct > 50 ? 'bg-bambu-green' : pct > 20 ? 'bg-yellow-500' : 'bg-red-500'}`}
          style={{ width: `${Math.min(pct, 100)}%` }}
        />
      </div>
      <span className="text-xs text-bambu-gray min-w-[40px] text-right">{Math.round(remaining)}g</span>
    </div>
  ),
  spool_name: ({ spool, catalogMap }) => {
    const entry = spool.core_weight_catalog_id != null ? catalogMap[spool.core_weight_catalog_id] : undefined;
    return <span className="text-sm text-bambu-gray">{entry?.name || '-'}</span>;
  },
  cost_per_kg: ({ spool, currencySymbol }) => (
    <span className="text-sm text-bambu-gray">
      {spool.cost_per_kg != null ? `${currencySymbol}${spool.cost_per_kg.toFixed(2)}` : '-'}
    </span>
  ),
  weight_check: ({ spool, onSyncWeight }) => {
    const scaleWeight = spool.last_scale_weight;
    if (scaleWeight == null) return <span className="text-sm text-bambu-gray/50" title="No scale measurement">-</span>;

    const coreWeight = spool.core_weight || 0;
    const calculatedWeight = Math.max(0, spool.label_weight - spool.weight_used) + coreWeight;

    // Edge case: scale < core_weight means spool is empty or not on scale - treat as match
    let difference: number;
    let isMatch: boolean;
    if (scaleWeight < coreWeight) {
      difference = scaleWeight - coreWeight;
      isMatch = true;
    } else {
      difference = scaleWeight - calculatedWeight;
      isMatch = Math.abs(difference) <= 50;
    }

    const diffStr = difference > 0 ? `+${Math.round(difference)}` : `${Math.round(difference)}`;
    const tooltip = isMatch
      ? `Scale: ${Math.round(scaleWeight)}g\nCalculated: ${Math.round(calculatedWeight)}g\nDifference: ${diffStr}g (within tolerance)`
      : `Scale: ${Math.round(scaleWeight)}g\nCalculated: ${Math.round(calculatedWeight)}g\nDifference: ${diffStr}g (mismatch!)`;

    return (
      <div
        className={`flex items-center gap-1 text-sm font-medium ${isMatch ? 'text-green-700 dark:text-green-400' : 'text-yellow-700 dark:text-yellow-400'}`}
        title={tooltip}
      >
        <span>{Math.round(scaleWeight)}g</span>
        {isMatch ? (
          <Check className="w-3 h-3" />
        ) : (
          <>
            <AlertTriangle className="w-3 h-3" />
            {onSyncWeight && (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  e.preventDefault();
                  onSyncWeight(spool);
                }}
                className="p-1 hover:bg-bambu-green/20 rounded transition-colors text-bambu-green"
                title="Sync: trust scale weight and reset tracking"
              >
                <RefreshCw className="w-3.5 h-3.5" />
              </button>
            )}
          </>
        )}
      </div>
    );
  },
  filament_diameter: ({ spool }) => (
    <span className="text-sm text-bambu-gray font-mono">
      {spool.filament_diameter ? `${spool.filament_diameter} mm` : '-'}
    </span>
  ),
  lot: ({ spool }) => (
    <span className="text-sm text-bambu-gray font-mono">{spool.lot ?? '-'}</span>
  ),
};

// Sort value extractors - return a comparable value for each sortable column
// LocationDisplay (Spoolman-mode) augments the legacy SpoolAssignment shape (BamDude PR #1241 port).
const columnSortValues: Record<string, (spool: InventorySpool, assignmentMap: Record<number, SpoolAssignment | LocationDisplay>) => string | number> = {
  id: (s) => s.id,
  // display_name is sortable — the real comparison lives in the sortedSpools
  // memo (it needs the user's configurable template which module scope can't
  // see). This stub only marks the column as sortable so handleSort accepts
  // header clicks and the ArrowUpDown affordance renders.
  display_name: () => 0,
  added_time: (s) => s.created_at || '',
  purchase_date: (s) => s.purchase_date || '',
  encode_time: (s) => s.encode_time || '',
  last_used_time: (s) => s.last_used || '',
  material: (s) => (s.material || '').toLowerCase(),
  subtype: (s) => (s.subtype || '').toLowerCase(),
  // The NAME column sorts by name — alphabetical is what it says on the header.
  color_name: (s) => (s.color_name || '').toLowerCase(),
  // ⚠️ The swatch columns sort by COLOUR. The swatch column had no extractor at
  // all, so its header ignored clicks; the combined one sorted by name, which
  // files a titanium grey under T and a burgundy under B — an ordering nobody
  // reading a row of swatches can follow.
  rgba: (s) => colorSortKey(s.rgba),
  color_combined: (s) => colorSortKey(s.rgba),
  brand: (s) => (s.brand || '').toLowerCase(),
  slicer_filament: (s) => (s.slicer_filament_name || s.slicer_filament || '').toLowerCase(),
  location: (s, am) => {
    const a = am[s.id];
    if (!a) return '';
    const isExt = a.ams_id === 254 || a.ams_id === 255;
    const isHt = !isExt && a.ams_id >= 128;
    const label = a.ams_label ? ` (${a.ams_label})` : '';
    return `${a.printer_name || ''} ${formatSlotLabel(a.ams_id, a.tray_id, isHt, isExt)}${label}`;
  },
  storage_location: (s) => (s.storage_location || '').toLowerCase(),
  purchase_location: (s) => (s.purchase_location || '').toLowerCase(),
  label_weight: (s) => s.label_weight,
  net: (s) => Math.max(0, s.label_weight - s.weight_used),
  gross: (s) => Math.max(0, s.label_weight - s.weight_used) + s.core_weight,
  used: (s) => s.weight_used,
  remaining: (s) => s.label_weight > 0 ? Math.max(0, s.label_weight - s.weight_used) / s.label_weight : 0,
  note: (s) => (s.note || '').toLowerCase(),
  data_origin: (s) => (s.data_origin || '').toLowerCase(),
  tag_type: (s) => (s.tag_type || '').toLowerCase(),
  stock: (s) => s.slicer_filament ? 1 : 0,
  spool_name: (s) => s.core_weight_catalog_id ?? 0,
  cost_per_kg: (s) => s.cost_per_kg ?? 0,
  filament_diameter: (s) => s.filament_diameter || '',
  lot: (s) => s.lot ?? 0,
  weight_check: (s) => {
    if (s.last_scale_weight == null) return -1;
    const expectedGross = Math.max(0, s.label_weight - s.weight_used) + s.core_weight;
    return Math.abs(s.last_scale_weight - expectedGross);
  },
};

const SORT_STATE_KEY = 'bamdude-inventory-sort';

function loadSortState(): SortState {
  try {
    const stored = localStorage.getItem(SORT_STATE_KEY);
    if (stored) return JSON.parse(stored);
  } catch { /* ignore */ }
  return null;
}

function saveSortState(state: SortState) {
  try {
    if (state) {
      localStorage.setItem(SORT_STATE_KEY, JSON.stringify(state));
    } else {
      localStorage.removeItem(SORT_STATE_KEY);
    }
  } catch { /* ignore */ }
}

// Wrapper: detects Spoolman mode and passes it to the shared inventory UI
export default function InventoryPageRouter() {
  const { t } = useTranslation();
  const { data: spoolmanSettings, isLoading: spoolmanSettingsLoading } = useQuery({
    queryKey: ['spoolman-settings'],
    queryFn: api.getSpoolmanSettings,
    staleTime: 5 * 60 * 1000,
  });
  const spoolmanMode = spoolmanSettings?.spoolman_enabled === 'true';
  const spoolmanModeReady = !spoolmanSettingsLoading;

  if (spoolmanSettings?.spoolman_enabled === 'true' && spoolmanSettings?.spoolman_url) {
    const spoolmanUrl = spoolmanSettings.spoolman_url.replace(/\/+$/, '');
    // Browsers block HTTP iframes inside HTTPS parents (mixed-content rule,
    // independent of CSP). Surface a targeted warning instead of letting
    // the iframe render as a silent blank — there's nothing BamDude can do
    // browser-side to override the block. See upstream issue #1096.
    const bamdudeIsHttps = window.location.protocol === 'https:';
    const spoolmanIsHttp = spoolmanUrl.toLowerCase().startsWith('http://');
    if (bamdudeIsHttps && spoolmanIsHttp) {
      return (
        <div className="p-6 max-w-3xl mx-auto">
          <div className="rounded-lg border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/20 p-5">
            <div className="flex items-start gap-3">
              <AlertTriangle className="w-5 h-5 text-amber-600 dark:text-amber-400 mt-0.5 shrink-0" />
              <div className="flex-1 min-w-0 space-y-2 text-sm">
                <p className="font-semibold text-amber-900 dark:text-amber-100">
                  {t('inventory.spoolmanMixedContentTitle')}
                </p>
                <p className="text-amber-800 dark:text-amber-200">
                  {t('inventory.spoolmanMixedContentBody')}
                </p>
                <ul className="list-disc pl-5 space-y-1 text-amber-800 dark:text-amber-200">
                  <li>{t('inventory.spoolmanMixedContentFixReverseProxy')}</li>
                  <li>{t('inventory.spoolmanMixedContentFixOpenNewTab')}</li>
                </ul>
                <div className="flex flex-wrap gap-2 pt-2">
                  <a
                    href={`${spoolmanUrl}/spool`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-2 px-3 py-1.5 text-sm rounded bg-amber-600 hover:bg-amber-700 text-white"
                  >
                    {t('inventory.spoolmanOpenInNewTab')}
                  </a>
                </div>
              </div>
            </div>
          </div>
        </div>
      );
    }
    return (
      <iframe
        src={`${spoolmanUrl}/spool`}
        className="h-full w-full border-0"
        title="Spoolman"
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"
      />
    );
  }

  return <InventoryPage spoolmanMode={spoolmanMode} spoolmanModeReady={spoolmanModeReady} />;
}

function InventoryPage({ spoolmanMode = false, spoolmanModeReady = true }: { spoolmanMode?: boolean; spoolmanModeReady?: boolean }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasPermission, loading: authLoading } = useAuth();
  // Forecast tab gated on perm + non-Spoolman mode (Spoolman proxies spools, so we
  // can't read spool_usage_history through the iframe).
  const canViewForecast = !authLoading && !spoolmanMode && hasPermission('inventory:forecast_read');
  const [searchParams, setSearchParams] = useSearchParams();
  const [formModal, setFormModal] = useState<{ spool?: InventorySpool | null; mode: SpoolFormMode } | null>(null);
  const deepLinkHandled = useRef(false);
  const [confirmAction, setConfirmAction] = useState<
    | { type: 'delete' | 'archive' | 'reset-consumed-counter'; spoolId: number }
    | { type: 'reset-all-consumed-counters' }
    | null
  >(null);
  // Label printing (B.1 #809). null = closed; otherwise the IDs to pre-check.
  const [labelPickerSpoolIds, setLabelPickerSpoolIds] = useState<number[] | null>(null);
  const [showBulkEdit, setShowBulkEdit] = useState(false);

  // Filter state
  // --- Bulk selection (#1795) -------------------------------------------
  //
  // Row checkboxes drive every bulk action. The previous behaviour — "edit
  // whatever the filter currently shows" — is preserved as an explicit
  // toolbar action ("select all matching the filter"), so nothing is lost,
  // but Delete and Archive can no longer fire against a set the user never
  // looked at.
  //
  // The selection is cleared whenever the visible set changes underneath it
  // (filter, search, tab, grouping), because a count in the toolbar that no
  // longer matches what is on screen is worse than no selection at all.
  const [selectedIds, setSelectedIds] = useState<Set<number>>(() => new Set());
  const [bulkPending, setBulkPending] = useState(false);
  // Destructive bulk actions go through a confirmation — deleting a selection
  // is not undoable and the count can be large.
  const [confirmBulk, setConfirmBulk] = useState<'archive' | 'restore' | 'delete' | null>(null);

  const toggleSelected = (id: number) =>
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const setManySelected = (ids: number[], selected: boolean) =>
    setSelectedIds((prev) => {
      const next = new Set(prev);
      for (const id of ids) {
        if (selected) next.add(id);
        else next.delete(id);
      }
      return next;
    });

  const clearSelection = () => setSelectedIds(new Set());

  // Filters are restored from localStorage — see `loadFilters`. Read ONCE into
  // the initialisers rather than pushed in by an effect: a later write would be
  // a second render with a different list, and the selection-clearing effect
  // below would fire on it.
  const [storedFilters] = useState(loadFilters);

  const [archiveFilter, setArchiveFilter] = useState<ArchiveFilter>(storedFilters.archiveFilter);
  const [usageFilter, setUsageFilter] = useState<UsageFilter>(storedFilters.usageFilter);
  const [materialFilter, setMaterialFilter] = useState(storedFilters.materialFilter);
  const [brandFilter, setBrandFilter] = useState(storedFilters.brandFilter);
  // Filter by resolved colour NAME (single source of truth via the colour
  // catalog), with options drawn from existing (non-archived) spools only.
  const [colorFilter, setColorFilter] = useState(storedFilters.colorFilter);
  // Re-resolve colour-name options once the colour catalog finishes loading.
  const colorCatalogVersion = useColorCatalogVersion();
  const [categoryFilter, setCategoryFilter] = useState(storedFilters.categoryFilter);
  const [spoolFilter, setSpoolFilter] = useState(storedFilters.spoolFilter);
  const [stockFilter, setStockFilter] = useState<StockFilter>(storedFilters.stockFilter);
  const [assignedFilter, setAssignedFilter] = useState<AssignedFilter>(storedFilters.assignedFilter);
  const [search, setSearch] = useState(storedFilters.search);
  const [viewMode, setViewMode] = useState<ViewMode>(storedFilters.viewMode);
  const [sortState, setSortState] = useState<SortState>(loadSortState);
  const [columnConfig, setColumnConfig] = useState<ColumnConfig[]>(loadColumnConfig);
  const [showColumnModal, setShowColumnModal] = useState(false);
  const [groupSimilar, setGroupSimilar] = useState(() => {
    try {
      return localStorage.getItem('bamdude-inventory-group') === 'true';
    } catch { return false; }
  });
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());

  // Pagination state (pageSize persisted to localStorage)
  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(() => {
    try {
      const stored = localStorage.getItem('bamdude-inventory-pageSize');
      if (stored) {
        const n = Number(stored);
        if ([12, 24, 48, 96, -1].includes(n)) return n;
      }
    } catch { /* ignore */ }
    return 24;
  });

  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
  });

  const dateFormat: DateFormat = settings?.date_format || 'system';
  const spoolDisplayTemplate = settings?.spool_display_template || DEFAULT_SPOOL_DISPLAY_TEMPLATE;

  // CSV import/export (#1576). Local inventory only — disabled in Spoolman mode
  // (Spoolman owns the data store and has its own CSV import/export there).
  const [csvImportOpen, setCsvImportOpen] = useState(false);
  const [exportingCsv, setExportingCsv] = useState(false);
  // Structured storage-location catalog manager (upstream #1505).
  const [locationsModalOpen, setLocationsModalOpen] = useState(false);
  const handleExportCsv = useCallback(async () => {
    setExportingCsv(true);
    try {
      await api.exportSpoolsCsv();
    } catch (err) {
      showToast(err instanceof Error ? err.message : t('inventory.csv.exportError', 'Export failed'), 'error');
    } finally {
      setExportingCsv(false);
    }
  }, [showToast, t]);
  // Tooltip on both CSV buttons when they're disabled in Spoolman mode.
  const spoolmanCsvHint = spoolmanMode
    ? t('inventory.csv.spoolmanHint', 'In Spoolman mode, use Spoolman\'s built-in CSV import/export.')
    : undefined;

  // Query key and fetch function differ based on data source
  const spoolsQueryKey = spoolmanMode ? ['spoolman-inventory-spools'] : ['inventory-spools'];
  const refreshSpoolQueries = () => invalidateSpoolAndLocationQueries(queryClient, spoolsQueryKey);
  const { data: spools, isLoading } = useQuery({
    queryKey: spoolsQueryKey,
    queryFn: () =>
      spoolmanMode ? api.getSpoolmanInventorySpools(true) : api.getSpools(true),
    refetchInterval: 30000,
  });

  // Structured storage-location catalog + spool counts (upstream #1505).
  const { data: storageLocations = [] } = useQuery({
    queryKey: inventoryLocationsQueryKey,
    queryFn: api.getLocations,
  });

  // Storage-location filter is deep-linkable via ?location_id=<id> (or the
  // sentinel ``__none__`` for spools with no location set), same pattern as the
  // ?spool=<id> edit deep-link below.
  const _rawLocationParam = searchParams.get('location_id');
  const storageLocationFilter =
    _rawLocationParam === '__none__'
      ? '__none__'
      : _rawLocationParam && /^\d+$/.test(_rawLocationParam) && Number(_rawLocationParam) > 0
        ? _rawLocationParam
        : '';

  // Deep-link: open edit modal for ?spool=<id>
  // Prefer the already-loaded spool list (no extra API call); fall back to a
  // targeted fetch for the rare case where the full list hasn't arrived yet.
  const _rawSpoolParam = searchParams.get('spool');
  // Only accept strings of digits representing a positive integer — guards against
  // NaN (Number('abc')), 0, negatives, and floats like '1.5' that would produce
  // an invalid path parameter and trigger unnecessary 422 responses from the API.
  const deepLinkSpoolId =
    _rawSpoolParam && /^\d+$/.test(_rawSpoolParam) && Number(_rawSpoolParam) > 0
      ? Number(_rawSpoolParam)
      : null;
  const deepLinkInList = spools?.find((s) => s.id === deepLinkSpoolId) ?? null;

  const clearDeepLinkParam = useCallback(() => {
    deepLinkHandled.current = true;
    setSearchParams((prev) => { prev.delete('spool'); return prev; }, { replace: true });
  }, [setSearchParams]);

  // Targeted fetch — only fires when mode is known and spool isn't in the list yet
  const { data: deepLinkSpool, isError: deepLinkFetchFailed, error: deepLinkError } = useQuery({
    queryKey: spoolmanMode
      ? ['spoolman-inventory-spool', deepLinkSpoolId]
      : ['inventory-spool', deepLinkSpoolId],
    queryFn: () =>
      spoolmanMode
        ? api.getSpoolmanInventorySpool(deepLinkSpoolId!)
        : api.getSpool(deepLinkSpoolId!),
    enabled: spoolmanModeReady && deepLinkSpoolId !== null && deepLinkInList === null,
    staleTime: Infinity,
    retry: (failureCount, error) =>
      failureCount < 2 && !(error instanceof ApiError && error.status === 404),
  });

  useEffect(() => {
    if (deepLinkHandled.current) return;

    // Case 1: spool is already in the fetched list
    if (spoolmanModeReady && deepLinkSpoolId && deepLinkInList) {
      clearDeepLinkParam();
      setFormModal({ spool: deepLinkInList, mode: 'edit' });
      return;
    }

    // Case 2: spool was fetched individually
    if (deepLinkSpool) {
      clearDeepLinkParam();
      setFormModal({ spool: deepLinkSpool, mode: 'edit' });
      return;
    }

    // Case 3: fetch failed
    if (deepLinkFetchFailed) {
      clearDeepLinkParam();
      const is404 = deepLinkError instanceof ApiError && deepLinkError.status === 404;
      showToast(t(is404 ? 'inventory.deepLinkSpoolNotFound' : 'inventory.deepLinkFetchFailed'), 'error');
    }
  }, [
    spoolmanModeReady,
    deepLinkSpoolId,
    deepLinkInList,
    deepLinkSpool,
    deepLinkFetchFailed,
    deepLinkError,
    clearDeepLinkParam,
    showToast,
    t,
  ]);

  const { data: assignments } = useQuery({
    queryKey: ['spool-assignments'],
    queryFn: () => api.getAssignments(),
    refetchInterval: 30000,
  });

  // Spoolman-mode slot assignments. spool.id IS the spoolman_spool_id, so this
  // feeds into the same assignmentMap that the LOCATION column reads.
  const {
    data: spoolmanSlotAssignments = [],
    isError: spoolmanSlotAssignmentsError,
  } = useQuery({
    queryKey: ['spoolman-slot-assignments-all'],
    queryFn: () => api.getSpoolmanSlotAssignments(),
    enabled: spoolmanMode,
    refetchInterval: 30000,
    staleTime: 10000,
    retry: 1,
  });

  // Surface a single toast when the slot-assignment endpoint goes down — the
  // LOCATION column would otherwise silently show "-" for every Spoolman spool.
  // useRef guard prevents repeated toasts during refetchInterval polls.
  const slotErrorToastShown = useRef(false);
  useEffect(() => {
    if (spoolmanSlotAssignmentsError && !slotErrorToastShown.current) {
      slotErrorToastShown.current = true;
      showToast(t('inventory.spoolmanUnreachable'), 'error');
    } else if (!spoolmanSlotAssignmentsError) {
      slotErrorToastShown.current = false;
    }
  }, [spoolmanSlotAssignmentsError, showToast, t]);

  const { data: catalogEntries } = useQuery({
    queryKey: ['spool-catalog'],
    queryFn: () => api.getSpoolCatalog(),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) =>
      spoolmanMode ? api.deleteSpoolmanInventorySpool(id) : api.deleteSpool(id),
    onSuccess: () => {
      refreshSpoolQueries();
      showToast(t('inventory.spoolDeleted'), 'success');
    },
    onError: (error: Error) => {
      if (error instanceof ApiError && error.status === 404) {
        showToast(t('inventory.deleteSpoolNotFound'), 'error');
      } else if (error instanceof ApiError && error.status === 503) {
        showToast(t('inventory.spoolmanUnreachable'), 'error');
      } else {
        showToast(t('inventory.deleteFailed'), 'error');
      }
    },
  });

  const archiveMutation = useMutation({
    mutationFn: (id: number) =>
      spoolmanMode ? api.archiveSpoolmanInventorySpool(id) : api.archiveSpool(id),
    onSuccess: () => {
      refreshSpoolQueries();
      showToast(t('inventory.spoolArchived'), 'success');
    },
    onError: (error: Error) => {
      if (error instanceof ApiError && error.status === 404) {
        showToast(t('inventory.archiveSpoolNotFound'), 'error');
      } else if (error instanceof ApiError && error.status === 503) {
        showToast(t('inventory.spoolmanUnreachable'), 'error');
      } else {
        showToast(t('inventory.archiveFailed'), 'error');
      }
    },
  });

  const restoreMutation = useMutation({
    mutationFn: (id: number) =>
      spoolmanMode ? api.restoreSpoolmanInventorySpool(id) : api.restoreSpool(id),
    onSuccess: () => {
      refreshSpoolQueries();
      showToast(t('inventory.spoolRestored'), 'success');
    },
    onError: (error: Error) => {
      if (error instanceof ApiError && error.status === 404) {
        showToast(t('inventory.restoreSpoolNotFound'), 'error');
      } else if (error instanceof ApiError && error.status === 503) {
        showToast(t('inventory.spoolmanUnreachable'), 'error');
      } else {
        showToast(t('inventory.restoreFailed'), 'error');
      }
    },
  });

  const resetConsumedCounterMutation = useMutation({
    mutationFn: (id: number) =>
      spoolmanMode ? api.resetSpoolmanInventorySpoolConsumedCounter(id) : api.resetSpoolConsumedCounter(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: spoolsQueryKey });
      showToast(t('inventory.consumedCounterReset'), 'success');
    },
    onError: () => {
      showToast(t('inventory.resetConsumedCounterFailed'), 'error');
    },
  });

  const bulkResetConsumedCounterMutation = useMutation({
    mutationFn: (ids: number[]) =>
      spoolmanMode
        ? api.bulkResetSpoolmanInventorySpoolConsumedCounter(ids)
        : api.bulkResetSpoolConsumedCounter(ids),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: spoolsQueryKey });
      showToast(t('inventory.allConsumedCountersReset', { count: data.reset }), 'success');
    },
    onError: () => {
      showToast(t('inventory.resetConsumedCounterFailed'), 'error');
    },
  });

  // Bulk archive / restore / delete over the current selection (#1795).
  //
  // Both inventory modes are wired: Spoolman had none of these endpoints at
  // all, so mass actions there used to be impossible. The two backends report
  // differently — built-in returns counts plus `not_found`, Spoolman returns
  // counts plus a per-row `errors` list, because it is a remote service where
  // individual rows can fail — so the toast branches on all-succeeded /
  // partial / all-failed rather than always claiming success.
  const runBulk = async (
    kind: 'archive' | 'restore' | 'delete',
    ids: number[],
  ): Promise<{ ok: number; failed: number }> => {
    if (spoolmanMode) {
      const res =
        kind === 'archive'
          ? await api.bulkArchiveSpoolmanInventorySpools(ids)
          : kind === 'restore'
            ? await api.bulkRestoreSpoolmanInventorySpools(ids)
            : await api.bulkDeleteSpoolmanInventorySpools(ids);
      const ok = res.archived ?? res.restored ?? res.deleted ?? 0;
      return { ok, failed: res.errors?.length ?? 0 };
    }
    const res =
      kind === 'archive'
        ? await api.bulkArchiveSpools(ids)
        : kind === 'restore'
          ? await api.bulkRestoreSpools(ids)
          : await api.bulkDeleteSpools(ids);
    const ok = res.archived ?? res.restored ?? res.deleted ?? 0;
    return { ok, failed: res.not_found?.length ?? 0 };
  };

  const bulkActionMutation = useMutation({
    mutationFn: ({ kind, ids }: { kind: 'archive' | 'restore' | 'delete'; ids: number[] }) =>
      runBulk(kind, ids),
    onMutate: () => setBulkPending(true),
    onSettled: () => setBulkPending(false),
    onSuccess: ({ ok, failed }, { kind }) => {
      queryClient.invalidateQueries({ queryKey: spoolsQueryKey });
      const label = t(`inventory.bulk.action.${kind}`);
      if (ok > 0 && failed === 0) {
        showToast(t('inventory.bulk.done', { action: label, count: ok }), 'success');
        clearSelection();
      } else if (ok > 0) {
        // Partial: say so, and KEEP the selection so the user can retry the rest.
        showToast(t('inventory.bulk.partial', { action: label, ok, failed }), 'warning');
      } else {
        showToast(t('inventory.bulk.failed', { action: label, count: failed }), 'error');
      }
    },
    onError: (error: Error) => showToast(error.message || t('inventory.bulk.failedGeneric'), 'error'),
  });

  // ``resetableSpoolIds`` is the target of the "Reset all usage" bulk
  // action. Includes archived spools so a one-click "clear the lifetime
  // counter" actually clears the lifetime counter — archived consumed
  // weight now counts in Total Consumed too (#1390 follow-up).
  const resetableSpoolIds = useMemo(() => (spools ?? []).map((s) => s.id), [spools]);

  // Low stock threshold from backend settings
  const lowStockThreshold = settings?.low_stock_threshold ?? 20;
  const [showThresholdInput, setShowThresholdInput] = useState(false);
  const [thresholdInput, setThresholdInput] = useState(lowStockThreshold.toString());

  // Sync thresholdInput when lowStockThreshold changes and input is not shown
  useEffect(() => {
    if (!showThresholdInput) {
      setThresholdInput(lowStockThreshold.toString());
    }
  }, [lowStockThreshold, showThresholdInput]);

  const updateThresholdMutation = useMutation({
    mutationFn: (threshold: number) => api.updateSettings({ low_stock_threshold: threshold }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['settings'] });
      showToast(t('common.saved'), 'success');
      setShowThresholdInput(false);
    },
    onError: () => {
      showToast(t('inventory.lowStockThresholdError'), 'error');
    },
  });

  // Stats calculation (active spools only)
  const stats = useMemo(() => {
    if (!spools) return null;
    let totalWeight = 0;
    let totalConsumed = 0;
    let lowStock = 0;
    let activeCount = 0;
    const byMaterial: Record<string, { count: number; weight: number }> = {};
    for (const s of spools) {
      // "Total Consumed" is the resettable lifetime counter
      // (``weight_used - baseline``). Past consumption of an archived
      // spool is real history and must stay in the running total — so
      // this aggregation happens BEFORE the archived-skip below
      // (#1390 follow-up). Pre-m075 servers don't send the baseline
      // field — ``?? 0`` falls back to the old "raw weight_used" displayed value.
      totalConsumed += Math.max(0, s.weight_used - (s.weight_used_baseline ?? 0));
      if (s.archived_at) continue;
      activeCount++;
      const remaining = Math.max(0, s.label_weight - s.weight_used);
      totalWeight += remaining;
      const pct = s.label_weight > 0 ? (remaining / s.label_weight) * 100 : 0;
      // B.8 — per-spool override falls back to the global setting when NULL.
      const threshold = s.low_stock_threshold_pct ?? lowStockThreshold;
      if (pct < threshold) lowStock++;
      const mat = s.material || 'Unknown';
      if (!byMaterial[mat]) byMaterial[mat] = { count: 0, weight: 0 };
      byMaterial[mat].count++;
      byMaterial[mat].weight += remaining;
    }
    return { totalWeight, totalConsumed, lowStock, byMaterial, totalSpools: activeCount };
  }, [spools, lowStockThreshold]);

  const inPrinterCount =
    (assignments?.length ?? 0) + (spoolmanMode ? spoolmanSlotAssignments.length : 0);

  const currencySymbol = getCurrencySymbol(settings?.currency || 'USD');

  // Map spool_id -> location display data for the LOCATION column.
  // Local SpoolAssignment entries first, then Spoolman SlotAssignment fills in
  // remaining IDs. Local wins on collision (defensive — modes are exclusive in
  // practice, but a stray pair with the same numeric id would otherwise be
  // unpredictable). spool.id IS the spoolman_spool_id in Spoolman mode.
  const assignmentMap = useMemo<Record<number, LocationDisplay>>(() => {
    const map: Record<number, LocationDisplay> = {};
    for (const a of assignments || []) {
      map[a.spool_id] = {
        printer_id: a.printer_id,
        printer_name: a.printer_name,
        ams_id: a.ams_id,
        tray_id: a.tray_id,
        ams_label: a.ams_label ?? null,
      };
    }
    for (const a of spoolmanSlotAssignments) {
      // Defensive: skip malformed entries (missing or invalid spool id, ams id,
      // tray id). The Pydantic response model on the backend should already
      // reject these, but MITM proxies and stale CDN responses can drop fields.
      if (
        typeof a?.spoolman_spool_id !== 'number' ||
        a.spoolman_spool_id <= 0 ||
        typeof a.printer_id !== 'number' ||
        typeof a.ams_id !== 'number' ||
        typeof a.tray_id !== 'number'
      ) continue;
      if (!map[a.spoolman_spool_id]) {
        map[a.spoolman_spool_id] = {
          printer_id: a.printer_id,
          printer_name: a.printer_name ?? null,
          ams_id: a.ams_id,
          tray_id: a.tray_id,
          ams_label: a.ams_label ?? null,
        };
      }
    }
    return map;
  }, [assignments, spoolmanSlotAssignments]);

  // Map catalog_id -> catalog entry for spool name column
  const catalogMap = useMemo(() => {
    const map: Record<number, SpoolCatalogEntry> = {};
    for (const e of catalogEntries || []) {
      map[e.id] = e;
    }
    return map;
  }, [catalogEntries]);

  // Materials by weight for the "By Material" stat card. Sorted heaviest
  // first so the dominant material lands at the top of the chip list.
  // No slice cap — operators with many materials (PLA / PETG / ABS / ASA /
  // TPU / PC / PA / Wood / etc.) need to see them all on the dashboard;
  // the card flex-wraps chips so it just grows in height when the list is
  // long, and the other 4 stat cards in the row stretch to match.
  const topMaterials = useMemo(() => {
    if (!stats) return [];
    return Object.entries(stats.byMaterial).sort((a, b) => b[1].weight - a[1].weight);
  }, [stats]);

  // Filtering pipeline
  const filteredSpools = useMemo(() => {
    let filtered = spools || [];

    // Archive filter
    if (archiveFilter === 'active') {
      filtered = filtered.filter((s) => !s.archived_at);
    } else {
      filtered = filtered.filter((s) => !!s.archived_at);
    }

    // Usage filter
    if (usageFilter === 'used') {
      filtered = filtered.filter((s) => s.weight_used > 0);
    } else if (usageFilter === 'new') {
      filtered = filtered.filter((s) => s.weight_used === 0);
    } else if (usageFilter === 'lowstock') {
      filtered = filtered.filter((s) => {
        const remaining = Math.max(0, s.label_weight - s.weight_used);
        const pct = s.label_weight > 0 ? (remaining / s.label_weight) * 100 : 0;
        // B.8 — per-spool override falls back to the global setting.
        const threshold = s.low_stock_threshold_pct ?? lowStockThreshold;
        return pct < threshold;
      });
    }

    // Material dropdown
    if (materialFilter) {
      filtered = filtered.filter((s) => s.material === materialFilter);
    }

    // Brand dropdown
    if (brandFilter) {
      filtered = filtered.filter((s) => s.brand === brandFilter);
    }

    // Colour dropdown — matches the resolved colour NAME (catalog single
    // source of truth), so two near-identical hexes that map to the same
    // name (e.g. both "Black") filter together.
    if (colorFilter) {
      filtered = filtered.filter((s) => resolveSpoolColorName(s.color_name, s.rgba) === colorFilter);
    }

    // Category dropdown (#729) — '__none__' picks uncategorised spools.
    if (categoryFilter) {
      if (categoryFilter === '__none__') {
        filtered = filtered.filter((s) => !s.category);
      } else {
        filtered = filtered.filter((s) => s.category === categoryFilter);
      }
    }

    // Spool name dropdown
    if (spoolFilter) {
      const catalogId = Number(spoolFilter);
      filtered = filtered.filter((s) => s.core_weight_catalog_id === catalogId);
    }

    // Storage location dropdown (upstream #1505 catalog). ``__none__`` lets the
    // user find spools with no location. Match on the FK when present; fall back
    // to the denormalized free-text string (legacy rows not yet re-saved).
    if (storageLocationFilter) {
      if (storageLocationFilter === '__none__') {
        filtered = filtered.filter((s) => !s.location_id && !s.storage_location?.trim());
      } else {
        const locId = Number(storageLocationFilter);
        const locName = storageLocations.find((l) => l.id === locId)?.name?.trim().toLowerCase();
        filtered = filtered.filter((s) => {
          if (s.location_id != null) return s.location_id === locId;
          if (locName) return (s.storage_location || '').trim().toLowerCase() === locName;
          return false;
        });
      }
    }

    // Stock filter
    if (stockFilter === 'stock') {
      filtered = filtered.filter((s) => !s.slicer_filament);
    } else if (stockFilter === 'configured') {
      filtered = filtered.filter((s) => !!s.slicer_filament);
    }

    // Loaded in a printer, or on the shelf. Reads the same `assignmentMap` the
    // Location column renders from, so the filter and the column can never
    // disagree — and so it answers for BOTH inventory backends: the map is
    // already merged from local assignments and Spoolman's slot assignments.
    if (assignedFilter === 'assigned') {
      filtered = filtered.filter((s) => !!assignmentMap[s.id]);
    } else if (assignedFilter === 'unassigned') {
      filtered = filtered.filter((s) => !assignmentMap[s.id]);
    }

    // Global search — tokenised substring match against the synthesised display
    // name so queries like "SUN Bl" find "SUNLU PETG Black". Also keeps the
    // prior per-column fallbacks so a free-text search still hits note /
    // slicer_filament_name / subtype fields that aren't always folded into
    // the template.
    if (search) {
      const q = search.toLowerCase();
      filtered = filtered.filter((s) => {
        const displayName = formatSpoolDisplayName(s, spoolDisplayTemplate);
        if (spoolDisplayNameMatches(displayName, search)) return true;
        return (
          s.brand?.toLowerCase().includes(q) ||
          s.material.toLowerCase().includes(q) ||
          s.color_name?.toLowerCase().includes(q) ||
          s.subtype?.toLowerCase().includes(q) ||
          s.note?.toLowerCase().includes(q) ||
          s.slicer_filament_name?.toLowerCase().includes(q)
        );
      });
    }

    return filtered;
  }, [spools, archiveFilter, usageFilter, materialFilter, brandFilter, colorFilter, categoryFilter, spoolFilter, storageLocationFilter, stockFilter, assignedFilter, assignmentMap, search, spoolDisplayTemplate, lowStockThreshold, storageLocations]);

  // Reset page on filter changes
  const resetPage = () => setPageIndex(0);

  // Storage-location filter writes to the URL (?location_id=<id>|__none__) so
  // the current view is shareable/deep-linkable, mirroring the ?spool= pattern.
  const setStorageLocationFilter = useCallback((value: string) => {
    setSearchParams((prev) => {
      prev.delete('location_id');
      if (value) {
        prev.set('location_id', value);
      }
      return prev;
    }, { replace: true });
    resetPage();
  }, [setSearchParams]);

  // Unique values for filter dropdowns
  const uniqueMaterials = [...new Set(spools?.map((s) => s.material) || [])].sort();
  const uniqueBrands = [...new Set(spools?.map((s) => s.brand).filter(Boolean) || [])].sort() as string[];
  // Colour options come from EXISTING (non-archived) spools only, by resolved
  // colour name. ``colorCatalogVersion`` is referenced so the list re-resolves
  // once the colour catalog loads.
  const uniqueColors = useMemo(() => {
    void colorCatalogVersion;
    const set = new Set<string>();
    for (const s of spools || []) {
      if (s.archived_at) continue;
      const name = resolveSpoolColorName(s.color_name, s.rgba);
      if (name) set.add(name);
    }
    return [...set].sort((a, b) => a.localeCompare(b));
  }, [spools, colorCatalogVersion]);
  const uniqueSpoolCatalogIds = [...new Set(spools?.map((s) => s.core_weight_catalog_id).filter((id): id is number => id != null) || [])].sort((a, b) => {
    const nameA = (catalogMap[a]?.name || '').toLowerCase();
    const nameB = (catalogMap[b]?.name || '').toLowerCase();
    return nameA.localeCompare(nameB);
  });
  const uniqueCategories = [...new Set(spools?.map((s) => s.category?.trim()).filter(Boolean) as string[] || [])].sort();
  const hasUncategorized = (spools ?? []).some((s) => !s.category);
  // Storage-location options now come from the catalog query (upstream #1505);
  // the chip only needs to know whether an "unfiled" bucket should be offered.
  const hasUnsetStorageLocation = (spools ?? []).some((s) => !s.location_id && !s.storage_location?.trim());

  // Check if any filters are non-default
  const hasActiveFilters = archiveFilter !== 'active' || usageFilter !== 'all' || !!materialFilter || !!brandFilter || !!colorFilter || !!categoryFilter || !!spoolFilter || !!storageLocationFilter || stockFilter !== 'all' || assignedFilter !== 'all' || !!search;

  const handleColumnConfigSave = (config: ColumnConfig[]) => {
    setColumnConfig(config);
    saveColumnConfig(config);
  };

  // Visible column IDs in order
  const visibleColumns = useMemo(
    () => columnConfig.filter((c) => c.visible).map((c) => c.id),
    [columnConfig]
  );

  // Rendered columns: settings still expose ``rgba`` and ``color_name`` as
  // two independent toggles, but the table merges them into a single
  // "Колір" column when both are on so the swatch + name read as one thing.
  // When only ``color_name`` is visible it still renders as plain text, just
  // with the "Колір" header via getHeaderLabel() below.
  const renderColumns = useMemo(() => toRenderColumns(visibleColumns), [visibleColumns]);

  const getHeaderLabel = (colId: string): string => {
    // Header override: when only ``color_name`` is visible, label it "Колір"
    // too so the table is consistent regardless of which toggle is on.
    if (colId === 'color_name' && !visibleColumns.includes('rgba')) {
      return t('inventory.columns.rgba');
    }
    return columnHeaders[colId]?.(t) ?? colId;
  };

  const handleSort = (colId: string) => {
    if (!columnSortValues[colId]) return; // Not sortable
    setSortState((prev) => {
      let next: SortState;
      if (prev?.column === colId) {
        // Toggle direction, or clear on third click
        next = prev.direction === 'asc' ? { column: colId, direction: 'desc' } : null;
      } else {
        next = { column: colId, direction: 'asc' };
      }
      saveSortState(next);
      return next;
    });
    resetPage();
  };

  // Sort filtered spools
  const sortedSpools = useMemo(() => {
    if (!sortState) return filteredSpools;
    // display_name sorts via the synthesised name (user-configurable template)
    // rather than any raw column — locale-aware so "Ясен" sorts cyrillically
    // and digits within a name (e.g. "100% PLA") compare numerically where
    // numeric flag is available.
    if (sortState.column === 'display_name') {
      const nameFor = (s: InventorySpool) =>
        formatSpoolDisplayName(s, spoolDisplayTemplate).toLowerCase();
      const sorted = [...filteredSpools].sort((a, b) => {
        const cmp = nameFor(a).localeCompare(nameFor(b), undefined, { numeric: true });
        return sortState.direction === 'asc' ? cmp : -cmp;
      });
      return sorted;
    }
    const extractor = columnSortValues[sortState.column];
    if (!extractor) return filteredSpools;
    const sorted = [...filteredSpools].sort((a, b) => {
      const va = extractor(a, assignmentMap);
      const vb = extractor(b, assignmentMap);
      if (va < vb) return sortState.direction === 'asc' ? -1 : 1;
      if (va > vb) return sortState.direction === 'asc' ? 1 : -1;
      return 0;
    });
    return sorted;
  }, [filteredSpools, sortState, assignmentMap, spoolDisplayTemplate]);

  // Group similar spools when toggle is active
  const displayItems = useMemo((): DisplayItem[] => {
    if (!groupSimilar) return sortedSpools.map((s) => ({ type: 'single' as const, spool: s }));

    const groups = new Map<string, InventorySpool[]>();

    for (const spool of sortedSpools) {
      // Only group unused & unassigned spools
      if (spool.weight_used > 0 || assignmentMap[spool.id]) {
        // Will be added as singles in the walk below
      } else {
        const key = spoolGroupKey(spool);
        const arr = groups.get(key);
        if (arr) arr.push(spool);
        else groups.set(key, [spool]);
      }
    }

    const items: DisplayItem[] = [];
    const processedKeys = new Set<string>();

    // Walk sortedSpools order so groups appear at the position of their first member
    for (const spool of sortedSpools) {
      if (spool.weight_used > 0 || assignmentMap[spool.id]) {
        items.push({ type: 'single', spool });
        continue;
      }
      const key = spoolGroupKey(spool);
      if (processedKeys.has(key)) continue;
      processedKeys.add(key);
      const members = groups.get(key)!;
      if (members.length === 1) {
        items.push({ type: 'single', spool: members[0] });
      } else {
        items.push({ type: 'group', key, spools: members, representative: members[0] });
      }
    }
    return items;
  }, [sortedSpools, groupSimilar, assignmentMap]);

  // Pagination (after sorting) - pageSize -1 means "All"
  const showAll = pageSize === -1;
  const totalDisplayItems = displayItems.length;
  const effectivePageSize = showAll ? totalDisplayItems || 1 : pageSize;
  const totalPages = Math.max(1, Math.ceil(totalDisplayItems / effectivePageSize));
  const safePageIndex = showAll ? 0 : Math.min(pageIndex, totalPages - 1);
  const pagedItems = showAll
    ? displayItems
    : displayItems.slice(safePageIndex * effectivePageSize, (safePageIndex + 1) * effectivePageSize);
  // Ids on screen right now — what the header checkbox ticks. Groups are
  // flattened, because a collapsed group still represents its members.
  const visibleSpoolIds = useMemo(
    () =>
      pagedItems.flatMap((item) =>
        item.type === 'group' ? item.spools.map((sp) => sp.id) : [item.spool.id],
      ),
    [pagedItems],
  );

  // Remember the filters. One effect rather than a write in each of the ~20
  // places a filter changes — including `clearAllFilters`, which is how a clear
  // becomes a cleared key without a second code path to keep in step.
  useEffect(() => {
    saveFilters({
      archiveFilter, usageFilter, materialFilter, brandFilter, colorFilter,
      categoryFilter, spoolFilter, stockFilter, assignedFilter, search, viewMode,
    });
  }, [archiveFilter, usageFilter, materialFilter, brandFilter, colorFilter,
      categoryFilter, spoolFilter, stockFilter, assignedFilter, search, viewMode]);

  // Drop the selection whenever the visible set changes underneath it. A
  // toolbar reading "12 selected" over a list that no longer contains those
  // rows is how a Delete lands on the wrong spools.
  //
  // ⚠️ Category, spool, storage location and stock were missing from this list,
  // so narrowing by those kept a selection made under the previous one.
  useEffect(() => {
    setSelectedIds(new Set());
  }, [archiveFilter, usageFilter, materialFilter, brandFilter, colorFilter, categoryFilter,
      spoolFilter, storageLocationFilter, stockFilter, assignedFilter, search, groupSimilar, spoolmanMode]);

  const selectedSpools = useMemo(
    () => filteredSpools.filter((sp) => selectedIds.has(sp.id)),
    [filteredSpools, selectedIds],
  );

  const toggleGroupSimilar = () => {
    const next = !groupSimilar;
    setGroupSimilar(next);
    setExpandedGroups(new Set());
    resetPage();
    try { localStorage.setItem('bamdude-inventory-group', String(next)); } catch { /* ignore */ }
  };

  const toggleGroupExpand = (key: string) => {
    setExpandedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const handlePageSizeChange = (size: number) => {
    setPageSize(size);
    setPageIndex(0);
    try { localStorage.setItem('bamdude-inventory-pageSize', String(size)); } catch { /* ignore */ }
  };

  /**
   * The table and the cards used to carry two copies of this control, already
   * drifted apart (one had button tooltips, the other didn't) — now one shared
   * component, the same one the archives page uses. `pageIndex` is 0-based here
   * and the bar counts from 1, like the number it puts on screen; the two
   * conversions live here so nothing downstream has to remember which is which.
   */
  const paginationBar = (variant: 'card' | 'bare') => (
    <PaginationBar
      page={safePageIndex + 1}
      totalPages={totalPages}
      perPage={pageSize}
      total={totalDisplayItems}
      items={totalDisplayItems !== 1 ? t('inventory.spools') : t('inventory.spool')}
      variant={variant}
      onPageChange={(p) => setPageIndex(p - 1)}
      onPerPageChange={handlePageSizeChange}
    />
  );

  const clearAllFilters = () => {
    setArchiveFilter('active');
    setUsageFilter('all');
    setMaterialFilter('');
    setBrandFilter('');
    setColorFilter('');
    setCategoryFilter('');
    setSpoolFilter('');
    setStorageLocationFilter('');
    setStockFilter('all');
    setAssignedFilter('all');
    setSearch('');
    resetPage();
  };

  return (
    <div className="p-4 md:p-6 space-y-4">
      {/* Header. ⚠️ Stacks below sm and the actions wrap: the buttons side by
          side are ~600px and nothing in that row can shrink, so on a phone the
          header pushed past the viewport and took the WHOLE PAGE with it —
          <main> is the scroll container, so everything inside it panned
          sideways. Identical at >=640px. Same pattern the Statistics, Settings
          and Archives headers use, and the filter bar further down this page. */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
            <h1 className="text-2xl font-bold text-white flex items-center gap-3"><Disc3 className="w-6 h-6 text-bambu-green" />{t('inventory.title')}</h1>
            <p className="text-sm text-bambu-gray">{t('inventory.noSpools').split('.')[0] ? '' : ''}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {/* Bulk edit — internal inventory only (not Spoolman mode). Opens a
              modal that lets the user pick which of the filtered spools to edit
              and which fields to change. */}
          {!spoolmanMode && hasPermission('inventory:update') && (
            <Button
              variant="secondary"
              disabled={filteredSpools.length === 0}
              onClick={() => setShowBulkEdit(true)}
              title={t('inventory.bulkEdit.title')}
            >
              <Layers className="w-4 h-4" />
              {t('inventory.bulkEdit.button')}
            </Button>
          )}
          <Button
            variant="secondary"
            disabled={filteredSpools.length === 0}
            // Pre-select every visible spool so the user lands in "all
            // checked", then refines downward in the modal. Per-card icon
            // pre-selects only that spool — both flows share the same picker.
            onClick={() => setLabelPickerSpoolIds(filteredSpools.map((s) => s.id))}
            title={
              filteredSpools.length === 0
                ? t('inventory.labels.noSpoolsTitle')
                : t('inventory.labels.bulkTitle', { count: filteredSpools.length })
            }
          >
            <Printer className="w-4 h-4" />
            {t('inventory.labels.printLabels')}
          </Button>
          {/* CSV import/export (#1576). Operates on BamDude's local inventory.
              In Spoolman mode the buttons stay visible (feature parity) but are
              disabled with a hint pointing at Spoolman's own CSV export, since
              Spoolman owns the data store in that mode. */}
          <Button variant="secondary" disabled={spoolmanMode} onClick={() => setCsvImportOpen(true)} title={spoolmanCsvHint}>
            <Upload className="w-4 h-4" />
            {t('inventory.csv.importButton', 'Import CSV')}
          </Button>
          <Button variant="secondary" disabled={spoolmanMode || exportingCsv} onClick={handleExportCsv} title={spoolmanCsvHint}>
            {exportingCsv ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
            {t('inventory.csv.exportButton', 'Export CSV')}
          </Button>
          <Button variant="secondary" onClick={() => setLocationsModalOpen(true)}>
            <MapPin className="w-4 h-4" />
            {t('locations.manage')}
          </Button>
          <Button onClick={() => setFormModal({ spool: null, mode: 'create' })}>
            <Plus className="w-4 h-4" />
            {t('inventory.addSpool')}
          </Button>
        </div>
      </div>

      {/* Stats Bar */}
      {stats && !isLoading && (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
          {/* Total Inventory */}
          <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg p-4">
            <div className="flex items-center gap-2 mb-1">
              <Package className="w-4 h-4 text-bambu-green" />
              <span className="text-xs text-bambu-gray font-medium uppercase tracking-wide">{t('inventory.totalInventory')}</span>
            </div>
            <div className="text-xl font-bold text-white">{formatWeight(stats.totalWeight, true)}</div>
            <div className="text-xs text-bambu-gray mt-1">{stats.totalSpools} {stats.totalSpools !== 1 ? t('inventory.spools') : t('inventory.spool')}</div>
          </div>

          {/* By Material */}
          <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg p-4">
            <div className="flex items-center gap-2 mb-1">
              <Layers className="w-4 h-4 text-green-600 dark:text-green-400" />
              <span className="text-xs text-bambu-gray font-medium uppercase tracking-wide">{t('inventory.byMaterial')}</span>
            </div>
            <div className="flex flex-wrap gap-1.5 mt-1">
              {topMaterials.map(([mat, data]) => (
                <span
                  key={mat}
                  className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${MATERIAL_COLORS[mat] || 'bg-bambu-dark-tertiary text-bambu-gray'}`}
                >
                  {mat} <span className="opacity-70">{formatWeight(data.weight, true)}</span>
                </span>
              ))}
            </div>
          </div>

          {/* Total Consumed */}
          <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg p-4">
            <div className="flex items-center justify-between gap-2 mb-1">
              <div className="flex items-center gap-2">
                <TrendingDown className="w-4 h-4 text-blue-600 dark:text-blue-400" />
                <span className="text-xs text-bambu-gray font-medium uppercase tracking-wide">{t('inventory.totalConsumed')}</span>
              </div>
              {stats.totalConsumed > 0 && resetableSpoolIds.length > 0 && (
                <button
                  onClick={() => setConfirmAction({ type: 'reset-all-consumed-counters' })}
                  className="p-1 text-bambu-gray hover:text-red-600 dark:hover:text-red-400 rounded transition-colors"
                  title={t('inventory.resetAllConsumedCountersTooltip')}
                  aria-label={t('inventory.resetAllConsumedCounters')}
                >
                  <Eraser className="w-3.5 h-3.5" />
                </button>
              )}
            </div>
            <div className="text-xl font-bold text-white">{formatWeight(stats.totalConsumed, true)}</div>
            <div className="text-xs text-bambu-gray mt-1">{t('inventory.sinceTracking')}</div>
          </div>

          {/* In Printer */}
          <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg p-4">
            <div className="flex items-center gap-2 mb-1">
              <Printer className="w-4 h-4 text-purple-600 dark:text-purple-400" />
              <span className="text-xs text-bambu-gray font-medium uppercase tracking-wide">{t('inventory.inPrinter')}</span>
            </div>
            <div className="text-xl font-bold text-white">{inPrinterCount}</div>
            <div className="text-xs text-bambu-gray mt-1">{t('inventory.loadedInAms')}</div>
          </div>

          {/* Low Stock */}
          <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg p-4">
            <div className="flex items-center gap-2 mb-1">
              <AlertTriangle className="w-4 h-4 text-yellow-600 dark:text-yellow-400" />
              <span className="text-xs text-bambu-gray font-medium uppercase tracking-wide">{t('inventory.lowStock')}</span>
            </div>
            <div className={`text-xl font-bold ${stats.lowStock > 0 ? 'text-yellow-700 dark:text-yellow-400' : 'text-white'}`}>{stats.lowStock}</div>
            <div className="text-xs text-bambu-gray mt-1 flex items-center gap-2">
              {showThresholdInput ? (
                <form
                  onSubmit={e => {
                    e.preventDefault();
                    const val = parseFloat(thresholdInput);
                    if (!isNaN(val) && val >= 0.1 && val <= 99.9) {
                      updateThresholdMutation.mutate(val);
                    } else {
                      showToast(t('inventory.lowStockThresholdError'), 'error');
                    }
                  }}
                  className="flex items-center gap-2"
                >
                  <span className="text-xs text-bambu-gray">{'<'}</span>
                  <input
                    type="text"
                    inputMode="decimal"
                    pattern="^\d{0,2}(\.\d?)?$"
                    maxLength={4}
                    value={thresholdInput}
                    onChange={e => {
                      // Only allow up to 2 digits before decimal and 1 after
                      const val = e.target.value.replace(/[^\d.]/g, '');
                      if (/^\d{0,2}(\.\d?)?$/.test(val)) {
                        setThresholdInput(val);
                      }
                    }}
                    className="px-1.5 py-1 rounded border border-bambu-dark-tertiary text-xs text-white bg-bambu-dark-secondary focus:outline-none focus:border-bambu-green w-14 text-center"
                    onWheel={e => e.currentTarget.blur()}
                    disabled={updateThresholdMutation.isPending}
                  />

                  <span className="text-xs text-bambu-gray">%</span>
                  <Button type="submit" size="sm" disabled={updateThresholdMutation.isPending}>{t('common.save')}</Button>
                  <Button type="button" size="sm" variant="ghost" onClick={() => setShowThresholdInput(false)} disabled={updateThresholdMutation.isPending}>{t('common.cancel')}</Button>
                </form>
              ) : (
                <>
                  <span className="text-bambu-gray">{'< '}{lowStockThreshold}%</span>
                  <button
                    className="p-1.5 text-bambu-gray hover:text-white rounded transition-colors"
                    title={t('common.edit')}
                    onClick={() => {
                      setThresholdInput(lowStockThreshold.toString());
                      setShowThresholdInput(true);
                    }}
                  >
                    <Edit2 className="w-4 h-4" />
                  </button>
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Toolbar: Search + View toggle */}
      <div className="flex flex-col sm:flex-row gap-3 items-start sm:items-center justify-between">
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-bambu-gray/50" />
          <input
            type="text"
            value={search}
            onChange={(e) => { setSearch(e.target.value); resetPage(); }}
            placeholder={t('inventory.search')}
            className="w-full pl-10 pr-8 py-2 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg text-white text-sm placeholder:text-bambu-gray/50 focus:outline-none focus:border-bambu-green"
          />
          {search && (
            <button
              onClick={() => { setSearch(''); resetPage(); }}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-bambu-gray hover:text-white"
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>

        <div className="flex items-center gap-2">
          {/* Columns button (table view only) */}
          {viewMode === 'table' && (
            <button
              onClick={() => setShowColumnModal(true)}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-bambu-gray border border-bambu-dark-tertiary rounded-lg hover:bg-bambu-dark-tertiary transition-colors"
              title={t('inventory.configureColumns')}
            >
              <Columns className="w-4 h-4" />
              <span className="hidden sm:inline">{t('inventory.columnsLabel')}</span>
            </button>
          )}
          {/* Group similar toggle */}
          <button
            onClick={toggleGroupSimilar}
            className={`flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium border rounded-lg transition-colors ${
              groupSimilar
                ? 'bg-bambu-green/20 text-bambu-green border-bambu-green/30'
                : 'text-bambu-gray border-bambu-dark-tertiary hover:bg-bambu-dark-tertiary'
            }`}
            title={t('inventory.groupSimilar')}
          >
            <Group className="w-4 h-4" />
            <span className="hidden sm:inline">{t('inventory.groupSimilar')}</span>
          </button>
          {/* Table / Cards toggle */}
          <div className="flex bg-bambu-dark-primary border border-bambu-dark-tertiary rounded-lg overflow-hidden">
            <button
              onClick={() => setViewMode('table')}
              className={`flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium transition-colors ${
                viewMode === 'table'
                  ? 'bg-bambu-green text-white'
                  : 'text-bambu-gray hover:bg-bambu-dark-tertiary'
              }`}
            >
              <TableProperties className="w-4 h-4" />
              <span className="hidden sm:inline">{t('inventory.table')}</span>
            </button>
            <button
              onClick={() => setViewMode('cards')}
              className={`flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium transition-colors ${
                viewMode === 'cards'
                  ? 'bg-bambu-green text-white'
                  : 'text-bambu-gray hover:bg-bambu-dark-tertiary'
              }`}
            >
              <LayoutGrid className="w-4 h-4" />
              <span className="hidden sm:inline">{t('inventory.cards')}</span>
            </button>
            {/* Forecast tab — gated on perm + non-Spoolman mode (upstream #1184) */}
            {!spoolmanMode && (
              <button
                onClick={() => canViewForecast && setViewMode('forecast')}
                disabled={!canViewForecast}
                title={canViewForecast ? undefined : t('forecast.noReadAccess')}
                className={`flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
                  viewMode === 'forecast'
                    ? 'bg-bambu-green text-white'
                    : 'text-bambu-gray hover:bg-bambu-dark-tertiary'
                }`}
              >
                {canViewForecast ? <TrendingUp className="w-4 h-4" /> : <Lock className="w-4 h-4" />}
                <span className="hidden sm:inline">{t('forecast.title')}</span>
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Filter chips row */}
      <div className="flex flex-wrap items-center gap-2">
        {/* Active / Archived chips */}
        <div className="flex items-center rounded-lg border border-bambu-dark-tertiary overflow-hidden">
          <button
            onClick={() => { setArchiveFilter('active'); resetPage(); }}
            className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium transition-colors ${
              archiveFilter === 'active'
                ? 'bg-bambu-green/20 text-bambu-green'
                : 'text-bambu-gray hover:bg-bambu-dark-tertiary'
            }`}
          >
            <Package className="w-3.5 h-3.5" />
            {t('inventory.active')}
          </button>
          <button
            onClick={() => { setArchiveFilter('archived'); resetPage(); }}
            className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium transition-colors ${
              archiveFilter === 'archived'
                ? 'bg-bambu-green/20 text-bambu-green'
                : 'text-bambu-gray hover:bg-bambu-dark-tertiary'
            }`}
          >
            <Archive className="w-3.5 h-3.5" />
            {t('inventory.archived')}
          </button>
        </div>

        <div className="w-px h-5 bg-bambu-dark-tertiary" />

        {/* All / Used / New chips */}
        <div className="flex items-center rounded-lg border border-bambu-dark-tertiary overflow-hidden">
          <button
            onClick={() => { setUsageFilter('all'); resetPage(); }}
            className={`px-3 py-1.5 text-xs font-medium transition-colors ${
              usageFilter === 'all'
                ? 'bg-bambu-green/20 text-bambu-green'
                : 'text-bambu-gray hover:bg-bambu-dark-tertiary'
            }`}
          >
            {t('inventory.all')}
          </button>
          <button
            onClick={() => { setUsageFilter('used'); resetPage(); }}
            className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium transition-colors ${
              usageFilter === 'used'
                ? 'bg-bambu-green/20 text-bambu-green'
                : 'text-bambu-gray hover:bg-bambu-dark-tertiary'
            }`}
          >
            <Clock className="w-3.5 h-3.5" />
            {t('inventory.used')}
          </button>
          <button
            onClick={() => { setUsageFilter('new'); resetPage(); }}
            className={`px-3 py-1.5 text-xs font-medium transition-colors ${
              usageFilter === 'new'
                ? 'bg-bambu-green/20 text-bambu-green'
                : 'text-bambu-gray hover:bg-bambu-dark-tertiary'
            }`}
          >
            {t('inventory.new')}
          </button>
          <button
            onClick={() => { setUsageFilter('lowstock'); resetPage(); }}
            className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium transition-colors ${
              usageFilter === 'lowstock'
                ? 'bg-yellow-100 dark:bg-yellow-500/20 text-yellow-700 dark:text-yellow-400'
                : 'text-bambu-gray hover:bg-bambu-dark-tertiary'
            }`}
          >
            <AlertTriangle className="w-3.5 h-3.5" />
            {t('inventory.lowStock')}
          </button>
        </div>

        {/* Stock filter chips */}
        <div className="flex items-center rounded-lg border border-bambu-dark-tertiary overflow-hidden">
          <button
            onClick={() => { setStockFilter('all'); resetPage(); }}
            className={`px-3 py-1.5 text-xs font-medium transition-colors ${
              stockFilter === 'all'
                ? 'bg-bambu-green/20 text-bambu-green'
                : 'text-bambu-gray hover:bg-bambu-dark-tertiary'
            }`}
          >
            {t('inventory.all')}
          </button>
          <button
            onClick={() => { setStockFilter('stock'); resetPage(); }}
            className={`px-3 py-1.5 text-xs font-medium transition-colors ${
              stockFilter === 'stock'
                ? 'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400'
                : 'text-bambu-gray hover:bg-bambu-dark-tertiary'
            }`}
          >
            {t('inventory.stock')}
          </button>
          <button
            onClick={() => { setStockFilter('configured'); resetPage(); }}
            className={`px-3 py-1.5 text-xs font-medium transition-colors ${
              stockFilter === 'configured'
                ? 'bg-bambu-green/20 text-bambu-green'
                : 'text-bambu-gray hover:bg-bambu-dark-tertiary'
            }`}
          >
            {t('inventory.configured')}
          </button>
        </div>

        {/* Loaded in a printer, or on the shelf. Three states rather than a
            checkbox: "not assigned" is a question people ask as often as
            "assigned" — it is what is left to take — and a two-state tick
            cannot express it without meaning "all" in one of its positions. */}
        <div className="flex items-center rounded-lg border border-bambu-dark-tertiary overflow-hidden">
          <button
            onClick={() => { setAssignedFilter('all'); resetPage(); }}
            className={`px-3 py-1.5 text-xs font-medium transition-colors ${
              assignedFilter === 'all'
                ? 'bg-bambu-green/20 text-bambu-green'
                : 'text-bambu-gray hover:bg-bambu-dark-tertiary'
            }`}
          >
            {t('inventory.all')}
          </button>
          <button
            onClick={() => { setAssignedFilter('assigned'); resetPage(); }}
            title={t('inventory.inPrinterHint')}
            className={`px-3 py-1.5 text-xs font-medium transition-colors ${
              assignedFilter === 'assigned'
                ? 'bg-bambu-green/20 text-bambu-green'
                : 'text-bambu-gray hover:bg-bambu-dark-tertiary'
            }`}
          >
            {t('inventory.inPrinter')}
          </button>
          <button
            onClick={() => { setAssignedFilter('unassigned'); resetPage(); }}
            title={t('inventory.onShelfHint')}
            className={`px-3 py-1.5 text-xs font-medium transition-colors ${
              assignedFilter === 'unassigned'
                ? 'bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400'
                : 'text-bambu-gray hover:bg-bambu-dark-tertiary'
            }`}
          >
            {t('inventory.onShelf')}
          </button>
        </div>

        <div className="w-px h-5 bg-bambu-dark-tertiary" />

        {/* Material dropdown chip */}
        <select
          value={materialFilter}
          onChange={(e) => { setMaterialFilter(e.target.value); resetPage(); }}
          className={`px-3 py-1.5 rounded-lg border text-xs font-medium transition-colors cursor-pointer focus:outline-none ${
            materialFilter
              ? 'bg-bambu-green/20 text-bambu-green border-bambu-green/30'
              : 'bg-transparent text-bambu-gray border-bambu-dark-tertiary hover:bg-bambu-dark-tertiary'
          }`}
        >
          <option value="">{t('inventory.material')}</option>
          {withCurrentValue(uniqueMaterials, materialFilter).map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>

        {/* Brand dropdown chip */}
        <select
          value={brandFilter}
          onChange={(e) => { setBrandFilter(e.target.value); resetPage(); }}
          className={`px-3 py-1.5 rounded-lg border text-xs font-medium transition-colors cursor-pointer focus:outline-none ${
            brandFilter
              ? 'bg-bambu-green/20 text-bambu-green border-bambu-green/30'
              : 'bg-transparent text-bambu-gray border-bambu-dark-tertiary hover:bg-bambu-dark-tertiary'
          }`}
        >
          <option value="">{t('inventory.brand')}</option>
          {withCurrentValue(uniqueBrands, brandFilter).map((b) => (
            <option key={b} value={b}>{b}</option>
          ))}
        </select>

        {/* Colour dropdown chip — options from existing (non-archived) spools,
            by resolved colour name. Only render once at least one active spool
            has a resolvable colour (or a colour is already selected). */}
        {(uniqueColors.length > 0 || colorFilter) && (
          <select
            value={colorFilter}
            onChange={(e) => { setColorFilter(e.target.value); resetPage(); }}
            className={`px-3 py-1.5 rounded-lg border text-xs font-medium transition-colors cursor-pointer focus:outline-none ${
              colorFilter
                ? 'bg-bambu-green/20 text-bambu-green border-bambu-green/30'
                : 'bg-transparent text-bambu-gray border-bambu-dark-tertiary hover:bg-bambu-dark-tertiary'
            }`}
          >
            <option value="">{t('inventory.color')}</option>
            {withCurrentValue(uniqueColors, colorFilter).map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        )}

        {/* Category dropdown chip (#729) — only render once at least one
            spool carries a category, otherwise it's noise. */}
        {(uniqueCategories.length > 0 || categoryFilter) && (
          <select
            value={categoryFilter}
            onChange={(e) => { setCategoryFilter(e.target.value); resetPage(); }}
            className={`px-3 py-1.5 rounded-lg border text-xs font-medium transition-colors cursor-pointer focus:outline-none ${
              categoryFilter
                ? 'bg-bambu-green/20 text-bambu-green border-bambu-green/30'
                : 'bg-transparent text-bambu-gray border-bambu-dark-tertiary hover:bg-bambu-dark-tertiary'
            }`}
          >
            <option value="">{t('inventory.category')}</option>
            {/* `__none__` is a real value with its own option below, so it must
                not be folded in as a literal alongside it. */}
            {withCurrentValue(uniqueCategories, categoryFilter === '__none__' ? '' : categoryFilter).map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
            {(hasUncategorized || categoryFilter === '__none__') && (
              <option value="__none__">{t('inventory.categoryNone')}</option>
            )}
          </select>
        )}

        {/* Storage location dropdown chip (#1400) — only render once at
            least one spool carries a storage location, otherwise it's
            noise (matches the category chip pattern). */}
        {(storageLocations.length > 0 || storageLocationFilter) && (
          <select
            value={storageLocationFilter}
            onChange={(e) => { setStorageLocationFilter(e.target.value); }}
            className={`px-3 py-1.5 rounded-lg border text-xs font-medium transition-colors cursor-pointer focus:outline-none ${
              storageLocationFilter
                ? 'bg-bambu-green/20 text-bambu-green border-bambu-green/30'
                : 'bg-transparent text-bambu-gray border-bambu-dark-tertiary hover:bg-bambu-dark-tertiary'
            }`}
          >
            <option value="">{t('inventory.storageLocation')}</option>
            {storageLocations.map((loc) => (
              <option key={loc.id} value={String(loc.id)}>{loc.name}</option>
            ))}
            {hasUnsetStorageLocation && (
              <option value="__none__">{t('inventory.storageLocationNone')}</option>
            )}
          </select>
        )}

        {/* Spool name dropdown chip */}
        {/* `|| spoolFilter` — a restored filter whose catalog entry is gone
            must still have a chip to clear it from. */}
        {(uniqueSpoolCatalogIds.length > 0 || spoolFilter) && (
          <select
            value={spoolFilter}
            onChange={(e) => { setSpoolFilter(e.target.value); resetPage(); }}
            className={`px-3 py-1.5 rounded-lg border text-xs font-medium transition-colors cursor-pointer focus:outline-none ${
              spoolFilter
                ? 'bg-bambu-green/20 text-bambu-green border-bambu-green/30'
                : 'bg-transparent text-bambu-gray border-bambu-dark-tertiary hover:bg-bambu-dark-tertiary'
            }`}
          >
            <option value="">{t('inventory.spoolName')}</option>
            {withCurrentId(uniqueSpoolCatalogIds, spoolFilter).map((id) => (
              <option key={id} value={id}>{catalogMap[id]?.name || `#${id}`}</option>
            ))}
          </select>
        )}

        {/* Clear filters */}
        {hasActiveFilters && (
          <>
            <div className="w-px h-5 bg-bambu-dark-tertiary" />
            <button
              onClick={clearAllFilters}
              className="flex items-center gap-1 text-xs text-bambu-gray hover:text-bambu-green transition-colors"
            >
              <X className="w-3.5 h-3.5" />
              {t('inventory.clearFilters')}
            </button>
          </>
        )}

        {/* Results count */}
        <span className="ml-auto text-xs text-bambu-gray">
          {sortedSpools.length} {sortedSpools.length !== 1 ? t('inventory.spools') : t('inventory.spool')}
          {groupSimilar && totalDisplayItems < sortedSpools.length && ` (${totalDisplayItems} ${t('inventory.groupedRows')})`}
        </span>
      </div>

      {/* Content */}
      {isLoading ? (
        <div className="flex justify-center py-16">
          <Loader2 className="w-8 h-8 text-bambu-green animate-spin" />
        </div>
      ) : viewMode === 'forecast' ? (
        /* Forecast view (upstream #1184) */
        <ForecastPanel spools={spools || []} />
      ) : viewMode === 'cards' ? (
        /* Cards view */
        pagedItems.length > 0 ? (
          <>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
              {pagedItems.map((item) => {
                if (item.type === 'group') {
                  const { key, spools: groupSpools, representative: rep } = item;
                  // Total remaining filament across the group (#1368) — the
                  // headline number for the collapsed card, vs one member's.
                  const groupRemaining = groupSpools.reduce(
                    (sum, s) => sum + Math.max(0, s.label_weight - s.weight_used),
                    0,
                  );
                  const colorStyle = rep.rgba ? `#${rep.rgba.substring(0, 6)}` : '#808080';
                  const isExpanded = expandedGroups.has(key);
                  return (
                    <div key={`group-${key}`} className="col-span-full">
                      {/* Group header card */}
                      <div
                        className="bg-bambu-dark-secondary rounded-lg overflow-hidden border border-bambu-green/30 hover:border-bambu-green transition-colors cursor-pointer"
                        onClick={() => toggleGroupExpand(key)}
                      >
                        <div className="h-10 flex items-center px-4 gap-3" style={{ backgroundColor: colorStyle }}>
                          <span className="bg-white/90 text-gray-800 px-3 py-0.5 rounded-full text-sm font-medium">
                            {resolveSpoolColorName(rep.color_name, rep.rgba) || '-'}
                          </span>
                        </div>
                        <div className="px-4 py-3 flex items-center justify-between">
                          <div className="flex items-center gap-3">
                            <ChevronDown className={`w-4 h-4 text-bambu-gray transition-transform ${isExpanded ? '' : '-rotate-90'}`} />
                            <div>
                              <h3 className="font-semibold text-white">{rep.material}{rep.subtype ? ` ${rep.subtype}` : ''}</h3>
                              <p className="text-sm text-bambu-gray">{rep.brand || '-'}</p>
                            </div>
                          </div>
                          <div className="flex items-center gap-2">
                            <span className="text-sm text-bambu-gray" title={t('inventory.remaining')}>
                              {formatWeight(groupRemaining)}
                            </span>
                            <span className="text-xs font-medium bg-bambu-green/20 text-bambu-green px-2 py-0.5 rounded-full">
                              {t('inventory.groupedSpools', { count: groupSpools.length })}
                            </span>
                          </div>
                        </div>
                      </div>
                      {/* Expanded individual spools */}
                      {isExpanded && (
                        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4 mt-2 ml-4">
                          {groupSpools.map((spool) => {
                            const remaining = Math.max(0, spool.label_weight - spool.weight_used);
                            const pct = spool.label_weight > 0 ? (remaining / spool.label_weight) * 100 : 0;
                            const spoolColor = spool.rgba ? `#${spool.rgba.substring(0, 6)}` : '#808080';
                            return (
                              <SpoolCard
                                key={spool.id}
                                spool={spool}
                                remaining={remaining}
                                pct={pct}
                                colorStyle={spoolColor}
                                onClick={() => setFormModal({ spool, mode: 'edit' })}
                                onPrintLabel={() => setLabelPickerSpoolIds([spool.id])}
                                onCopy={() => setFormModal({ spool, mode: 'copy' })}
                                t={t}
                              />
                            );
                          })}
                        </div>
                      )}
                    </div>
                  );
                }
                const spool = item.spool;
                const remaining = Math.max(0, spool.label_weight - spool.weight_used);
                const pct = spool.label_weight > 0 ? (remaining / spool.label_weight) * 100 : 0;
                const colorStyle = spool.rgba ? `#${spool.rgba.substring(0, 6)}` : '#808080';
                return (
                  <SpoolCard
                    key={spool.id}
                    spool={spool}
                    remaining={remaining}
                    pct={pct}
                    colorStyle={colorStyle}
                    onClick={() => setFormModal({ spool, mode: 'edit' })}
                    onPrintLabel={() => setLabelPickerSpoolIds([spool.id])}
                    onCopy={() => setFormModal({ spool, mode: 'copy' })}
                    t={t}
                  />
                );
              })}
            </div>
            {/* Pagination for cards — bare, since a grid has no card of its
                own for a footer to sit in. */}
            {paginationBar('bare')}
          </>
        ) : (
          <EmptyFilterState
            hasFilters={hasActiveFilters}
            onAddSpool={() => setFormModal({ spool: null, mode: 'create' })}
            t={t}
          />
        )
      ) : (
        /* Table view */
        pagedItems.length > 0 ? (
          <>
          {/* Bulk-action toolbar (#1795). Appears only once something is
              selected, so it costs nothing until it is wanted. Sticky, because
              a selection made at the bottom of a long list must stay actionable
              without scrolling back up. */}
          {selectedIds.size > 0 && (
            <div className="sticky top-0 z-20 mb-2 flex flex-wrap items-center gap-2 rounded-lg border border-bambu-green/30 bg-bambu-dark-secondary/95 backdrop-blur-sm px-3 py-2">
              <span className="text-sm font-medium text-white">
                {t('inventory.bulk.selectedCount', { count: selectedIds.size })}
              </span>
              {/* Preserves the pre-#1795 behaviour — "act on everything the
                  filter shows" — as an explicit, visible action rather than an
                  implicit default. */}
              {selectedIds.size < filteredSpools.length && (
                <button
                  type="button"
                  onClick={() => setManySelected(filteredSpools.map((sp) => sp.id), true)}
                  className="px-2 py-1 text-xs rounded bg-bambu-dark text-bambu-gray hover:text-white transition-colors"
                >
                  {t('inventory.bulk.selectAllFiltered', { count: filteredSpools.length })}
                </button>
              )}
              <div className="flex-1" />
              <button
                type="button"
                disabled={bulkPending}
                onClick={() => setShowBulkEdit(true)}
                className="px-3 py-1.5 text-xs font-medium rounded bg-bambu-green/20 text-bambu-green hover:bg-bambu-green/30 transition-colors disabled:opacity-50"
              >
                {t('common.edit')}
              </button>
              <button
                type="button"
                disabled={bulkPending}
                onClick={() => setLabelPickerSpoolIds([...selectedIds])}
                className="px-3 py-1.5 text-xs font-medium rounded bg-bambu-dark text-bambu-gray hover:text-white transition-colors disabled:opacity-50"
              >
                {t('inventory.labels.printOne')}
              </button>
              <button
                type="button"
                disabled={bulkPending}
                onClick={() => bulkResetConsumedCounterMutation.mutate([...selectedIds])}
                className="px-3 py-1.5 text-xs font-medium rounded bg-bambu-dark text-bambu-gray hover:text-white transition-colors disabled:opacity-50"
              >
                {t('inventory.resetConsumedCounter')}
              </button>
              {/* Archive vs Restore follows the tab you are on, mirroring the
                  per-row action — on the Archived tab the only sensible bulk
                  move is putting them back. */}
              {archiveFilter === 'archived' ? (
                <button
                  type="button"
                  disabled={bulkPending}
                  onClick={() => setConfirmBulk('restore')}
                  className="px-3 py-1.5 text-xs font-medium rounded bg-bambu-dark text-bambu-gray hover:text-white transition-colors disabled:opacity-50"
                >
                  {t('inventory.restore')}
                </button>
              ) : (
                <button
                  type="button"
                  disabled={bulkPending}
                  onClick={() => setConfirmBulk('archive')}
                  className="px-3 py-1.5 text-xs font-medium rounded bg-amber-500/15 text-amber-400 hover:bg-amber-500/25 transition-colors disabled:opacity-50"
                >
                  {t('inventory.archive')}
                </button>
              )}
              <button
                type="button"
                disabled={bulkPending}
                onClick={() => setConfirmBulk('delete')}
                className="px-3 py-1.5 text-xs font-medium rounded bg-red-500/15 text-red-400 hover:bg-red-500/25 transition-colors disabled:opacity-50"
              >
                {t('common.delete')}
              </button>
              <button
                type="button"
                onClick={clearSelection}
                className="px-2 py-1 text-xs rounded text-bambu-gray hover:text-white transition-colors"
              >
                {t('inventory.bulk.clear')}
              </button>
            </div>
          )}
          <div className="bg-bambu-dark-secondary rounded-lg overflow-hidden border border-bambu-dark-tertiary">
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-bambu-dark-tertiary bg-bambu-dark-tertiary/30">
                    {/* Select-all-visible. Note this ticks the rows ON SCREEN;
                        "select every row matching the filter" is a separate,
                        explicit toolbar action so a paged-away row is never
                        deleted by a checkbox the user could not see. */}
                    <th className="py-3 pl-4 pr-0 w-8">
                      <input
                        type="checkbox"
                        checked={visibleSpoolIds.length > 0 && visibleSpoolIds.every((id) => selectedIds.has(id))}
                        ref={(el) => {
                          if (el) {
                            const picked = visibleSpoolIds.filter((id) => selectedIds.has(id)).length;
                            el.indeterminate = picked > 0 && picked < visibleSpoolIds.length;
                          }
                        }}
                        onChange={(e) => setManySelected(visibleSpoolIds, e.target.checked)}
                        aria-label={t('inventory.bulk.selectAllVisible')}
                        className="w-4 h-4 accent-bambu-green cursor-pointer"
                      />
                    </th>
                    {renderColumns.map((colId) => {
                      const sortable = !!columnSortValues[colId];
                      const isActive = sortState?.column === colId;
                      return (
                        <th
                          key={colId}
                          className={`text-left py-3 px-4 text-xs font-medium uppercase tracking-wide select-none ${colId === 'remaining' ? 'min-w-[150px]' : ''} ${
                            sortable ? 'cursor-pointer hover:text-bambu-green transition-colors' : ''
                          } ${isActive ? 'text-bambu-green' : 'text-bambu-gray'}`}
                          onClick={sortable ? () => handleSort(colId) : undefined}
                        >
                          <span className="inline-flex items-center gap-1">
                            {getHeaderLabel(colId)}
                            {sortable && (
                              isActive
                                ? sortState.direction === 'asc'
                                  ? <ArrowUp className="w-3 h-3" />
                                  : <ArrowDown className="w-3 h-3" />
                                : <ArrowUpDown className="w-3 h-3 opacity-30" />
                            )}
                          </span>
                        </th>
                      );
                    })}
                    <th className="text-right py-3 px-4 text-xs font-medium text-bambu-gray uppercase tracking-wide">{t('common.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {pagedItems.map((item) => {
                    if (item.type === 'group') {
                      const { key, spools: groupSpools } = item;
                      const isExpanded = expandedGroups.has(key);
                      // Header row shows group totals (#1368): an aggregate
                      // spool plus remaining / pct summed across all members.
                      const headerSpool = aggregateGroupSpool(groupSpools);
                      const remaining = Math.max(0, headerSpool.label_weight - headerSpool.weight_used);
                      const pct = headerSpool.label_weight > 0 ? (remaining / headerSpool.label_weight) * 100 : 0;
                      return (
                        <SpoolTableGroup
                          key={`group-${key}`}
                          spools={groupSpools}
                          selectedIds={selectedIds}
                          onToggleSelected={toggleSelected}
                          onToggleGroupSelected={setManySelected}
                          headerSpool={headerSpool}
                          remaining={remaining}
                          pct={pct}
                          isExpanded={isExpanded}
                          onToggle={() => toggleGroupExpand(key)}
                          onEdit={(s) => setFormModal({ spool: s, mode: 'edit' })}
                          onCopy={(s) => setFormModal({ spool: s, mode: 'copy' })}
                          onArchive={(id) => setConfirmAction({ type: 'archive', spoolId: id })}
                          onDelete={(id) => setConfirmAction({ type: 'delete', spoolId: id })}
                          onPrintLabel={(id) => setLabelPickerSpoolIds([id])}
                          onResetConsumedCounter={(id) => setConfirmAction({ type: 'reset-consumed-counter', spoolId: id })}
                          visibleColumns={renderColumns}
                          assignmentMap={assignmentMap}
                          catalogMap={catalogMap}
                          currencySymbol={currencySymbol}
                          dateFormat={dateFormat}
                          t={t}
                          spoolDisplayTemplate={spoolDisplayTemplate}
                        />
                      );
                    }
                    const spool = item.spool;
                    const remaining = Math.max(0, spool.label_weight - spool.weight_used);
                    const pct = spool.label_weight > 0 ? (remaining / spool.label_weight) * 100 : 0;
                    return (
                      <SpoolTableRow
                        key={spool.id}
                        spool={spool}
                        remaining={remaining}
                        pct={pct}
                        isSelected={selectedIds.has(spool.id)}
                        onToggleSelected={() => toggleSelected(spool.id)}
                        onEdit={() => setFormModal({ spool, mode: 'edit' })}
                        onCopy={() => setFormModal({ spool, mode: 'copy' })}
                        onRestore={() => restoreMutation.mutate(spool.id)}
                        onArchive={() => setConfirmAction({ type: 'archive', spoolId: spool.id })}
                        onDelete={() => setConfirmAction({ type: 'delete', spoolId: spool.id })}
                        onPrintLabel={() => setLabelPickerSpoolIds([spool.id])}
                        onResetConsumedCounter={() => setConfirmAction({ type: 'reset-consumed-counter', spoolId: spool.id })}
                        visibleColumns={renderColumns}
                        assignmentMap={assignmentMap}
                        catalogMap={catalogMap}
                        currencySymbol={currencySymbol}
                        dateFormat={dateFormat}
                        t={t}
                        spoolDisplayTemplate={spoolDisplayTemplate}
                      />
                    );
                  })}
                </tbody>
              </table>
            </div>

            {paginationBar('card')}
          </div>
          </>
        ) : (
          <EmptyFilterState
            hasFilters={hasActiveFilters}
            onAddSpool={() => setFormModal({ spool: null, mode: 'create' })}
            t={t}
          />
        )
      )}

      {/* Spool Form Modal */}
      {formModal !== null && (
        <SpoolFormModal
          isOpen={true}
          onClose={() => setFormModal(null)}
          spool={formModal.spool}
          mode={formModal.mode}
          currencySymbol={currencySymbol}
          spoolmanMode={spoolmanMode}
          spoolsQueryKey={spoolsQueryKey}
        />
      )}

      {/* CSV import modal (#1576) */}
      {csvImportOpen && (
        <SpoolCsvImportModal
          onClose={() => setCsvImportOpen(false)}
          onImported={(created) => {
            setCsvImportOpen(false);
            queryClient.invalidateQueries({ queryKey: spoolsQueryKey });
            showToast(t('inventory.csv.importSuccess', '{{count}} spools imported', { count: created }), 'success');
          }}
        />
      )}

      {/* Confirm Modal (delete / archive / reset-consumed-counter / reset-all-consumed-counters) */}
      {/* Bulk confirmation (#1795). Deleting a selection is not undoable and
          the count can be large, so it never fires straight off the toolbar. */}
      {confirmBulk && (
        <ConfirmModal
          title={t(`inventory.bulk.action.${confirmBulk}`)}
          message={t(`inventory.bulk.confirm.${confirmBulk}`, { count: selectedIds.size })}
          confirmText={t(`inventory.bulk.action.${confirmBulk}`)}
          variant={confirmBulk === 'delete' ? 'danger' : 'warning'}
          onConfirm={() => {
            bulkActionMutation.mutate({ kind: confirmBulk, ids: [...selectedIds] });
            setConfirmBulk(null);
          }}
          onCancel={() => setConfirmBulk(null)}
        />
      )}

      {confirmAction && (
        <ConfirmModal
          title={
            confirmAction.type === 'delete' ? t('common.delete') :
            confirmAction.type === 'archive' ? t('inventory.archive') :
            confirmAction.type === 'reset-consumed-counter' ? t('inventory.resetConsumedCounter') :
            t('inventory.resetAllConsumedCounters')
          }
          message={
            confirmAction.type === 'delete' ? t('inventory.deleteConfirm') :
            confirmAction.type === 'archive' ? t('inventory.archiveConfirm') :
            confirmAction.type === 'reset-consumed-counter' ? t('inventory.resetConsumedCounterConfirm') :
            t('inventory.resetAllConsumedCountersConfirm', { count: resetableSpoolIds.length })
          }
          confirmText={
            confirmAction.type === 'delete' ? t('common.delete') :
            confirmAction.type === 'archive' ? t('inventory.archive') :
            t('inventory.resetConsumedCounter')
          }
          variant={confirmAction.type === 'archive' ? 'warning' : 'danger'}
          onConfirm={() => {
            if (confirmAction.type === 'delete') {
              deleteMutation.mutate(confirmAction.spoolId);
            } else if (confirmAction.type === 'archive') {
              archiveMutation.mutate(confirmAction.spoolId);
            } else if (confirmAction.type === 'reset-consumed-counter') {
              resetConsumedCounterMutation.mutate(confirmAction.spoolId);
            } else {
              bulkResetConsumedCounterMutation.mutate(resetableSpoolIds);
            }
            setConfirmAction(null);
          }}
          onCancel={() => setConfirmAction(null)}
        />
      )}

      {/* Column Config Modal */}
      <ColumnConfigModal
        isOpen={showColumnModal}
        onClose={() => setShowColumnModal(false)}
        columns={columnConfig}
        defaultColumns={DEFAULT_COLUMNS}
        onSave={handleColumnConfigSave}
      />

      {/* Label printing (B.1 #809). The picker routes to either the internal
          `/inventory/labels` or the `/spoolman/labels` endpoint based on the
          current inventory backend — the unified UI exposes both. */}
      <LabelTemplatePickerModal
        isOpen={labelPickerSpoolIds !== null}
        onClose={() => setLabelPickerSpoolIds(null)}
        availableSpools={filteredSpools}
        initialSelectedIds={labelPickerSpoolIds ?? []}
        spoolmanMode={spoolmanMode}
        spoolDisplayTemplate={spoolDisplayTemplate}
      />
      {showBulkEdit && (
        <BulkEditSpoolsModal
          isOpen
          spools={selectedSpools}
          allSpools={spools || []}
          catalogEntries={catalogEntries || []}
          spoolDisplayTemplate={spoolDisplayTemplate}
          onClose={() => setShowBulkEdit(false)}
          onSaved={() => refreshSpoolQueries()}
        />
      )}

      <LocationsModal
        open={locationsModalOpen}
        onClose={() => setLocationsModalOpen(false)}
        onPickLocation={(id) => setStorageLocationFilter(String(id))}
      />
    </div>
  );
}

/* Spool card for cards view */
function SpoolCard({
  spool, remaining, pct, colorStyle, onClick, onPrintLabel, onCopy, t,
}: {
  spool: InventorySpool;
  remaining: number;
  pct: number;
  colorStyle: string;
  onClick: () => void;
  onPrintLabel?: () => void;
  onCopy?: () => void;
  t: (key: string, opts?: Record<string, unknown>) => string;
}) {
  return (
    <div
      className={`bg-bambu-dark-secondary rounded-lg overflow-hidden border border-bambu-dark-tertiary hover:border-bambu-green transition-colors cursor-pointer ${spool.archived_at ? 'opacity-50' : ''}`}
      onClick={onClick}
    >
      <div className="h-14 flex items-center justify-center" style={{ backgroundColor: colorStyle }}>
        <span className="bg-white/90 text-gray-800 px-3 py-0.5 rounded-full text-sm font-medium">
          {resolveSpoolColorName(spool.color_name, spool.rgba) || '-'}
        </span>
      </div>
      <div className="p-4 space-y-3">
        <div className="flex items-start justify-between gap-2">
          <div>
            <h3 className="font-semibold text-white">
              {spool.material}{spool.subtype ? ` ${spool.subtype}` : ''}
            </h3>
            <p className="text-sm text-bambu-gray">{spool.brand || '-'}</p>
          </div>
          <div className="flex items-center gap-1">
            {onCopy && (
              <button
                onClick={(e) => { e.stopPropagation(); onCopy(); }}
                className="p-1 text-bambu-gray hover:text-bambu-green rounded transition-colors"
                title={t('inventory.copySpool')}
                aria-label={t('inventory.copySpool')}
              >
                <Copy className="w-4 h-4" />
              </button>
            )}
            {onPrintLabel && (
              <button
                onClick={(e) => { e.stopPropagation(); onPrintLabel(); }}
                className="p-1 text-bambu-gray hover:text-white rounded transition-colors"
                title={t('inventory.labels.printOne')}
                aria-label={t('inventory.labels.printOne')}
              >
                <Printer className="w-4 h-4" />
              </button>
            )}
            <span className="text-xs font-mono text-bambu-gray bg-bambu-dark-tertiary px-2 py-1 rounded">
              #{spool.id}
            </span>
          </div>
        </div>
        <div>
          <div className="flex justify-between text-xs text-bambu-gray mb-1">
            <span>{t('inventory.remaining')}</span>
            <span>{Math.round(pct)}%</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="flex-1 h-2 bg-bambu-dark-tertiary rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full ${pct > 50 ? 'bg-bambu-green' : pct > 20 ? 'bg-yellow-500' : 'bg-red-500'}`}
                style={{ width: `${Math.min(pct, 100)}%` }}
              />
            </div>
            <span className="text-xs text-bambu-gray min-w-[40px] text-right">
              {Math.round(remaining)}g
            </span>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-2 text-xs">
          <div>
            <span className="text-bambu-gray/60">{t('inventory.labelWeight')}: </span>
            <span className="text-bambu-gray">{formatWeight(spool.label_weight)}</span>
          </div>
          <div>
            <span className="text-bambu-gray/60">{t('inventory.weightUsed')}: </span>
            <span className="text-bambu-gray">
              {spool.weight_used > 0 ? formatWeight(spool.weight_used) : '-'}
            </span>
          </div>
        </div>
        {spool.k_profiles && spool.k_profiles.length > 0 && (
          <div className="pt-2 border-t border-bambu-dark-tertiary space-y-1">
            <div className="text-[10px] uppercase tracking-wide text-bambu-gray/60">
              {t('inventory.kprofile.title')}
            </div>
            <div className="flex flex-wrap gap-1">
              {spool.k_profiles.map((kp) => {
                const flow = (kp.nozzle_type || '').toUpperCase().startsWith('HIGH') ? 'HF' : 'S';
                const k = Math.trunc((kp.k_value ?? 0) * 1000) / 1000;
                return (
                  <span
                    key={kp.id}
                    className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[11px] bg-bambu-green/15 text-bambu-green"
                    title={kp.name || undefined}
                  >
                    {kp.auto_linked && (
                      <Sparkles className="w-3 h-3 opacity-80" aria-label={t('inventory.kprofile.autoTooltip')} />
                    )}
                    <span className="max-w-[90px] truncate">{kp.name || `#${kp.id}`}</span>
                    <span className="opacity-70">· {kp.nozzle_diameter} {flow} · K{k.toFixed(3)}</span>
                  </span>
                );
              })}
            </div>
          </div>
        )}
        {spool.note && (
          <div
            className="text-xs text-bambu-gray/60 pt-2 border-t border-bambu-dark-tertiary truncate"
            title={spool.note}
          >
            {spool.note}
          </div>
        )}
      </div>
    </div>
  );
}

/* Single spool row for table view */
function SpoolTableRow({
  spool, remaining, pct, onEdit, onCopy, onRestore, onArchive, onDelete, onPrintLabel, onResetConsumedCounter,
  visibleColumns, assignmentMap, catalogMap, currencySymbol, dateFormat, t, onSyncWeight,
  spoolDisplayTemplate, isSelected, onToggleSelected,
}: {
  spool: InventorySpool;
  /** Bulk selection (#1795). Omitted in contexts with no selection column. */
  isSelected?: boolean;
  onToggleSelected?: () => void;
  remaining: number;
  pct: number;
  onEdit: () => void;
  onCopy?: () => void;
  onRestore: () => void;
  onArchive: () => void;
  onDelete: () => void;
  onPrintLabel?: () => void;
  onResetConsumedCounter?: () => void;
  visibleColumns: string[];
  assignmentMap: Record<number, LocationDisplay>;
  catalogMap: Record<number, SpoolCatalogEntry>;
  currencySymbol: string;
  dateFormat: DateFormat;
  t: TFn;
  onSyncWeight?: (spool: InventorySpool) => void;
  spoolDisplayTemplate: string;
}) {
  return (
    <tr
      className={`border-b border-bambu-dark-tertiary/50 hover:bg-bambu-dark-tertiary/30 transition-colors cursor-pointer ${
        spool.archived_at ? 'opacity-50' : ''
      }`}
      onClick={onEdit}
    >
      {/* Selection checkbox (#1795). stopPropagation because the whole row
          opens the editor on click — ticking a box must not do that too. */}
      {onToggleSelected && (
        <td className="py-3 pl-4 pr-0 w-8" onClick={(e) => e.stopPropagation()}>
          <input
            type="checkbox"
            checked={!!isSelected}
            onChange={() => onToggleSelected()}
            aria-label={t('inventory.bulk.selectRow')}
            className="w-4 h-4 accent-bambu-green cursor-pointer"
          />
        </td>
      )}
      {visibleColumns.map((colId) => (
        <td key={colId} className="py-3 px-4">
          {columnCells[colId]?.({ spool, remaining, pct, assignmentMap, catalogMap, currencySymbol, dateFormat, t, onSyncWeight, spoolDisplayTemplate })}
        </td>
      ))}
      <td className="py-3 px-4">
        <div className="flex items-center justify-end gap-1" onClick={(e) => e.stopPropagation()}>
          <button onClick={onEdit} className="p-1.5 text-bambu-gray hover:text-white rounded transition-colors" title={t('common.edit')}>
            <Edit2 className="w-4 h-4" />
          </button>
          {onCopy && (
            <button onClick={onCopy} className="p-1.5 text-bambu-gray hover:text-bambu-green rounded transition-colors" title={t('inventory.copySpool')}>
              <Copy className="w-4 h-4" />
            </button>
          )}
          {onPrintLabel && (
            <button onClick={onPrintLabel} className="p-1.5 text-bambu-gray hover:text-white rounded transition-colors" title={t('inventory.labels.printOne')}>
              <Printer className="w-4 h-4" />
            </button>
          )}
          {/* Eraser also renders on archived spools (#1390 follow-up):
              archived consumed weight now counts in "Total Consumed", so
              the user needs a way to zero an archived spool's tracking
              counter individually without having to un-archive it first. */}
          {onResetConsumedCounter && spool.weight_used > (spool.weight_used_baseline ?? 0) && (
            <button onClick={onResetConsumedCounter} className="p-1.5 text-bambu-gray hover:text-orange-600 dark:hover:text-orange-400 rounded transition-colors" title={t('inventory.resetConsumedCounterTooltip')}>
              <Eraser className="w-4 h-4" />
            </button>
          )}
          {spool.archived_at ? (
            <button onClick={onRestore} className="p-1.5 text-bambu-gray hover:text-bambu-green rounded transition-colors" title={t('inventory.restore')}>
              <RotateCcw className="w-4 h-4" />
            </button>
          ) : (
            <button onClick={onArchive} className="p-1.5 text-bambu-gray hover:text-yellow-600 dark:hover:text-yellow-400 rounded transition-colors" title={t('inventory.archive')}>
              <Archive className="w-4 h-4" />
            </button>
          )}
          <button onClick={onDelete} className="p-1.5 text-bambu-gray hover:text-red-600 dark:hover:text-red-400 rounded transition-colors" title={t('common.delete')}>
            <Trash2 className="w-4 h-4" />
          </button>
        </div>
      </td>
    </tr>
  );
}

/* Grouped spool rows for table view */
function SpoolTableGroup({
  spools, headerSpool, remaining, pct, isExpanded, onToggle,
  onEdit, onCopy, onArchive, onDelete, onPrintLabel, onResetConsumedCounter,
  visibleColumns, assignmentMap, catalogMap, currencySymbol, dateFormat, t, onSyncWeight,
  spoolDisplayTemplate, selectedIds, onToggleSelected, onToggleGroupSelected,
}: {
  spools: InventorySpool[];
  /** Bulk selection (#1795). The group header ticks every member at once. */
  selectedIds?: Set<number>;
  onToggleSelected?: (id: number) => void;
  onToggleGroupSelected?: (ids: number[], selected: boolean) => void;
  // Aggregate of all members (summed quantities, shared identity) — rendered
  // in the collapsed header row so it shows group totals (#1368).
  headerSpool: InventorySpool;
  remaining: number;
  pct: number;
  isExpanded: boolean;
  onToggle: () => void;
  onEdit: (spool: InventorySpool) => void;
  onCopy?: (spool: InventorySpool) => void;
  onArchive: (id: number) => void;
  onDelete: (id: number) => void;
  onPrintLabel?: (spoolId: number) => void;
  onResetConsumedCounter?: (id: number) => void;
  visibleColumns: string[];
  assignmentMap: Record<number, LocationDisplay>;
  catalogMap: Record<number, SpoolCatalogEntry>;
  currencySymbol: string;
  dateFormat: DateFormat;
  t: TFn;
  onSyncWeight?: (spool: InventorySpool) => void;
  spoolDisplayTemplate: string;
}) {
  return (
    <>
      {/* Group header row */}
      <tr
        className="border-b border-bambu-dark-tertiary/50 hover:bg-bambu-dark-tertiary/30 transition-colors cursor-pointer bg-bambu-green/5"
        onClick={onToggle}
      >
        {/* Group checkbox — ticks or clears every member in one go. Indeterminate
            when only some are selected, so a partial group is visible at a glance. */}
        {onToggleGroupSelected && (
          <td className="py-3 pl-4 pr-0 w-8" onClick={(e) => e.stopPropagation()}>
            <input
              type="checkbox"
              checked={spools.every((sp) => selectedIds?.has(sp.id))}
              ref={(el) => {
                if (el) {
                  const picked = spools.filter((sp) => selectedIds?.has(sp.id)).length;
                  el.indeterminate = picked > 0 && picked < spools.length;
                }
              }}
              onChange={(e) => onToggleGroupSelected(spools.map((sp) => sp.id), e.target.checked)}
              aria-label={t('inventory.bulk.selectGroup')}
              className="w-4 h-4 accent-bambu-green cursor-pointer"
            />
          </td>
        )}
        {visibleColumns.map((colId, idx) => (
          <td key={colId} className="py-3 px-4">
            {idx === 0 ? (
              <div className="flex items-center gap-2">
                <ChevronDown className={`w-4 h-4 text-bambu-gray transition-transform ${isExpanded ? '' : '-rotate-90'}`} />
                {columnCells[colId]?.({ spool: headerSpool, remaining, pct, assignmentMap, catalogMap, currencySymbol, dateFormat, t, onSyncWeight, spoolDisplayTemplate })}
              </div>
            ) : colId === 'id' ? (
              <span className="text-xs font-medium bg-bambu-green/20 text-bambu-green px-2 py-0.5 rounded-full">
                {t('inventory.groupedSpools', { count: spools.length })}
              </span>
            ) : (
              columnCells[colId]?.({ spool: headerSpool, remaining, pct, assignmentMap, catalogMap, currencySymbol, dateFormat, t, onSyncWeight, spoolDisplayTemplate })
            )}
          </td>
        ))}
        <td className="py-3 px-4">
          <span className="text-xs text-bambu-gray">
            {spools.map((s) => `#${s.id}`).join(', ')}
          </span>
        </td>
      </tr>
      {/* Expanded individual rows */}
      {isExpanded && spools.map((spool) => {
        const r = Math.max(0, spool.label_weight - spool.weight_used);
        const p = spool.label_weight > 0 ? (r / spool.label_weight) * 100 : 0;
        return (
          <SpoolTableRow
            key={spool.id}
            spool={spool}
            remaining={r}
            pct={p}
            isSelected={selectedIds?.has(spool.id)}
            onToggleSelected={onToggleSelected ? () => onToggleSelected(spool.id) : undefined}
            onEdit={() => onEdit(spool)}
            onCopy={onCopy ? () => onCopy(spool) : undefined}
            onRestore={() => {}}
            onArchive={() => onArchive(spool.id)}
            onDelete={() => onDelete(spool.id)}
            onPrintLabel={onPrintLabel ? () => onPrintLabel(spool.id) : undefined}
            onResetConsumedCounter={onResetConsumedCounter ? () => onResetConsumedCounter(spool.id) : undefined}
            visibleColumns={visibleColumns}
            assignmentMap={assignmentMap}
            catalogMap={catalogMap}
            currencySymbol={currencySymbol}
            dateFormat={dateFormat}
            t={t}
            onSyncWeight={onSyncWeight}
            spoolDisplayTemplate={spoolDisplayTemplate}
          />
        );
      })}
    </>
  );
}

/* Empty state */
function EmptyFilterState({
  hasFilters,
  onAddSpool,
  t,
}: {
  hasFilters: boolean;
  onAddSpool: () => void;
  t: (key: string) => string;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-16 px-4">
      <div className="relative mb-6">
        <div className="absolute inset-0 -m-4 bg-bambu-green/5 rounded-full blur-2xl" />
        <div className="relative flex items-center justify-center w-24 h-24 rounded-2xl bg-gradient-to-br from-bambu-dark-secondary to-bambu-dark-tertiary border border-bambu-dark-tertiary shadow-lg">
          <div className="absolute -top-1 -right-1 w-3 h-3 rounded-full bg-bambu-green/30" />
          <div className="absolute -bottom-2 -left-2 w-2 h-2 rounded-full bg-bambu-green/20" />
          {hasFilters ? (
            <Search className="w-10 h-10 text-bambu-gray/40" strokeWidth={1.5} />
          ) : (
            <div className="relative">
              <div className="w-14 h-14 rounded-full border-4 border-bambu-gray/20 flex items-center justify-center">
                <div className="w-6 h-6 rounded-full bg-bambu-gray/10 border-2 border-bambu-gray/20" />
              </div>
              <div className="absolute -bottom-1 -right-1 w-6 h-6 rounded-full bg-bambu-green flex items-center justify-center shadow-md">
                <span className="text-white text-lg font-bold leading-none">+</span>
              </div>
            </div>
          )}
        </div>
      </div>
      <h3 className="text-lg font-semibold text-white mb-2 text-center">
        {hasFilters ? t('inventory.noSpoolsMatch') : t('inventory.noSpools').split('.')[0]}
      </h3>
      <p className="text-sm text-bambu-gray text-center max-w-sm mb-6">
        {hasFilters
          ? t('inventory.noSpoolsMatchDesc')
          : t('inventory.noSpools')
        }
      </p>
      {!hasFilters && (
        <Button onClick={onAddSpool}>
          <Package className="w-4 h-4" />
          {t('inventory.addSpool')}
        </Button>
      )}
    </div>
  );
}
