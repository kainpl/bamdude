import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Repeat, ChevronDown, ChevronUp } from 'lucide-react';
import { SWAP_MACRO_EVENTS, type SwapMacroEvent, type SwapMacrosPanelProps } from './types';

const EVENT_LABEL_KEYS: Record<SwapMacroEvent, { label: string; desc: string }> = {
  swap_mode_start: {
    label: 'printModal.swapEventStart',
    desc: 'printModal.swapEventStartDesc',
  },
  swap_mode_change_table: {
    label: 'printModal.swapEventChangeTable',
    desc: 'printModal.swapEventChangeTableDesc',
  },
};

export function SwapMacrosPanel({ options, onChange }: SwapMacrosPanelProps) {
  const { t } = useTranslation();
  const [isExpanded, setIsExpanded] = useState(false);

  const toggleMaster = () => {
    const nextExecute = !options.execute;
    onChange({
      execute: nextExecute,
      // Re-hydrate the full event list when switching back on so users don't
      // get left in a "execute=true, events=[]" dead state if they previously
      // unchecked everything.
      events: nextExecute && options.events.length === 0 ? [...SWAP_MACRO_EVENTS] : options.events,
    });
  };

  const toggleEvent = (event: SwapMacroEvent) => {
    const isOn = options.events.includes(event);
    const nextEvents = isOn ? options.events.filter(e => e !== event) : [...options.events, event];
    // Removing the last event also clears the master toggle — matches the
    // operator mental model "no events = feature off".
    onChange({
      execute: nextEvents.length > 0 && options.execute,
      events: nextEvents,
    });
  };

  return (
    <div className="mb-4">
      <button
        type="button"
        onClick={() => setIsExpanded(!isExpanded)}
        className="flex items-center gap-2 text-sm text-bambu-gray hover:text-white transition-colors w-full"
      >
        <Repeat className="w-4 h-4" />
        <span>{t('printModal.swapMacros')}</span>
        {isExpanded ? (
          <ChevronUp className="w-4 h-4 ml-auto" />
        ) : (
          <ChevronDown className="w-4 h-4 ml-auto" />
        )}
      </button>
      {isExpanded && (
        <div className="mt-2 bg-bambu-dark rounded-lg p-3 space-y-2">
          <label className="flex items-center justify-between cursor-pointer group">
            <div>
              <span className="text-sm text-white">{t('printModal.executeSwapMacros')}</span>
              <p className="text-xs text-bambu-gray">{t('printModal.executeSwapMacrosDesc')}</p>
            </div>
            <div
              className={`relative w-10 h-5 rounded-full transition-colors ${
                options.execute ? 'bg-bambu-green' : 'bg-bambu-dark-tertiary'
              }`}
              onClick={toggleMaster}
            >
              <div
                className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform ${
                  options.execute ? 'translate-x-5' : 'translate-x-0.5'
                }`}
              />
            </div>
          </label>
          <div className="pt-2 border-t border-bambu-dark-tertiary space-y-2">
            <p className="text-xs text-bambu-gray">{t('printModal.swapMacroEvents')}</p>
            {SWAP_MACRO_EVENTS.map(event => {
              const checked = options.events.includes(event);
              const { label, desc } = EVENT_LABEL_KEYS[event];
              return (
                <label key={event} className="flex items-center justify-between cursor-pointer group pl-2">
                  <div>
                    <span className={`text-sm ${options.execute ? 'text-white' : 'text-bambu-gray'}`}>
                      {t(label)}
                    </span>
                    <p className="text-xs text-bambu-gray">{t(desc)}</p>
                  </div>
                  <input
                    type="checkbox"
                    checked={checked}
                    disabled={!options.execute}
                    onChange={() => toggleEvent(event)}
                    className="w-4 h-4 rounded accent-bambu-green disabled:opacity-40"
                  />
                </label>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
