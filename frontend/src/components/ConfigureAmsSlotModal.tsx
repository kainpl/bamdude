import { useState, useMemo, useEffect, useCallback, useRef } from 'react';
import { useQuery, useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { X, Loader2, Settings2, ChevronDown, CheckCircle2, RotateCcw } from 'lucide-react';
import { api } from '../api/client';
import type { KProfile } from '../api/client';
import { isMatchingCalibration } from './spool-form/utils';
import { Button } from './Button';

interface SlotInfo {
  amsId: number;
  trayId: number;
  trayCount: number;
  trayType?: string;
  trayColor?: string;
  traySubBrands?: string;
  trayInfoIdx?: string;
  extruderId?: number;
  caliIdx?: number | null;
  savedPresetId?: string;
}

// Get proper AMS label (handles HT AMS with ID 128+)
function getAmsLabel(amsId: number, trayCount: number): string {
  // External spool
  if (amsId === 255) return 'External';

  let normalizedId: number;
  let isHt = false;

  if (amsId >= 128 && amsId <= 135) {
    // HT AMS range: 128-135 → A-H
    normalizedId = amsId - 128;
    isHt = true;
  } else if (amsId >= 0 && amsId <= 3) {
    // Regular AMS range: 0-3 → A-D
    normalizedId = amsId;
    // Check tray count as secondary indicator
    isHt = trayCount === 1;
  } else {
    // Unknown range - fallback to A
    normalizedId = 0;
  }

  // Cap to valid letter range (A-H)
  normalizedId = Math.max(0, Math.min(normalizedId, 7));
  const letter = String.fromCharCode(65 + normalizedId);

  return isHt ? `HT-${letter}` : `AMS-${letter}`;
}

interface ConfigureAmsSlotModalProps {
  isOpen: boolean;
  onClose: () => void;
  printerId: number;
  slotInfo: SlotInfo;
  nozzleDiameter?: string;
  printerModel?: string;
  onSuccess?: () => void;
  fullScreen?: boolean;
}


// Common color name to hex mapping
const COLOR_NAME_MAP: Record<string, string> = {
  // Basic colors
  'white': 'FFFFFF',
  'black': '000000',
  'red': 'FF0000',
  'green': '00FF00',
  'blue': '0000FF',
  'yellow': 'FFFF00',
  'cyan': '00FFFF',
  'magenta': 'FF00FF',
  'orange': 'FFA500',
  'purple': '800080',
  'pink': 'FFC0CB',
  'brown': '8B4513',
  'gray': '808080',
  'grey': '808080',
  // Filament-specific colors
  'jade white': 'FFFEF2',
  'ivory': 'FFFFF0',
  'beige': 'F5F5DC',
  'cream': 'FFFDD0',
  'silver': 'C0C0C0',
  'gold': 'FFD700',
  'bronze': 'CD7F32',
  'copper': 'B87333',
  'navy': '000080',
  'teal': '008080',
  'olive': '808000',
  'maroon': '800000',
  'coral': 'FF7F50',
  'salmon': 'FA8072',
  'lime': '32CD32',
  'mint': '98FF98',
  'forest green': '228B22',
  'sky blue': '87CEEB',
  'royal blue': '4169E1',
  'turquoise': '40E0D0',
  'lavender': 'E6E6FA',
  'violet': 'EE82EE',
  'plum': 'DDA0DD',
  'tan': 'D2B48C',
  'chocolate': 'D2691E',
  'charcoal': '36454F',
  'slate': '708090',
  'transparent': '000000', // Will need special handling
  'natural': 'F5F5DC',
  'wood': 'DEB887',
};

// Quick-select color presets (common filament colors)
// Basic colors shown by default
const QUICK_COLORS_BASIC = [
  { name: 'White', hex: 'FFFFFF' },
  { name: 'Black', hex: '000000' },
  { name: 'Red', hex: 'FF0000' },
  { name: 'Blue', hex: '0000FF' },
  { name: 'Green', hex: '00AA00' },
  { name: 'Yellow', hex: 'FFFF00' },
  { name: 'Orange', hex: 'FFA500' },
  { name: 'Gray', hex: '808080' },
];

// Extended colors shown when expanded
const QUICK_COLORS_EXTENDED = [
  { name: 'Cyan', hex: '00FFFF' },
  { name: 'Magenta', hex: 'FF00FF' },
  { name: 'Purple', hex: '800080' },
  { name: 'Pink', hex: 'FFC0CB' },
  { name: 'Brown', hex: '8B4513' },
  { name: 'Beige', hex: 'F5F5DC' },
  { name: 'Navy', hex: '000080' },
  { name: 'Teal', hex: '008080' },
  { name: 'Lime', hex: '32CD32' },
  { name: 'Gold', hex: 'FFD700' },
  { name: 'Silver', hex: 'C0C0C0' },
  { name: 'Maroon', hex: '800000' },
  { name: 'Olive', hex: '808000' },
  { name: 'Coral', hex: 'FF7F50' },
  { name: 'Salmon', hex: 'FA8072' },
  { name: 'Turquoise', hex: '40E0D0' },
  { name: 'Violet', hex: 'EE82EE' },
  { name: 'Indigo', hex: '4B0082' },
  { name: 'Chocolate', hex: 'D2691E' },
  { name: 'Tan', hex: 'D2B48C' },
  { name: 'Slate', hex: '708090' },
  { name: 'Charcoal', hex: '36454F' },
  { name: 'Ivory', hex: 'FFFFF0' },
  { name: 'Cream', hex: 'FFFDD0' },
];

// Try to convert color name to hex
function colorNameToHex(name: string): string | null {
  const normalized = name.toLowerCase().trim();
  return COLOR_NAME_MAP[normalized] || null;
}

export function ConfigureAmsSlotModal({
  isOpen,
  onClose,
  printerId,
  slotInfo,
  nozzleDiameter = '0.4',
  onSuccess,
  fullScreen,
}: ConfigureAmsSlotModalProps) {
  const { t } = useTranslation();
  const [selectedPresetId, setSelectedPresetId] = useState<string>('');

  // Poke the cloud preset mirror once per open (server-side debounced) so a
  // preset created in BS/Orca moments ago is already resolvable (spec A §3).
  useEffect(() => {
    if (isOpen) api.triggerFilamentPresetSync().catch(() => undefined);
  }, [isOpen]);
  const [selectedKProfile, setSelectedKProfile] = useState<KProfile | null>(null);
  const [colorHex, setColorHex] = useState<string>(''); // Just the 6-char hex, no alpha
  const [colorInput, setColorInput] = useState<string>(''); // User's text input (name or hex)
  const [searchQuery, setSearchQuery] = useState('');
  const [showSuccess, setShowSuccess] = useState(false);
  const [showExtendedColors, setShowExtendedColors] = useState(false);
  const scrolledToRef = useRef<string>('');

  // Fetch cloud settings (gracefully handle 401 when logged out)
  // Orca Cloud filament profiles, same shape as Bambu Cloud's. Each query
  // is independent — the picker degrades gracefully if Orca Cloud isn't
  // connected (no entries surface, no error banner because we don't want
  // to nag users who deliberately only use Bambu Cloud).
  // Fetch local presets
  // Fetch built-in filament names (static fallback)
  // Fetch K profiles
  // Non-archived spools — the colour palette source for the picked family.
  const { data: spoolsData } = useQuery({
    queryKey: ['spools'],
    queryFn: () => api.getSpools(false),
    enabled: isOpen,
    staleTime: 30_000,
  });

  // The family catalog IS the source list now (spec A §5.5): official
  // families + the user's custom ones, one identity, no per-source tiers.
  const { data: familiesData, isLoading: familiesLoading } = useQuery({
    queryKey: ['filamentFamilies', searchQuery],
    queryFn: () => api.getFilamentFamilies(searchQuery),
    enabled: isOpen,
    staleTime: 30_000,
  });

  const { data: kprofilesData, isLoading: kprofilesLoading } = useQuery({
    queryKey: ['kprofiles', printerId, nozzleDiameter],
    queryFn: () => api.getKProfiles(printerId, nozzleDiameter),
    enabled: isOpen && !!printerId,
  });

  // Fetch color catalog
  // Canonical Bambu printer-model registry ("Bambu Lab X1 Carbon" → "X1C").
  // Drives the @-suffix long-form matcher and the body-scan in
  // extractPresetModel, plus the reverse short-code → long-name lookup that
  // builds this slot's full printer-preset name for the local-preset
  // ``compatible_printers`` filter (#1623). Long staleTime: the registry only
  // changes across backend releases.
  // Configure slot mutation — ONE identity path (spec A §5.2): the family id
  // goes out as tray_info_idx and the backend's slot-assignment builder
  // resolves the versioned setting_id, temps and type from the catalog.
  const configureMutation = useMutation({
    mutationFn: async () => {
      if (!selectedPresetId) throw new Error('No filament family selected');
      const fam = (familiesData || []).find(f => f.filament_id === selectedPresetId);
      const caliIdx = selectedKProfile?.slot_id ?? -1;
      const color = colorHex || slotInfo.trayColor?.slice(0, 6) || 'FFFFFF';
      const kValue = selectedKProfile?.k_value ? parseFloat(selectedKProfile.k_value) : 0;

      const result = await api.configureAmsSlot(printerId, slotInfo.amsId, slotInfo.trayId, {
        tray_info_idx: selectedPresetId,
        tray_type: fam?.filament_type || '',
        tray_sub_brands: fam?.alias || '',
        tray_color: color + 'FF', // Add alpha
        // 0 = "let the backend builder take them from the catalog preset
        // for this printer" (0 falls through to the catalog inside
        // build_slot_assignment's temp_overrides handling).
        nozzle_temp_min: 0,
        nozzle_temp_max: 0,
        cali_idx: caliIdx,
        nozzle_diameter: nozzleDiameter,
        setting_id: '',
        kprofile_filament_id: selectedKProfile?.filament_id,
        kprofile_setting_id: selectedKProfile?.setting_id || undefined,
        k_value: kValue,
      });
      return result;
    },
    onSuccess: () => {
      setShowSuccess(true);
      onSuccess?.();
      // Close after showing success briefly
      setTimeout(() => {
        setShowSuccess(false);
        onClose();
      }, 1500);
    },
  });

  // Reset slot mutation
  const resetMutation = useMutation({
    mutationFn: async () => {
      return api.resetAmsSlot(printerId, slotInfo.amsId, slotInfo.trayId);
    },
    onSuccess: () => {
      setShowSuccess(true);
      onSuccess?.();
      setTimeout(() => {
        setShowSuccess(false);
        onClose();
      }, 1500);
    },
  });

  // Unified item shape kept for the existing list JSX; ids are FAMILY ids.
  type PresetItem = { id: string; name: string; source: 'orca_cloud' | 'cloud' | 'local' | 'builtin'; isUser: boolean };

  const filteredPresets = useMemo(() => {
    const query = searchQuery.toLowerCase();
    const items: PresetItem[] = [];
    for (const fam of familiesData || []) {
      const hay = `${fam.alias} ${fam.vendor || ''} ${fam.filament_type || ''} ${fam.filament_id}`.toLowerCase();
      if (query && !hay.includes(query)) continue;
      const source: PresetItem['source'] =
        fam.origin === 'system'
          ? 'builtin'
          : fam.origin === 'cloud_orca'
            ? 'orca_cloud'
            : fam.origin === 'local' || fam.origin === 'authored'
              ? 'local'
              : 'cloud';
      const label = [fam.alias, fam.vendor && fam.vendor !== 'Generic' ? null : null].filter(Boolean).join('');
      items.push({ id: fam.filament_id, name: label || fam.alias, source, isUser: fam.origin !== 'system' });
    }
    // Custom families first (that's what the user came to pick), then official.
    return items.sort((a, b) => {
      if (a.isUser !== b.isUser) return a.isUser ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
  }, [familiesData, searchQuery]);

  // Selected family info for colour matching + the K-profile name fallback.
  const selectedPresetInfo = useMemo(() => {
    if (!selectedPresetId) return null;
    const fam = (familiesData || []).find(f => f.filament_id === selectedPresetId);
    if (!fam) return { fullName: selectedPresetId, material: '', brand: '', variant: '' };
    return {
      fullName: fam.alias,
      material: fam.filament_type || '',
      brand: fam.vendor && fam.vendor !== 'Generic' ? fam.vendor : '',
      variant: '',
    };
  }, [selectedPresetId, familiesData]);

  // For backwards compatibility with the label
  const selectedMaterial = selectedPresetInfo?.fullName || '';

  // Colours offered for the picked family = the DISTINCT colours of the
  // user's own non-archived spools linked to that family. The global colour
  // catalog listed every colour ever sold — far too many, mostly irrelevant.
  const catalogColors = useMemo(() => {
    if (!selectedPresetId || !spoolsData) return [];
    const seen = new Set<string>();
    const out: { id: string; hex_color: string; color_name: string }[] = [];
    for (const spool of spoolsData) {
      const fid = spool.filament_family_id;
      if (fid !== selectedPresetId) continue;
      const hex = (spool.rgba || '').slice(0, 6).toUpperCase();
      if (!hex || seen.has(hex)) continue;
      seen.add(hex);
      out.push({ id: hex, hex_color: `#${hex}`, color_name: spool.color_name || '' });
    }
    return out;
  }, [selectedPresetId, spoolsData]);

  // The selected id IS the family id — the same identity the printer's
  // K-profile table uses. No conversion, no cloud detail round-trip.
  const targetFilamentId = selectedPresetId || null;

  const matchingKProfiles = useMemo(() => {
    if (!kprofilesData?.profiles) return [];

    // Match through the shared helper rather than a bare `filament_id ===`
    // (#1688/#1689). It keeps our id-is-authoritative precedence — the id
    // comes from `resolveTargetFilamentId`, which collapses a custom preset
    // onto its inherited base, so an id mismatch is a real mismatch — but it
    // also gives us the name fallback for presets that resolve NO id at all.
    // That last part matters: an Orca Cloud preset's id is a UUID that
    // normalises to null, and the old id-only filter short-circuited to an
    // empty list, so the K-profile dropdown was permanently blank there.
    const filtered = selectedPresetInfo
      ? kprofilesData.profiles.filter(p =>
          isMatchingCalibration(
            { name: p.name, filament_id: p.filament_id },
            {
              material: selectedPresetInfo.material,
              brand: selectedPresetInfo.brand,
              subtype: selectedPresetInfo.variant,
            },
            targetFilamentId,
          ),
        )
      : [];

    // Deduplicate profiles with same name and k_value (multi-nozzle printers
    // have duplicates). Prefer the profile matching the slot's extruder
    // (ext-R uses extruder 0, ext-L uses extruder 1).
    const seen = new Map<string, KProfile>();
    for (const profile of filtered) {
      const key = `${profile.name}|${profile.k_value}`;
      const existing = seen.get(key);
      if (!existing) {
        seen.set(key, profile);
      } else if (slotInfo.extruderId !== undefined && profile.extruder_id === slotInfo.extruderId && existing.extruder_id !== slotInfo.extruderId) {
        seen.set(key, profile);
      }
    }

    const result = Array.from(seen.values());

    // Safety net (#1689): always surface the slot's CURRENTLY-BOUND K-profile
    // (by cali_idx / slot_id) even when it matched neither by id nor by name.
    // A spool assigned under "Generic PLA" can have a custom profile actively
    // bound on the printer whose filament_id differs from the preset. Without
    // this the dropdown is empty, and because Save only requires a preset,
    // `caliIdx` falls to -1 and the configure mutation sends
    // `extrusion_cali_sel: -1` — silently CLEARING the printer's binding while
    // the printer card's hover-card still shows the profile. Gated on
    // activeIdx > 0 so caliIdx 0/null can't leak an unrelated profile in.
    const activeIdx = slotInfo.caliIdx;
    if (activeIdx != null && activeIdx > 0 && !result.some(p => p.slot_id === activeIdx)) {
      const active = kprofilesData.profiles.find(
        p => p.slot_id === activeIdx && (slotInfo.extruderId === undefined || p.extruder_id === slotInfo.extruderId),
      );
      if (active) result.unshift(active);
    }

    return result;
  }, [kprofilesData?.profiles, selectedPresetInfo, targetFilamentId, slotInfo.extruderId, slotInfo.caliIdx]);

  // Pre-select current profile when modal opens, reset when closes
  useEffect(() => {
    if (isOpen) {
      // The tray already reports the family id — that IS the selection.
      if (slotInfo.trayInfoIdx) {
        setSelectedPresetId(slotInfo.trayInfoIdx);
      }

      // Pre-populate color from current slot (black is valid - empty slots don't pass trayColor)
      if (slotInfo.trayColor) {
        const hex = slotInfo.trayColor.slice(0, 6);
        if (hex) {
          setColorHex(hex);
        }
      }
    } else {
      // Reset when modal closes
      setSelectedPresetId('');
      setSelectedKProfile(null);
      setColorHex('');
      setColorInput('');
      setSearchQuery('');
      setShowSuccess(false);
      scrolledToRef.current = '';
    }
  }, [isOpen, slotInfo.trayInfoIdx, slotInfo.trayColor]);

  // Auto-select best matching K profile when preset changes
  useEffect(() => {
    if (matchingKProfiles.length > 0) {
      // Prefer the currently-active K-profile (by cali_idx) if available
      if (slotInfo.caliIdx != null && slotInfo.caliIdx > 0) {
        const active = matchingKProfiles.find(p => p.slot_id === slotInfo.caliIdx);
        if (active) {
          setSelectedKProfile(active);
          return;
        }
      }
      // Fallback: first matching profile
      setSelectedKProfile(matchingKProfiles[0]);
    } else {
      setSelectedKProfile(null);
    }
  }, [selectedPresetId, matchingKProfiles, slotInfo.caliIdx]);

  // Escape key handler
  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    if (e.key === 'Escape') {
      onClose();
    }
  }, [onClose]);

  useEffect(() => {
    if (isOpen) {
      document.addEventListener('keydown', handleKeyDown);
      return () => document.removeEventListener('keydown', handleKeyDown);
    }
  }, [isOpen, handleKeyDown]);

  const isLoading = familiesLoading || kprofilesLoading;

  // Scroll selected preset into view when data finishes loading or the selection changes.
  // Uses a ref guard so scrollIntoView only fires once per selection, preventing the
  // infinite scroll loop that occurred on Windows with inline callback refs.
  useEffect(() => {
    if (!isLoading && selectedPresetId && selectedPresetId !== scrolledToRef.current) {
      const raf = requestAnimationFrame(() => {
          const modal = document.querySelector('[class*="fixed inset-0 z-50"]');
          const el = modal?.querySelector(`[data-preset-id="${CSS.escape(selectedPresetId)}"]`);
        if (el) {
          scrolledToRef.current = selectedPresetId;
          el.scrollIntoView({ block: 'nearest' });
        }
      });
      return () => cancelAnimationFrame(raf);
    }
  }, [selectedPresetId, isLoading]);

  if (!isOpen) return null;
  const canSave = selectedPresetId && !configureMutation.isPending;

  // Get display color (custom or slot default)
  const displayColor = colorHex || slotInfo.trayColor?.slice(0, 6) || 'FFFFFF';

  return (
    <div className={`fixed inset-0 z-50 flex ${fullScreen ? '' : 'items-center justify-center'}`}>
      {/* Backdrop */}
      {!fullScreen && (
        <div
          className="absolute inset-0 bg-black/60 backdrop-blur-sm"
          onClick={onClose}
        />
      )}

      {/* Modal */}
      <div className={fullScreen
        ? 'relative w-full h-full bg-bambu-dark-secondary flex flex-col'
        : 'relative w-full max-w-lg mx-4 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl'
      }>
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary shrink-0">
          <div className="flex items-center gap-2">
            <Settings2 className="w-5 h-5 text-bambu-blue" />
            <h2 className="text-lg font-semibold text-white">{t('configureAmsSlot.title')}</h2>
            {/* Inline slot info in fullScreen mode */}
            {fullScreen && (
              <div className="flex items-center gap-2 ml-4 text-sm text-bambu-gray">
                <span className="text-white/30">|</span>
                {slotInfo.trayColor && (
                  <span
                    className="w-4 h-4 rounded-full border border-black/20"
                    style={{ backgroundColor: `#${slotInfo.trayColor.slice(0, 6)}` }}
                  />
                )}
                <span className="text-white/70">
                  {t('configureAmsSlot.slotLabel', { ams: getAmsLabel(slotInfo.amsId, slotInfo.trayCount), slot: slotInfo.trayId + 1 })}
                </span>
                {slotInfo.traySubBrands && (
                  <span>({slotInfo.traySubBrands})</span>
                )}
              </div>
            )}
          </div>
          <button
            onClick={onClose}
            className="p-1 text-bambu-gray hover:text-white rounded transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Content */}
        <div className={`p-4 overflow-y-auto ${fullScreen ? 'flex-1 min-h-0' : 'space-y-4 max-h-[60vh]'}`}>
          {/* Success overlay */}
          {showSuccess && (
            <div className="absolute inset-0 bg-bambu-dark-secondary/95 z-10 flex items-center justify-center rounded-xl">
              <div className="text-center space-y-3">
                <CheckCircle2 className="w-16 h-16 text-bambu-green mx-auto" />
                <p className="text-lg font-semibold text-white">{t('configureAmsSlot.slotConfigured')}</p>
                <p className="text-sm text-bambu-gray">{t('configureAmsSlot.settingsSentToPrinter')}</p>
              </div>
            </div>
          )}

          {/* Slot info */}
          {!fullScreen && (
            <div className="p-3 bg-bambu-dark rounded-lg border border-bambu-dark-tertiary">
              <p className="text-xs text-bambu-gray mb-1">{t('configureAmsSlot.configuringSlot')}</p>
              <div className="flex items-center gap-2">
                {slotInfo.trayColor && (
                  <span
                    className="w-4 h-4 rounded-full border border-black/20"
                    style={{ backgroundColor: `#${slotInfo.trayColor.slice(0, 6)}` }}
                  />
                )}
                <span className="text-white font-medium">
                  {t('configureAmsSlot.slotLabel', { ams: getAmsLabel(slotInfo.amsId, slotInfo.trayCount), slot: slotInfo.trayId + 1 })}
                </span>
                {slotInfo.traySubBrands && (
                  <span className="text-bambu-gray">({slotInfo.traySubBrands})</span>
                )}
              </div>
            </div>
          )}

          {isLoading ? (
            <div className="flex justify-center py-8">
              <Loader2 className="w-6 h-6 text-bambu-green animate-spin" />
            </div>
          ) : fullScreen ? (
            /* Two-column layout for kiosk display */
            <div className="flex gap-4 h-full">
              {/* Left column: Filament preset list (takes full height) */}
              <div className="w-1/2 flex flex-col min-h-0">
                <label className="block text-sm text-bambu-gray mb-2">
                  {t('configureAmsSlot.filamentProfile')} <span className="text-red-600 dark:text-red-400">*</span>
                </label>
                <input
                  type="text"
                  placeholder={t('configureAmsSlot.searchPresets')}
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white placeholder:text-bambu-gray focus:border-bambu-green focus:outline-none mb-2 shrink-0"
                />
                <div className="flex-1 min-h-0 overflow-y-auto space-y-1">
                  {filteredPresets.length === 0 ? (
                    <p className="text-center py-4 text-bambu-gray">
                      {((familiesData?.length ?? 0) === 0)
                        ? t('configureAmsSlot.noPresetsAvailable')
                        : t('configureAmsSlot.noMatchingPresets')}
                    </p>
                  ) : (
                    filteredPresets.map((preset) => (
                      <button
                        key={preset.id}
                        data-preset-id={preset.id}
                        onClick={() => setSelectedPresetId(preset.id)}
                        className={`group w-full p-2 rounded-lg border text-left transition-colors ${
                          selectedPresetId === preset.id
                            ? 'bg-bambu-green/20 border-bambu-green'
                            : 'bg-bambu-dark border-bambu-dark-tertiary hover:border-bambu-gray'
                        }`}
                      >
                        <div className="flex items-center justify-between">
                          <span className="text-white text-sm truncate group-hover:whitespace-normal group-hover:break-all" title={preset.name}>{preset.name}</span>
                          <div className="flex items-center gap-1 flex-shrink-0">
                            {preset.source === 'local' && (
                              <span className="text-xs px-1.5 py-0.5 rounded bg-green-100 dark:bg-green-500/20 text-green-700 dark:text-green-400">
                                {t('profiles.localProfiles.badge')}
                              </span>
                            )}
                            {preset.source === 'orca_cloud' && (
                              <span className="text-xs px-1.5 py-0.5 rounded bg-purple-100 dark:bg-purple-500/20 text-purple-700 dark:text-purple-400">
                                {t('configureAmsSlot.orcaCloud')}
                              </span>
                            )}
                            {preset.source === 'cloud' && (
                              <span className="text-xs px-1.5 py-0.5 rounded bg-bambu-blue/20 text-bambu-blue">
                                {t('configureAmsSlot.bambuCloud')}
                              </span>
                            )}
                            {preset.source === 'builtin' && (
                              <span className="text-xs px-1.5 py-0.5 rounded bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400">
                                {t('configureAmsSlot.builtin')}
                              </span>
                            )}
                            {preset.isUser && (
                              <span className="text-xs px-1.5 py-0.5 rounded bg-bambu-blue/20 text-bambu-blue">
                                {t('configureAmsSlot.custom')}
                              </span>
                            )}
                          </div>
                        </div>
                      </button>
                    ))
                  )}
                </div>
              </div>

              {/* Right column: K Profile + Color */}
              <div className="w-1/2 flex flex-col gap-4 min-h-0 overflow-y-auto">
                {/* K Profile Select */}
                <div>
                  <label className="block text-sm text-bambu-gray mb-2">
                    {t('configureAmsSlot.kProfileLabel')}
                    {selectedMaterial && (
                      <span className="ml-2 text-xs text-bambu-blue">
                        {t('configureAmsSlot.filteringFor', { material: selectedMaterial })}
                      </span>
                    )}
                  </label>
                  {matchingKProfiles.length > 0 ? (
                    <div className="relative">
                      <select
                        value={selectedKProfile?.name || ''}
                        onChange={(e) => {
                          const profile = matchingKProfiles.find(p => p.name === e.target.value);
                          setSelectedKProfile(profile || null);
                        }}
                        className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none appearance-none pr-10"
                      >
                        <option value="">{t('configureAmsSlot.noKProfile')}</option>
                        {matchingKProfiles.map((profile) => (
                          <option key={`${profile.name}-${profile.extruder_id}`} value={profile.name}>
                            {profile.name} (K={profile.k_value})
                          </option>
                        ))}
                      </select>
                      <ChevronDown className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-bambu-gray pointer-events-none" />
                    </div>
                  ) : selectedPresetId ? (
                    <p className="text-sm text-bambu-gray italic py-2">
                      {t('configureAmsSlot.noMatchingKProfiles')}
                    </p>
                  ) : (
                    <span className="inline-block text-xs px-2 py-1 rounded bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400 border border-amber-300 dark:border-amber-500/30">
                      {t('configureAmsSlot.selectFilamentFirst')}
                    </span>
                  )}
                  {selectedKProfile && (
                    <p className="text-xs text-bambu-green mt-1">
                      {t('configureAmsSlot.kFromCalibration', { value: selectedKProfile.k_value })}
                    </p>
                  )}
                </div>

                {/* Custom color */}
                <div>
                  <label className="block text-sm text-bambu-gray mb-2">
                    {t('configureAmsSlot.customColorLabel')}
                  </label>
                  {catalogColors.length > 0 && (
                    <div className="mb-3">
                      <p className="text-xs text-bambu-gray mb-1.5">
                        {t('configureAmsSlot.presetColors', { name: selectedPresetInfo?.fullName })}
                      </p>
                      <div className="flex flex-wrap gap-1.5">
                        {catalogColors.map((entry) => (
                          <button
                            key={entry.id}
                            onClick={() => {
                              const hex = entry.hex_color.replace('#', '').toUpperCase();
                              setColorHex(hex);
                              setColorInput(entry.color_name);
                            }}
                            className={`h-7 px-2 rounded-md border-2 transition-all flex items-center gap-1.5 ${
                              colorHex === entry.hex_color.replace('#', '').toUpperCase()
                                ? 'border-bambu-green scale-105'
                                : 'border-white/20 hover:border-white/40'
                            }`}
                            title={entry.color_name}
                          >
                            <span
                              className="w-4 h-4 rounded-full border border-black/20 flex-shrink-0"
                              style={{ backgroundColor: entry.hex_color }}
                            />
                            <span className="text-xs text-white/80 whitespace-nowrap">{entry.color_name}</span>
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                  <div className="flex flex-wrap gap-1.5 mb-2">
                    {QUICK_COLORS_BASIC.map((color) => (
                      <button
                        key={color.hex}
                        onClick={() => {
                          setColorHex(color.hex);
                          setColorInput(color.name);
                        }}
                        className={`w-7 h-7 rounded-md border-2 transition-all ${
                          colorHex === color.hex
                            ? 'border-bambu-green scale-110'
                            : 'border-white/20 hover:border-white/40'
                        }`}
                        style={{ backgroundColor: `#${color.hex}` }}
                        title={color.name}
                      />
                    ))}
                    <button
                      onClick={() => setShowExtendedColors(!showExtendedColors)}
                      className="w-7 h-7 rounded-md border-2 border-white/20 hover:border-white/40 flex items-center justify-center text-white/60 hover:text-white/80 transition-all text-xs"
                      title={showExtendedColors ? t('configureAmsSlot.showLessColors') : t('configureAmsSlot.showMoreColors')}
                    >
                      {showExtendedColors ? '−' : '+'}
                    </button>
                  </div>
                  {showExtendedColors && (
                    <div className="flex flex-wrap gap-1.5 mb-2">
                      {QUICK_COLORS_EXTENDED.map((color) => (
                        <button
                          key={color.hex}
                          onClick={() => {
                            setColorHex(color.hex);
                            setColorInput(color.name);
                          }}
                          className={`w-7 h-7 rounded-md border-2 transition-all ${
                            colorHex === color.hex
                              ? 'border-bambu-green scale-110'
                              : 'border-white/20 hover:border-white/40'
                          }`}
                          style={{ backgroundColor: `#${color.hex}` }}
                          title={color.name}
                        />
                      ))}
                    </div>
                  )}
                  <div className="flex gap-2 items-center">
                    <div
                      className="w-10 h-10 rounded-lg border-2 border-white/20 flex-shrink-0"
                      style={{ backgroundColor: `#${displayColor}` }}
                    />
                    <input
                      type="text"
                      placeholder={t('configureAmsSlot.colorPlaceholder')}
                      value={colorInput}
                      onChange={(e) => {
                        const input = e.target.value;
                        setColorInput(input);
                        const nameHex = colorNameToHex(input);
                        if (nameHex) {
                          setColorHex(nameHex);
                        } else {
                          const cleaned = input.replace(/[^0-9A-Fa-f]/g, '').toUpperCase();
                          if (cleaned.length === 6) {
                            setColorHex(cleaned);
                          } else if (cleaned.length === 3) {
                            setColorHex(cleaned.split('').map(c => c + c).join(''));
                          }
                        }
                      }}
                      className="flex-1 px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white placeholder:text-bambu-gray focus:border-bambu-green focus:outline-none text-sm"
                    />
                    {colorHex && (
                      <button
                        onClick={() => {
                          setColorHex('');
                          setColorInput('');
                        }}
                        className="px-2 py-1 text-xs text-bambu-gray hover:text-white bg-bambu-dark-tertiary rounded"
                        title={t('configureAmsSlot.clearCustomColor')}
                      >
                        {t('configureAmsSlot.clear')}
                      </button>
                    )}
                  </div>
                  {colorHex && (
                    <p className="text-xs text-bambu-gray mt-1.5">
                      {t('configureAmsSlot.hexLabel', { hex: colorHex })}
                    </p>
                  )}
                </div>
              </div>
            </div>
          ) : (
            <>
              {/* Filament Profile Select */}
              <div>
                <label className="block text-sm text-bambu-gray mb-2">
                  {t('configureAmsSlot.filamentProfile')} <span className="text-red-600 dark:text-red-400">*</span>
                </label>
                <div className="relative">
                  <input
                    type="text"
                    placeholder={t('configureAmsSlot.searchPresets')}
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white placeholder:text-bambu-gray focus:border-bambu-green focus:outline-none mb-2"
                  />
                  <div className="max-h-48 overflow-y-auto space-y-1">
                    {filteredPresets.length === 0 ? (
                      <p className="text-center py-4 text-bambu-gray">
                        {((familiesData?.length ?? 0) === 0)
                          ? t('configureAmsSlot.noPresetsAvailable')
                          : t('configureAmsSlot.noMatchingPresets')}
                      </p>
                    ) : (
                      filteredPresets.map((preset) => (
                        <button
                          key={preset.id}
                          data-preset-id={preset.id}
                          onClick={() => setSelectedPresetId(preset.id)}
                          className={`group w-full p-2 rounded-lg border text-left transition-colors ${
                            selectedPresetId === preset.id
                              ? 'bg-bambu-green/20 border-bambu-green'
                              : 'bg-bambu-dark border-bambu-dark-tertiary hover:border-bambu-gray'
                          }`}
                        >
                          <div className="flex items-center justify-between">
                            <span className="text-white text-sm truncate group-hover:whitespace-normal group-hover:break-all" title={preset.name}>{preset.name}</span>
                            <div className="flex items-center gap-1 flex-shrink-0">
                              {preset.source === 'local' && (
                                <span className="text-xs px-1.5 py-0.5 rounded bg-green-100 dark:bg-green-500/20 text-green-700 dark:text-green-400">
                                  {t('profiles.localProfiles.badge')}
                                </span>
                              )}
                              {preset.source === 'orca_cloud' && (
                                <span className="text-xs px-1.5 py-0.5 rounded bg-purple-100 dark:bg-purple-500/20 text-purple-700 dark:text-purple-400">
                                  {t('configureAmsSlot.orcaCloud')}
                                </span>
                              )}
                              {preset.source === 'cloud' && (
                                <span className="text-xs px-1.5 py-0.5 rounded bg-bambu-blue/20 text-bambu-blue">
                                  {t('configureAmsSlot.bambuCloud')}
                                </span>
                              )}
                              {preset.source === 'builtin' && (
                                <span className="text-xs px-1.5 py-0.5 rounded bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400">
                                  {t('configureAmsSlot.builtin')}
                                </span>
                              )}
                              {preset.isUser && (
                                <span className="text-xs px-1.5 py-0.5 rounded bg-bambu-blue/20 text-bambu-blue">
                                  {t('configureAmsSlot.custom')}
                                </span>
                              )}
                            </div>
                          </div>
                        </button>
                      ))
                    )}
                  </div>
                </div>
              </div>

              {/* K Profile Select */}
              <div>
                <label className="block text-sm text-bambu-gray mb-2">
                  {t('configureAmsSlot.kProfileLabel')}
                  {selectedMaterial && (
                    <span className="ml-2 text-xs text-bambu-blue">
                      {t('configureAmsSlot.filteringFor', { material: selectedMaterial })}
                    </span>
                  )}
                </label>
                {matchingKProfiles.length > 0 ? (
                  <div className="relative">
                    <select
                      value={selectedKProfile?.name || ''}
                      onChange={(e) => {
                        const profile = matchingKProfiles.find(p => p.name === e.target.value);
                        setSelectedKProfile(profile || null);
                      }}
                      className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none appearance-none pr-10"
                    >
                      <option value="">{t('configureAmsSlot.noKProfile')}</option>
                      {matchingKProfiles.map((profile) => (
                        <option key={`${profile.name}-${profile.extruder_id}`} value={profile.name}>
                          {profile.name} (K={profile.k_value})
                        </option>
                      ))}
                    </select>
                    <ChevronDown className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-bambu-gray pointer-events-none" />
                  </div>
                ) : selectedPresetId ? (
                  <p className="text-sm text-bambu-gray italic py-2">
                    {t('configureAmsSlot.noMatchingKProfiles')}
                  </p>
                ) : (
                  <span className="inline-block text-xs px-2 py-1 rounded bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-400 border border-amber-300 dark:border-amber-500/30">
                    {t('configureAmsSlot.selectFilamentFirst')}
                  </span>
                )}
                {selectedKProfile && (
                  <p className="text-xs text-bambu-green mt-1">
                    {t('configureAmsSlot.kFromCalibration', { value: selectedKProfile.k_value })}
                  </p>
                )}
              </div>

              {/* Optional: Custom color */}
              <div>
                <label className="block text-sm text-bambu-gray mb-2">
                  {t('configureAmsSlot.customColorLabel')}
                </label>
                {/* Catalog colors matching selected preset */}
                {catalogColors.length > 0 && (
                  <div className="mb-3">
                    <p className="text-xs text-bambu-gray mb-1.5">
                      {t('configureAmsSlot.presetColors', { name: selectedPresetInfo?.fullName })}
                    </p>
                    <div className="flex flex-wrap gap-1.5">
                      {catalogColors.map((entry) => (
                        <button
                          key={entry.id}
                          onClick={() => {
                            const hex = entry.hex_color.replace('#', '').toUpperCase();
                            setColorHex(hex);
                            setColorInput(entry.color_name);
                          }}
                          className={`h-7 px-2 rounded-md border-2 transition-all flex items-center gap-1.5 ${
                            colorHex === entry.hex_color.replace('#', '').toUpperCase()
                              ? 'border-bambu-green scale-105'
                              : 'border-white/20 hover:border-white/40'
                          }`}
                          title={entry.color_name}
                        >
                          <span
                            className="w-4 h-4 rounded-full border border-black/20 flex-shrink-0"
                            style={{ backgroundColor: entry.hex_color }}
                          />
                          <span className="text-xs text-white/80 whitespace-nowrap">{entry.color_name}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                )}
                {/* Quick color buttons */}
                <div className="flex flex-wrap gap-1.5 mb-2">
                  {QUICK_COLORS_BASIC.map((color) => (
                    <button
                      key={color.hex}
                      onClick={() => {
                        setColorHex(color.hex);
                        setColorInput(color.name);
                      }}
                      className={`w-7 h-7 rounded-md border-2 transition-all ${
                        colorHex === color.hex
                          ? 'border-bambu-green scale-110'
                          : 'border-white/20 hover:border-white/40'
                      }`}
                      style={{ backgroundColor: `#${color.hex}` }}
                      title={color.name}
                    />
                  ))}
                  <button
                    onClick={() => setShowExtendedColors(!showExtendedColors)}
                    className="w-7 h-7 rounded-md border-2 border-white/20 hover:border-white/40 flex items-center justify-center text-white/60 hover:text-white/80 transition-all text-xs"
                    title={showExtendedColors ? t('configureAmsSlot.showLessColors') : t('configureAmsSlot.showMoreColors')}
                  >
                    {showExtendedColors ? '−' : '+'}
                  </button>
                </div>
                {/* Extended colors (collapsible) */}
                {showExtendedColors && (
                  <div className="flex flex-wrap gap-1.5 mb-2">
                    {QUICK_COLORS_EXTENDED.map((color) => (
                      <button
                        key={color.hex}
                        onClick={() => {
                          setColorHex(color.hex);
                          setColorInput(color.name);
                        }}
                        className={`w-7 h-7 rounded-md border-2 transition-all ${
                          colorHex === color.hex
                            ? 'border-bambu-green scale-110'
                            : 'border-white/20 hover:border-white/40'
                        }`}
                        style={{ backgroundColor: `#${color.hex}` }}
                        title={color.name}
                      />
                    ))}
                  </div>
                )}
                {/* Color input: name or hex */}
                <div className="flex gap-2 items-center">
                  <div
                    className="w-10 h-10 rounded-lg border-2 border-white/20 flex-shrink-0"
                    style={{ backgroundColor: `#${displayColor}` }}
                  />
                  <input
                    type="text"
                    placeholder={t('configureAmsSlot.colorPlaceholder')}
                    value={colorInput}
                    onChange={(e) => {
                      const input = e.target.value;
                      setColorInput(input);

                      // Try to parse as color name first
                      const nameHex = colorNameToHex(input);
                      if (nameHex) {
                        setColorHex(nameHex);
                      } else {
                        // Try to parse as hex code
                        const cleaned = input.replace(/[^0-9A-Fa-f]/g, '').toUpperCase();
                        if (cleaned.length === 6) {
                          setColorHex(cleaned);
                        } else if (cleaned.length === 3) {
                          // Expand shorthand hex (e.g., F00 -> FF0000)
                          setColorHex(cleaned.split('').map(c => c + c).join(''));
                        }
                      }
                    }}
                    className="flex-1 px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white placeholder:text-bambu-gray focus:border-bambu-green focus:outline-none text-sm"
                  />
                  {colorHex && (
                    <button
                      onClick={() => {
                        setColorHex('');
                        setColorInput('');
                      }}
                      className="px-2 py-1 text-xs text-bambu-gray hover:text-white bg-bambu-dark-tertiary rounded"
                      title={t('configureAmsSlot.clearCustomColor')}
                    >
                      {t('configureAmsSlot.clear')}
                    </button>
                  )}
                </div>
                {colorHex && (
                  <p className="text-xs text-bambu-gray mt-1.5">
                    {t('configureAmsSlot.hexLabel', { hex: colorHex })}
                  </p>
                )}
              </div>
            </>
          )}
        </div>

        {/* Footer */}
        <div className="flex justify-between p-4 border-t border-bambu-dark-tertiary shrink-0">
          {/* Reset button on the left */}
          <Button
            variant="secondary"
            onClick={() => resetMutation.mutate()}
            disabled={resetMutation.isPending || configureMutation.isPending}
            className="text-red-600 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300 hover:bg-red-50 dark:hover:bg-red-500/10"
          >
            {resetMutation.isPending ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                {t('configureAmsSlot.resetting')}
              </>
            ) : (
              <>
                <RotateCcw className="w-4 h-4" />
                {t('configureAmsSlot.resetSlot')}
              </>
            )}
          </Button>
          {/* Cancel and Configure buttons on the right */}
          <div className="flex gap-2">
            <Button variant="secondary" onClick={onClose}>
              {t('configureAmsSlot.cancel')}
            </Button>
            <Button
              onClick={() => configureMutation.mutate()}
              disabled={!canSave}
            >
              {configureMutation.isPending ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  {t('configureAmsSlot.configuring')}
                </>
              ) : (
                <>
                  <Settings2 className="w-4 h-4" />
                  {t('configureAmsSlot.configureSlot')}
                </>
              )}
            </Button>
          </div>
        </div>

        {/* Error */}
        {(configureMutation.isError || resetMutation.isError) && (
          <div className="mx-4 mb-4 p-2 bg-red-100 dark:bg-red-500/20 border border-red-300 dark:border-red-500/50 rounded text-sm text-red-700 dark:text-red-400">
            {(configureMutation.error as Error)?.message || (resetMutation.error as Error)?.message}
          </div>
        )}
      </div>
    </div>
  );
}
