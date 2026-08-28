/**
 * CreateFilamentFamilyModal — BamDude's analog of BambuStudio's Create
 * Filament dialog (spec B): vendor + type + serial -> a new family with a
 * BS-compatible P-hash id and one root preset per chosen printer PROFILE
 * (BS preset names, not BamDude devices), with cloud push per variant.
 *
 * Variants (each tab forces one destination and offers the others):
 * - 'local' (Profiles → Local, spool form): saved locally; optional
 *   "also push to Bambu Cloud" / "also push to Orca Cloud".
 * - 'bambu' (Profiles → Bambu Cloud): pushed to the cloud; optional
 *   "also keep locally". Unchecked = cloud-only, the sync mirrors it back.
 * - 'orca' (Profiles → Orca Cloud): same shape against Orca Cloud; needs a
 *   pairing whose granted scope carries sync:write.
 */
import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';
import { api } from '../api/client';
import type { CreateFamilyResponse, UnifiedPreset } from '../api/client';
import { Button } from './Button';
import { useToast } from '../contexts/ToastContext';

interface CreateFilamentFamilyModalProps {
  open: boolean;
  onClose: () => void;
  onCreated?: (filamentId: string, alias: string) => void;
  variant?: 'local' | 'bambu' | 'orca';
}

const RESERVED_VENDORS = new Set(['bambu', 'generic']);

export function CreateFilamentFamilyModal({ open, onClose, onCreated, variant = 'local' }: CreateFilamentFamilyModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [vendor, setVendor] = useState('');
  const [filamentType, setFilamentType] = useState('PLA');
  const [serial, setSerial] = useState('');
  const [checkedNames, setCheckedNames] = useState<string[] | null>(null); // null = defaults not applied yet
  const [printerQuery, setPrinterQuery] = useState('');
  const [sourceMode, setSourceMode] = useState<'type' | 'preset'>('type');
  const [sourceKey, setSourceKey] = useState(''); // "<source>:<id>"
  const [secondaryChecked, setSecondaryChecked] = useState(false); // local: push bambu / cloud tabs: keep locally too
  const [orcaChecked, setOrcaChecked] = useState(false); // local variant: also push to Orca Cloud
  const [resultNotes, setResultNotes] = useState<string[]>([]);

  const { data: options } = useQuery({
    queryKey: ['filament-authoring-options'],
    queryFn: () => api.getFilamentAuthoringOptions(),
    enabled: open,
    staleTime: 5 * 60_000,
  });
  const { data: printers } = useQuery({
    queryKey: ['printers'],
    queryFn: () => api.getPrinters(),
    enabled: open,
  });
  const { data: printerModels } = useQuery({
    queryKey: ['printer-models'],
    queryFn: () => api.getPrinterModels(),
    enabled: open,
    staleTime: 10 * 60_000,
  });
  const { data: cloudStatus } = useQuery({
    queryKey: ['cloud-status'],
    queryFn: () => api.getCloudStatus(),
    enabled: open,
    staleTime: 60_000,
  });
  const { data: orcaStatus } = useQuery({
    queryKey: ['orca-cloud-status'],
    queryFn: () => api.orcaCloudStatus(),
    enabled: open,
    staleTime: 60_000,
  });
  const { data: presets } = useQuery({
    queryKey: ['slicer-presets'],
    queryFn: () => api.getSlicerPresets(),
    enabled: open && sourceMode === 'preset',
    staleTime: 60_000,
  });

  const allPrinterNames = useMemo(() => options?.printer_names || [], [options]);

  // Default selection: every nozzle variant of the models the farm actually
  // has (device model "P1S" -> long registry name -> profile names containing
  // it). BS preselects the user's installed printers the same way.
  useEffect(() => {
    if (!open || checkedNames !== null || allPrinterNames.length === 0) return;
    const models = new Set((printers || []).map((p) => (p.model || '').toUpperCase()).filter(Boolean));
    if (models.size === 0 || !printerModels) {
      setCheckedNames([]);
      return;
    }
    const longNames = Object.entries(printerModels)
      .filter(([, short]) => models.has(short.toUpperCase()))
      .map(([long]) => long);
    setCheckedNames(allPrinterNames.filter((n) => longNames.some((long) => n.startsWith(long))));
  }, [open, checkedNames, allPrinterNames, printers, printerModels]);

  const visiblePrinterNames = useMemo(() => {
    const q = printerQuery.trim().toLowerCase();
    return q ? allPrinterNames.filter((n) => n.toLowerCase().includes(q)) : allPrinterNames;
  }, [allPrinterNames, printerQuery]);

  // "From an existing preset" candidates: local + both clouds. The standard
  // tier is deliberately absent — "from type" IS the bundled-generic path.
  const presetChoices = useMemo(() => {
    if (!presets) return [] as Array<{ key: string; label: string }>;
    const tiers: Array<{ source: string; label: string; rows: UnifiedPreset[] }> = [
      { source: 'local', label: t('authoring.tierLocal'), rows: presets.local?.filament || [] },
      { source: 'cloud', label: t('authoring.tierBambu'), rows: presets.cloud?.filament || [] },
      { source: 'orca_cloud', label: t('authoring.tierOrca'), rows: presets.orca_cloud?.filament || [] },
    ];
    return tiers.flatMap((tier) =>
      tier.rows.map((p) => ({ key: `${tier.source}:${p.id}`, label: `${p.name} (${tier.label})` })),
    );
  }, [presets, t]);

  const vendorError = useMemo(() => {
    const v = vendor.trim();
    if (!v) return null; // "required" shows via the disabled submit
    if (RESERVED_VENDORS.has(v.toLowerCase())) return t('authoring.vendorReserved');
    if (/^\d+$/.test(v)) return t('authoring.vendorDigits');
    return null;
  }, [vendor, t]);

  const cloudConnected = !!cloudStatus?.is_authenticated && !!options?.push?.bambu;
  const orcaConnected = !!orcaStatus?.connected && !!options?.push?.orca;
  // The granted scope is baked into the pairing — a read-only pairing cannot
  // push, whatever the defaults say now.
  const orcaWritable = orcaConnected && (orcaStatus?.scope || '').includes('sync:write');
  const canSubmit =
    vendor.trim() !== '' &&
    serial.trim() !== '' &&
    !vendorError &&
    (variant !== 'bambu' || cloudConnected) &&
    (variant !== 'orca' || orcaWritable);

  const createMutation = useMutation({
    mutationFn: () => {
      const [source, sourceId] = sourceKey.includes(':')
        ? [sourceKey.slice(0, sourceKey.indexOf(':')), sourceKey.slice(sourceKey.indexOf(':') + 1)]
        : [null, null];
      return api.createFilamentFamily({
        vendor: vendor.trim(),
        filament_type: filamentType,
        serial: serial.trim(),
        printer_ids: [],
        printer_names: checkedNames ?? [],
        source_mode: sourceMode,
        source: sourceMode === 'preset' ? source : null,
        source_id: sourceMode === 'preset' ? sourceId : null,
        push_to_bambu: variant === 'bambu' ? true : variant === 'local' && secondaryChecked,
        push_to_orca: variant === 'orca' ? true : variant === 'local' && orcaChecked,
        save_local: variant === 'local' ? true : secondaryChecked,
      });
    },
    onSuccess: (result: CreateFamilyResponse) => {
      queryClient.invalidateQueries({ queryKey: ['filamentFamilies'] });
      queryClient.invalidateQueries({ queryKey: ['localPresets'] });
      queryClient.invalidateQueries({ queryKey: ['cloud-settings'] });
      const notes: string[] = [...result.warnings];
      for (const root of result.roots) {
        if (root.error) notes.push(`${root.printer_name || root.printer_id}: ${root.error}`);
      }
      for (const p of [...(result.push || []), ...(result.push_orca || [])]) {
        if (p.status === 'error') notes.push(`${p.name || ''}: ${p.detail || p.status}`);
      }
      if (result.attached) {
        showToast(t('authoring.attached', { name: result.name }));
      } else {
        showToast(t('authoring.created'));
      }
      onCreated?.(result.filament_id, result.name);
      if (notes.length > 0) {
        setResultNotes(notes); // keep the dialog open so the notes are read
        return;
      }
      onClose();
    },
    onError: (e: Error) => {
      showToast(`${t('authoring.createFailed')}: ${e.message}`, 'error');
    },
  });

  if (!open) return null;

  const toggleName = (name: string) => {
    const next = new Set(checkedNames ?? []);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    setCheckedNames(Array.from(next));
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="bg-bambu-dark border border-bambu-dark-tertiary rounded-xl w-full max-w-md max-h-[90vh] overflow-y-auto shadow-2xl">
        <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white">{t('authoring.title')}</h2>
          <button type="button" onClick={onClose} className="text-bambu-gray hover:text-white">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-4 space-y-4">
          <div>
            <label className="block text-sm text-bambu-gray-light mb-1">{t('authoring.vendor')}</label>
            <input
              value={vendor}
              onChange={(e) => setVendor(e.target.value)}
              className="w-full p-2.5 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary text-sm text-white outline-none focus:border-bambu-green"
              placeholder="Polymaker"
            />
            {vendorError && <p className="mt-1 text-xs text-red-600 dark:text-red-400">{vendorError}</p>}
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-bambu-gray-light mb-1">{t('authoring.type')}</label>
              <select
                value={filamentType}
                onChange={(e) => setFilamentType(e.target.value)}
                className="w-full p-2.5 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary text-sm text-white outline-none focus:border-bambu-green"
              >
                {(options?.filament_types || ['PLA']).map((ft) => (
                  <option key={ft} value={ft}>
                    {ft}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm text-bambu-gray-light mb-1">{t('authoring.serial')}</label>
              <input
                value={serial}
                onChange={(e) => setSerial(e.target.value)}
                className="w-full p-2.5 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary text-sm text-white outline-none focus:border-bambu-green"
                placeholder="Basic"
              />
            </div>
          </div>

          {vendor.trim() && serial.trim() && (
            <p className="text-xs text-bambu-gray">
              {t('authoring.namePreview')}: <span className="text-white">{`${vendor.trim()} ${filamentType} ${serial.trim()}`}</span>
            </p>
          )}

          <div>
            <label className="block text-sm text-bambu-gray-light mb-1">{t('authoring.sourceMode')}</label>
            <div className="flex gap-4 text-sm text-white">
              <label className="flex items-center gap-1.5">
                <input type="radio" checked={sourceMode === 'type'} onChange={() => setSourceMode('type')} />
                {t('authoring.sourceModeType')}
              </label>
              <label className="flex items-center gap-1.5">
                <input type="radio" checked={sourceMode === 'preset'} onChange={() => setSourceMode('preset')} />
                {t('authoring.sourceModePreset')}
              </label>
            </div>
            {sourceMode === 'preset' && (
              <select
                value={sourceKey}
                onChange={(e) => setSourceKey(e.target.value)}
                className="mt-2 w-full p-2.5 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary text-sm text-white outline-none focus:border-bambu-green"
              >
                <option value="">{t('authoring.pickPreset')}</option>
                {presetChoices.map((c) => (
                  <option key={c.key} value={c.key}>
                    {c.label}
                  </option>
                ))}
              </select>
            )}
          </div>

          <div>
            <label className="block text-sm text-bambu-gray-light mb-1">{t('authoring.printers')}</label>
            <input
              value={printerQuery}
              onChange={(e) => setPrinterQuery(e.target.value)}
              placeholder={t('authoring.printerSearch')}
              className="w-full mb-1.5 px-2.5 py-1.5 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary text-sm text-white outline-none focus:border-bambu-green"
            />
            <div className="space-y-1 max-h-40 overflow-y-auto rounded-lg border border-bambu-dark-tertiary p-2">
              {visiblePrinterNames.map((name) => (
                <label key={name} className="flex items-center gap-2 text-sm text-white">
                  <input
                    type="checkbox"
                    checked={(checkedNames ?? []).includes(name)}
                    onChange={() => toggleName(name)}
                  />
                  {name}
                </label>
              ))}
              {visiblePrinterNames.length === 0 && (
                <p className="text-xs text-bambu-gray">{t('authoring.noPrinters')}</p>
              )}
            </div>
            {(checkedNames ?? []).length > 0 && (
              <p className="mt-1 text-xs text-bambu-gray">
                {t('authoring.printersSelected', { count: (checkedNames ?? []).length })}
              </p>
            )}
          </div>

          {variant === 'local' && cloudConnected && (
            <label className="flex items-center gap-2 text-sm text-white">
              <input
                type="checkbox"
                checked={secondaryChecked}
                onChange={(e) => setSecondaryChecked(e.target.checked)}
              />
              {t('authoring.pushBambu')}
            </label>
          )}
          {variant === 'local' && orcaConnected && (
            <label
              className={`flex items-center gap-2 text-sm ${orcaWritable ? 'text-white' : 'text-bambu-gray'}`}
              title={orcaWritable ? undefined : t('authoring.orcaNeedsWrite')}
            >
              <input
                type="checkbox"
                disabled={!orcaWritable}
                checked={orcaChecked && orcaWritable}
                onChange={(e) => setOrcaChecked(e.target.checked)}
              />
              {t('authoring.pushOrca')}
            </label>
          )}
          {(variant === 'bambu' || variant === 'orca') && (
            <label className="flex items-center gap-2 text-sm text-white">
              <input
                type="checkbox"
                checked={secondaryChecked}
                onChange={(e) => setSecondaryChecked(e.target.checked)}
              />
              {t('authoring.saveLocal')}
            </label>
          )}
          {variant === 'bambu' && !cloudConnected && (
            <p className="text-xs text-red-600 dark:text-red-400">{t('authoring.cloudRequired')}</p>
          )}
          {variant === 'orca' && !orcaConnected && (
            <p className="text-xs text-red-600 dark:text-red-400">{t('authoring.orcaRequired')}</p>
          )}
          {variant === 'orca' && orcaConnected && !orcaWritable && (
            <p className="text-xs text-red-600 dark:text-red-400">{t('authoring.orcaNeedsWrite')}</p>
          )}

          {resultNotes.length > 0 && (
            <div className="rounded-lg border border-yellow-700/60 bg-yellow-500/10 p-3">
              <p className="text-sm text-yellow-700 dark:text-yellow-300 font-medium mb-1">{t('authoring.warningsTitle')}</p>
              <ul className="text-xs text-yellow-700/80 dark:text-yellow-200/80 list-disc pl-4 space-y-0.5">
                {resultNotes.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 p-4 border-t border-bambu-dark-tertiary">
          <Button variant="secondary" onClick={onClose}>
            {resultNotes.length > 0 ? t('common.close') : t('common.cancel')}
          </Button>
          {resultNotes.length === 0 && (
            <Button onClick={() => createMutation.mutate()} disabled={!canSubmit || createMutation.isPending}>
              {t('authoring.createButton')}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
