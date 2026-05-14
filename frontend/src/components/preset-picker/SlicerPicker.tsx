import { Check, Cog, Loader2, XCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { useSlicerHealth } from '../../hooks/useSlicerHealth';

export type SlicerKind = 'orcaslicer' | 'bambu_studio';

interface SlicerPickerProps {
  value: SlicerKind | null;
  onChange: (next: SlicerKind) => void;
  disabled?: boolean;
}

/**
 * Per-job slicer picker, card-style — mirrors the "Filament Tracking"
 * section in Settings. Two big card-buttons, each carrying a live
 * reachability badge (version / offline / checking) so the user sees
 * what's available WITHOUT a separate status strip cluttering the modal.
 *
 * Disabled cards (sidecar offline) cannot be picked. When only one is
 * healthy the calling form still works — pre-select whatever is
 * reachable; if neither, the backend will surface the real error at
 * submit time.
 */
export function SlicerPicker({ value, onChange, disabled }: SlicerPickerProps) {
  const { t } = useTranslation();
  const options: { kind: SlicerKind; label: string }[] = [
    { kind: 'orcaslicer', label: 'OrcaSlicer' },
    { kind: 'bambu_studio', label: 'BambuStudio' },
  ];
  return (
    <fieldset>
      <legend className="text-xs text-bambu-gray mb-2">
        {t('slice.sidecarPicker', 'Slice with')}
      </legend>
      <div className="grid grid-cols-2 gap-3">
        {options.map((opt) => (
          <SlicerPickerCard
            key={opt.kind}
            kind={opt.kind}
            label={opt.label}
            selected={value === opt.kind}
            outerDisabled={!!disabled}
            onSelect={() => onChange(opt.kind)}
          />
        ))}
      </div>
    </fieldset>
  );
}

interface SlicerPickerCardProps {
  kind: SlicerKind;
  label: string;
  selected: boolean;
  outerDisabled: boolean;
  onSelect: () => void;
}

function SlicerPickerCard({
  kind,
  label,
  selected,
  outerDisabled,
  onSelect,
}: SlicerPickerCardProps) {
  const { t } = useTranslation();
  const { data, isLoading } = useSlicerHealth(kind);
  const healthy = data?.healthy === true;
  const version = data?.version ?? null;
  const error = data?.error ?? null;
  // Disable when the outer form is busy OR the sidecar isn't reachable —
  // clicking an offline card can't lead to a successful slice.
  const isDisabled = outerDisabled || isLoading || !healthy;

  const statusLabel = isLoading
    ? t('slicerHealth.statusChecking', 'checking…')
    : healthy
      ? t('slicerHealth.cardReady', { version: version ?? '?', defaultValue: 'Ready · v{{version}}' })
      : t('slicerHealth.cardOffline', 'Offline');

  const baseClass = selected
    ? 'border-bambu-green bg-bambu-green/10'
    : isDisabled
      ? 'border-bambu-dark-tertiary bg-bambu-dark/50 opacity-60 cursor-not-allowed'
      : 'border-bambu-dark-tertiary bg-bambu-dark hover:border-bambu-gray/50';
  const iconClass = selected ? 'text-bambu-green' : healthy ? 'text-bambu-gray' : 'text-red-400';
  const titleClass = selected ? 'text-white' : 'text-bambu-gray';
  const descClass = selected ? 'text-bambu-gray' : 'text-bambu-gray/60';

  return (
    <button
      type="button"
      onClick={() => {
        if (!isDisabled) onSelect();
      }}
      disabled={isDisabled}
      className={`p-3 rounded-lg border-2 text-left transition-colors ${baseClass}`}
    >
      <div className="flex items-center gap-2 mb-1.5">
        {isLoading ? (
          <Loader2 className={`w-4 h-4 animate-spin ${iconClass}`} />
        ) : healthy ? (
          <Cog className={`w-4 h-4 ${iconClass}`} />
        ) : (
          <XCircle className={`w-4 h-4 ${iconClass}`} />
        )}
        <span className={`text-sm font-medium ${titleClass}`}>{label}</span>
      </div>
      <p className={`text-xs ${descClass}`}>{statusLabel}</p>
      {!isLoading && !healthy && error && (
        <p className="text-xs text-red-400/80 mt-1 break-words">{error}</p>
      )}
      {selected && healthy && (
        <div className="flex items-center gap-1 mt-2">
          <Check className="w-3 h-3 text-bambu-green" />
          <span className="text-xs text-bambu-green">{t('slicerHealth.active', 'active')}</span>
        </div>
      )}
    </button>
  );
}
