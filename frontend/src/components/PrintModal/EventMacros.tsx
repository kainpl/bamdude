import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Zap, ChevronDown, ChevronUp } from 'lucide-react';
import type { Macro } from '../../api/client';

export interface EventMacrosPanelProps {
  /** Already filtered to this printer model, enabled, non-swap. */
  macros: Macro[];
  selectedIds: number[];
  onChange: (next: number[]) => void;
}

/**
 * Which macros run for this print.
 *
 * Opt-in: a macro fires only where it is ticked here, so a print started
 * outside BamDude — printer screen, Telegram, virtual printer — runs none.
 */
export function EventMacrosPanel({ macros, selectedIds, onChange }: EventMacrosPanelProps) {
  const { t } = useTranslation();
  const [isExpanded, setIsExpanded] = useState(false);

  // No applicable macro means no panel. An empty box is a question the
  // operator has to answer ("is something broken?") for no reason.
  if (macros.length === 0) return null;

  const toggle = (id: number) => {
    onChange(selectedIds.includes(id) ? selectedIds.filter(x => x !== id) : [...selectedIds, id]);
  };

  return (
    <div className="mb-4">
      <button
        type="button"
        onClick={() => setIsExpanded(!isExpanded)}
        className="flex items-center gap-2 text-sm text-bambu-gray hover:text-white transition-colors w-full"
      >
        <Zap className="w-4 h-4" />
        <span>{t('printModal.eventMacros')}</span>
        <span className="text-xs text-bambu-gray/70">
          {t('printModal.eventMacrosCount', { selected: selectedIds.length, total: macros.length })}
        </span>
        {isExpanded ? <ChevronUp className="w-4 h-4 ml-auto" /> : <ChevronDown className="w-4 h-4 ml-auto" />}
      </button>
      {isExpanded && (
        <div className="mt-2 bg-bambu-dark rounded-lg p-3 space-y-2">
          {macros.map(m => (
            <label key={m.id} className="flex items-center justify-between cursor-pointer group">
              <span className="text-sm text-white">
                {m.name}
                <span className="ml-2 text-xs text-bambu-gray">
                  {t(`settings.macroEvents.${m.event}`, { defaultValue: m.event })}
                  {m.trigger_layer != null && ` · ${t('settings.macroTriggerLayer')} ${m.trigger_layer}`}
                  {m.mqtt_action_param && ` · ${m.mqtt_action_param}`}
                </span>
              </span>
              <input
                type="checkbox"
                aria-label={m.name}
                checked={selectedIds.includes(m.id)}
                onChange={() => toggle(m.id)}
                className="accent-bambu-green"
              />
            </label>
          ))}
        </div>
      )}
    </div>
  );
}
