// The slicer cards of the slicing-setup family (spec slicing-setup r5, R7):
// two card-buttons, each with its live health (version / offline / checking),
// offline ones disabled, the selected one announced, the outdated-sidecar note
// under them. The markup is the Slice dialog's.
import { Check, Cog, Loader2, XCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { useSlicerHealth, type SlicerKind } from '../../hooks/useSlicerHealth';
import { SlicerOutdatedNote } from '../SlicerOutdatedNote';

interface SlicerChoiceProps {
  value: SlicerKind | null;
  onChange: (next: SlicerKind) => void;
  disabled?: boolean;
  catalogueOnly?: boolean;
}

const OPTIONS: { kind: SlicerKind; label: string }[] = [
  { kind: 'orcaslicer', label: 'OrcaSlicer' },
  { kind: 'bambu_studio', label: 'BambuStudio' },
];

export function SlicerChoice({ value, onChange, disabled, catalogueOnly }: SlicerChoiceProps) {
  const { t } = useTranslation();
  return (
    <fieldset>
      <legend className="text-xs text-bambu-gray mb-2">
        {catalogueOnly ? t('slicingSetup.presetSlicer') : t('slice.sidecarPicker', 'Slice with')}
      </legend>
      <div className="grid grid-cols-2 gap-3">
        {OPTIONS.map((opt) => catalogueOnly ? (
          <button
            key={opt.kind}
            type="button"
            disabled={disabled}
            aria-pressed={value === opt.kind}
            onClick={() => onChange(opt.kind)}
            className={`rounded-lg border px-3 py-2 text-left text-sm transition-colors disabled:opacity-50 ${value === opt.kind
              ? 'border-bambu-green bg-bambu-green/10 text-white'
              : 'border-bambu-dark-tertiary bg-bambu-dark text-bambu-gray hover:text-white'}`}
          >
            {opt.label}
          </button>
        ) : (
          <SlicerCard
            key={opt.kind}
            kind={opt.kind}
            label={opt.label}
            selected={value === opt.kind}
            outerDisabled={Boolean(disabled)}
            onSelect={() => onChange(opt.kind)}
          />
        ))}
      </div>
      {!catalogueOnly && <SlicerOutdatedNote kind={value} />}
    </fieldset>
  );
}

interface SlicerCardProps {
  kind: SlicerKind;
  label: string;
  selected: boolean;
  outerDisabled: boolean;
  onSelect: () => void;
}

function SlicerCard({ kind, label, selected, outerDisabled, onSelect }: SlicerCardProps) {
  const { t } = useTranslation();
  const { data, isLoading } = useSlicerHealth(kind);
  const healthy = data?.healthy === true;
  const version = data?.version ?? null;
  const error = data?.error ?? null;
  // An offline sidecar cannot be picked: clicking it could not lead to a slice.
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
  const iconClass = selected ? 'text-bambu-green' : healthy ? 'text-bambu-gray' : 'text-red-700 dark:text-red-400';
  const titleClass = selected ? 'text-white' : 'text-bambu-gray';
  const descClass = selected ? 'text-bambu-gray' : 'text-bambu-gray/60';

  return (
    <button
      type="button"
      onClick={() => {
        if (!isDisabled) onSelect();
      }}
      disabled={isDisabled}
      aria-pressed={selected}
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
      {!isLoading && !healthy && error && <p className="text-xs text-red-400/80 mt-1 break-words">{error}</p>}
      {selected && healthy && (
        <div className="flex items-center gap-1 mt-2">
          <Check className="w-3 h-3 text-bambu-green" />
          <span className="text-xs text-bambu-green">{t('slicerHealth.active', 'active')}</span>
        </div>
      )}
    </button>
  );
}
