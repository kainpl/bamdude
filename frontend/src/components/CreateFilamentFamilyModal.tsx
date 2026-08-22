/**
 * CreateFilamentFamilyModal — BamDude's analog of BambuStudio's Create
 * Filament dialog (spec B): vendor + type + serial -> a new family with a
 * BS-compatible P-hash id and one root preset per chosen printer, with an
 * optional push of the presets to Bambu Cloud.
 *
 * Two entry points render it: the Profiles page's Local tab and the spool
 * form's family picker ("Create new family…").
 */
import { useMemo, useState } from 'react';
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
}

const RESERVED_VENDORS = new Set(['bambu', 'generic']);

export function CreateFilamentFamilyModal({ open, onClose, onCreated }: CreateFilamentFamilyModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [vendor, setVendor] = useState('');
  const [filamentType, setFilamentType] = useState('PLA');
  const [serial, setSerial] = useState('');
  const [printerIds, setPrinterIds] = useState<number[] | null>(null); // null = all (default)
  const [sourceMode, setSourceMode] = useState<'type' | 'preset'>('type');
  const [sourceKey, setSourceKey] = useState(''); // "<source>:<id>"
  const [pushToBambu, setPushToBambu] = useState(false);
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
  const { data: cloudStatus } = useQuery({
    queryKey: ['cloud-status'],
    queryFn: () => api.getCloudStatus(),
    enabled: open,
    staleTime: 60_000,
  });
  const { data: presets } = useQuery({
    queryKey: ['slicer-presets'],
    queryFn: () => api.getSlicerPresets(),
    enabled: open && sourceMode === 'preset',
    staleTime: 60_000,
  });

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

  const allPrinterIds = useMemo(() => (printers || []).map((p) => p.id), [printers]);
  const checkedIds = printerIds ?? allPrinterIds;

  const vendorError = useMemo(() => {
    const v = vendor.trim();
    if (!v) return null; // "required" shows on submit attempt via disabled state
    if (RESERVED_VENDORS.has(v.toLowerCase())) return t('authoring.vendorReserved');
    if (/^\d+$/.test(v)) return t('authoring.vendorDigits');
    return null;
  }, [vendor, t]);

  const canSubmit = vendor.trim() !== '' && serial.trim() !== '' && !vendorError;

  const createMutation = useMutation({
    mutationFn: () => {
      const [source, sourceId] = sourceKey.includes(':')
        ? [sourceKey.slice(0, sourceKey.indexOf(':')), sourceKey.slice(sourceKey.indexOf(':') + 1)]
        : [null, null];
      return api.createFilamentFamily({
        vendor: vendor.trim(),
        filament_type: filamentType,
        serial: serial.trim(),
        printer_ids: checkedIds,
        source_mode: sourceMode,
        source: sourceMode === 'preset' ? source : null,
        source_id: sourceMode === 'preset' ? sourceId : null,
        push_to_bambu: pushToBambu,
      });
    },
    onSuccess: (result: CreateFamilyResponse) => {
      queryClient.invalidateQueries({ queryKey: ['filamentFamilies'] });
      queryClient.invalidateQueries({ queryKey: ['localPresets'] });
      const notes: string[] = [...result.warnings];
      for (const root of result.roots) {
        if (root.error) notes.push(`${root.printer_name || root.printer_id}: ${root.error}`);
      }
      for (const p of result.push || []) {
        if (p.status === 'error') notes.push(`${p.name || ''}: ${p.detail || p.status}`);
      }
      if (result.attached) {
        showToast(t('authoring.attached', { name: result.name }));
      } else {
        showToast(t('authoring.created'));
      }
      if (notes.length > 0) {
        setResultNotes(notes); // keep the dialog open so the notes are read
        onCreated?.(result.filament_id, result.name);
        return;
      }
      onCreated?.(result.filament_id, result.name);
      onClose();
    },
    onError: (e: Error) => {
      showToast(`${t('authoring.createFailed')}: ${e.message}`, 'error');
    },
  });

  if (!open) return null;

  const togglePrinter = (id: number) => {
    const next = new Set(checkedIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setPrinterIds(Array.from(next));
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="bg-bambu-dark border border-gray-700 rounded-xl w-full max-w-md max-h-[90vh] overflow-y-auto shadow-2xl">
        <div className="flex items-center justify-between p-4 border-b border-gray-700">
          <h2 className="text-lg font-semibold text-white">{t('authoring.title')}</h2>
          <button type="button" onClick={onClose} className="text-gray-400 hover:text-white">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-4 space-y-4">
          <div>
            <label className="block text-sm text-gray-300 mb-1">{t('authoring.vendor')}</label>
            <input
              value={vendor}
              onChange={(e) => setVendor(e.target.value)}
              className="w-full p-2.5 rounded-lg bg-bambu-darker border border-gray-600 text-sm text-white outline-none"
              placeholder="Polymaker"
            />
            {vendorError && <p className="mt-1 text-xs text-red-400">{vendorError}</p>}
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-gray-300 mb-1">{t('authoring.type')}</label>
              <select
                value={filamentType}
                onChange={(e) => setFilamentType(e.target.value)}
                className="w-full p-2.5 rounded-lg bg-bambu-darker border border-gray-600 text-sm text-white outline-none"
              >
                {(options?.filament_types || ['PLA']).map((ft) => (
                  <option key={ft} value={ft}>
                    {ft}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm text-gray-300 mb-1">{t('authoring.serial')}</label>
              <input
                value={serial}
                onChange={(e) => setSerial(e.target.value)}
                className="w-full p-2.5 rounded-lg bg-bambu-darker border border-gray-600 text-sm text-white outline-none"
                placeholder="Basic"
              />
            </div>
          </div>

          {vendor.trim() && serial.trim() && (
            <p className="text-xs text-gray-400">
              {t('authoring.namePreview')}: <span className="text-white">{`${vendor.trim()} ${filamentType} ${serial.trim()}`}</span>
            </p>
          )}

          <div>
            <label className="block text-sm text-gray-300 mb-1">{t('authoring.sourceMode')}</label>
            <div className="flex gap-4 text-sm text-gray-200">
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
                className="mt-2 w-full p-2.5 rounded-lg bg-bambu-darker border border-gray-600 text-sm text-white outline-none"
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
            <label className="block text-sm text-gray-300 mb-1">{t('authoring.printers')}</label>
            <div className="space-y-1 max-h-36 overflow-y-auto rounded-lg border border-gray-700 p-2">
              {(printers || []).map((p) => (
                <label key={p.id} className="flex items-center gap-2 text-sm text-gray-200">
                  <input type="checkbox" checked={checkedIds.includes(p.id)} onChange={() => togglePrinter(p.id)} />
                  {p.name}
                </label>
              ))}
              {(printers || []).length === 0 && (
                <p className="text-xs text-gray-500">{t('authoring.noPrinters')}</p>
              )}
            </div>
          </div>

          {options?.push?.bambu && cloudStatus?.is_authenticated && (
            <label className="flex items-center gap-2 text-sm text-gray-200">
              <input type="checkbox" checked={pushToBambu} onChange={(e) => setPushToBambu(e.target.checked)} />
              {t('authoring.pushBambu')}
            </label>
          )}

          {resultNotes.length > 0 && (
            <div className="rounded-lg border border-yellow-700/60 bg-yellow-900/20 p-3">
              <p className="text-sm text-yellow-300 font-medium mb-1">{t('authoring.warningsTitle')}</p>
              <ul className="text-xs text-yellow-200/80 list-disc pl-4 space-y-0.5">
                {resultNotes.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 p-4 border-t border-gray-700">
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
