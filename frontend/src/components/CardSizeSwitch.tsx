import { useTranslation } from 'react-i18next';
import { CARD_SIZE_LABELS } from '../utils/cardSize';

const TITLE_KEYS = [
  'printers.cardSize.small',
  'printers.cardSize.medium',
  'printers.cardSize.large',
  'printers.cardSize.extraLarge',
] as const;

interface CardSizeSwitchProps {
  value: number;
  onChange: (size: number) => void;
  /** Greyed and inert — the page is in a view the size does not apply to. */
  disabled?: boolean;
  /** Inside an overflow popover: stretch to the popover's width. */
  fullWidth?: boolean;
}

/**
 * The S · M · L · XL segmented control. One component for the Printers page
 * and the Queue page so the two never drift apart in look or behaviour; what
 * each size means (grid columns, card anatomy) stays with the page.
 */
export function CardSizeSwitch({ value, onChange, disabled = false, fullWidth = false }: CardSizeSwitchProps) {
  const { t } = useTranslation();
  return (
    <div
      className={`flex h-8 items-center bg-bambu-dark rounded-lg border border-bambu-dark-tertiary ${disabled ? 'opacity-40 pointer-events-none' : ''} ${fullWidth ? 'w-full' : ''}`}
      role="group"
      aria-label={t('printers.cardSize.groupLabel')}
    >
      {CARD_SIZE_LABELS.map((label, index) => {
        const size = index + 1;
        const isSelected = value === size;
        return (
          <button
            key={label}
            type="button"
            onClick={() => onChange(size)}
            aria-pressed={isSelected}
            className={`h-full px-2 text-xs font-medium transition-colors ${fullWidth ? 'flex-1' : ''} ${
              index === 0 ? 'rounded-l-lg' : ''
            } ${
              index === CARD_SIZE_LABELS.length - 1 ? 'rounded-r-lg' : ''
            } ${
              isSelected
                ? 'bg-bambu-green text-white'
                : 'text-white hover:bg-bambu-dark-tertiary'
            }`}
            title={t(TITLE_KEYS[index])}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}
