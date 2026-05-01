import { useTranslation } from 'react-i18next';
import { CheckCircle2, Loader2, XCircle } from 'lucide-react';
import { useSlicerHealth, type SlicerKind } from '../hooks/useSlicerHealth';

const SLICER_LABELS: Record<SlicerKind, string> = {
  orcaslicer: 'OrcaSlicer',
  bambu_studio: 'BambuStudio',
};

interface CommonProps {
  slicer: SlicerKind;
  // Optional auto-refresh cadence. Default = no polling beyond React
  // Query's standard refetch-on-mount/focus; the SystemInfo widget passes
  // 30000 to keep it live next to the rest of that page.
  pollMs?: number;
}

interface DotProps extends CommonProps {
  variant: 'dot';
}

interface InlineProps extends CommonProps {
  variant: 'inline';
  // When true, render the slicer name even on offline/unknown state.
  // SettingsPage uses this so the user always sees which slicer the dot
  // refers to — SliceModal can omit it because the surrounding context
  // already names the slicer.
  showLabel?: boolean;
}

interface CardProps extends CommonProps {
  variant: 'card';
}

export type SlicerHealthIndicatorProps = DotProps | InlineProps | CardProps;

export function SlicerHealthIndicator(props: SlicerHealthIndicatorProps) {
  const { t } = useTranslation();
  const { data, isLoading, isError } = useSlicerHealth(props.slicer, props.pollMs);
  const label = SLICER_LABELS[props.slicer];
  const healthy = data?.healthy === true;
  const version = data?.version ?? null;
  const url = data?.url ?? null;
  // Backend returns `error` on unreachable; isError on the query itself
  // means the /health endpoint call itself blew up (auth / network).
  const error = data?.error ?? (isError ? 'request_failed' : null);

  const tooltip = isLoading
    ? t('slicerHealth.checking', { label, defaultValue: 'Checking {{label}}…' })
    : healthy
      ? t('slicerHealth.ready', {
          label,
          version: version ?? '?',
          defaultValue: '{{label}} ready (v{{version}})',
        })
      : t('slicerHealth.unreachable', {
          label,
          error: error ?? '?',
          defaultValue: '{{label}} unreachable: {{error}}',
        });

  if (props.variant === 'dot') {
    return (
      <span
        className="inline-flex items-center"
        title={tooltip}
        aria-label={tooltip}
      >
        {isLoading ? (
          <Loader2 className="w-3 h-3 animate-spin text-bambu-gray" />
        ) : (
          <span
            className={`w-2.5 h-2.5 rounded-full ${
              healthy ? 'bg-emerald-500' : 'bg-red-500'
            }`}
            aria-hidden
          />
        )}
      </span>
    );
  }

  if (props.variant === 'inline') {
    const Icon = isLoading ? Loader2 : healthy ? CheckCircle2 : XCircle;
    const tone = isLoading
      ? 'text-bambu-gray'
      : healthy
        ? 'text-emerald-400'
        : 'text-red-400';
    return (
      <span className={`inline-flex items-center gap-1.5 text-xs ${tone}`} title={tooltip}>
        <Icon className={`w-3.5 h-3.5 ${isLoading ? 'animate-spin' : ''}`} />
        {props.showLabel ? (
          <span className="text-white">{label}</span>
        ) : null}
        <span>
          {isLoading
            ? t('slicerHealth.statusChecking', 'checking…')
            : healthy
              ? t('slicerHealth.statusReady', {
                  version: version ?? '?',
                  defaultValue: 'ready · v{{version}}',
                })
              : t('slicerHealth.statusOffline', 'offline')}
        </span>
      </span>
    );
  }

  // variant === 'card'
  const cardTone = isLoading
    ? 'border-bambu-dark-tertiary'
    : healthy
      ? 'border-emerald-500/40'
      : 'border-red-500/40';
  return (
    <div
      className={`flex items-start gap-3 p-4 bg-bambu-dark rounded-lg border ${cardTone}`}
    >
      <div
        className={`p-2 rounded-lg bg-bambu-dark-tertiary ${
          isLoading
            ? 'text-bambu-gray'
            : healthy
              ? 'text-emerald-400'
              : 'text-red-400'
        }`}
      >
        {isLoading ? (
          <Loader2 className="w-5 h-5 animate-spin" />
        ) : healthy ? (
          <CheckCircle2 className="w-5 h-5" />
        ) : (
          <XCircle className="w-5 h-5" />
        )}
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-sm text-bambu-gray">{label}</p>
        <p className="text-lg font-semibold text-white truncate">
          {isLoading
            ? t('slicerHealth.statusChecking', 'checking…')
            : healthy
              ? t('slicerHealth.cardReady', {
                  version: version ?? '?',
                  defaultValue: 'Ready · v{{version}}',
                })
              : t('slicerHealth.cardOffline', 'Offline')}
        </p>
        {url && (
          <p className="text-xs text-bambu-gray mt-0.5 truncate" title={url}>
            {url}
          </p>
        )}
        {!isLoading && !healthy && error && (
          <p className="text-xs text-red-400 mt-1 break-words">{error}</p>
        )}
      </div>
    </div>
  );
}
