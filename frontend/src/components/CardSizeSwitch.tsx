import { useTranslation } from 'react-i18next';
import { CARD_SIZE_LABELS } from '../utils/cardSize';

const TITLE_KEYS = {
  cards: [
    'printers.cardSize.small',
    'printers.cardSize.medium',
    'printers.cardSize.large',
    'printers.cardSize.extraLarge',
  ],
  tiles: [
    'printers.camWall.tileSize.small',
    'printers.camWall.tileSize.medium',
    'printers.camWall.tileSize.large',
    'printers.camWall.tileSize.extraLarge',
  ],
} as const;

const GROUP_LABEL_KEYS = {
  cards: 'printers.cardSize.groupLabel',
  tiles: 'printers.camWall.tileSize.groupLabel',
} as const;

interface CardSizeSwitchProps {
  value: number;
  onChange: (size: number) => void;
  /** Greyed and inert — the page is in a view the size does not apply to. */
  disabled?: boolean;
  /** Inside an overflow popover: stretch to the popover's width. */
  fullWidth?: boolean;
  /** What the sizes are sizes OF — only the wording changes: the Cam Wall
   *  sizes its tiles with the same control. */
  kind?: 'cards' | 'tiles';
}

/**
 * The S · M · L · XL segmented control. One component for the Printers page
 * and the Queue page so the two never drift apart in look or behaviour; what
 * each size means (grid columns, card anatomy) stays with the page.
 */
export function CardSizeSwitch({ value, onChange, disabled = false, fullWidth = false, kind = 'cards' }: CardSizeSwitchProps) {
  const { t } = useTranslation();
  return (
    <div
      className={`flex h-8 items-center bg-bambu-dark rounded-lg border border-bambu-dark-tertiary ${fullWidth ? 'w-full' : ''}`}
      role="group"
      aria-label={t(GROUP_LABEL_KEYS[kind])}
    >
      {CARD_SIZE_LABELS.map((label, index) => {
        const size = index + 1;
        const isSelected = value === size;
        return (
          <button
            key={label}
            type="button"
            disabled={disabled}
            onClick={() => onChange(size)}
            aria-pressed={isSelected}
            className={`h-full px-2 text-xs font-medium transition-colors focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-white disabled:opacity-40 disabled:cursor-not-allowed ${fullWidth ? 'flex-1' : ''} ${
              index === 0 ? 'rounded-l-lg' : ''
            } ${
              index === CARD_SIZE_LABELS.length - 1 ? 'rounded-r-lg' : ''
            } ${
              isSelected
                ? 'bg-bambu-green text-white'
                : 'text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary'
            }`}
            title={t(TITLE_KEYS[kind][index])}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}
