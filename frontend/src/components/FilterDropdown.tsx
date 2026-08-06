import { useState } from 'react';
import { ChevronDown } from 'lucide-react';

interface FilterDropdownOption {
  value: string;
  label: string;
  count?: number;
}

interface FilterDropdownProps {
  label: string;
  value: string;
  options: FilterDropdownOption[];
  onChange: (value: string) => void;
}

/**
 * Labelled single-select filter dropdown used above filterable lists.
 *
 * Lives here rather than beside its first caller: it is shared between
 * ProfilesPage and OrcaCloudProfilesView, and exporting it from the page
 * closed an import cycle (component -> page -> component).
 */
export function FilterDropdown({
  label,
  value,
  options,
  onChange,
}: FilterDropdownProps) {
  const [isOpen, setIsOpen] = useState(false);
  const selectedOption = options.find(o => o.value === value);

  return (
    <div className="relative">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-2 px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-sm text-white hover:border-bambu-gray-dark transition-colors"
      >
        <span className="text-bambu-gray">{label}:</span>
        <span>{selectedOption?.label || 'All'}</span>
        <ChevronDown className={`w-4 h-4 text-bambu-gray transition-transform ${isOpen ? 'rotate-180' : ''}`} />
      </button>

      {isOpen && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setIsOpen(false)} />
          <div className="absolute top-full left-0 mt-1 min-w-[160px] bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl z-20 py-1 max-h-60 overflow-y-auto">
            {options.map((option) => (
              <button
                key={option.value}
                onClick={() => { onChange(option.value); setIsOpen(false); }}
                className={`w-full px-3 py-2 text-left text-sm flex items-center justify-between hover:bg-bambu-dark-tertiary transition-colors ${
                  value === option.value ? 'text-bambu-green' : 'text-white'
                }`}
              >
                <span>{option.label}</span>
                {option.count !== undefined && (
                  <span className="text-bambu-gray text-xs">{option.count}</span>
                )}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
