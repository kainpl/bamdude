interface ToggleProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  /** What this switch is for. Without it the control has no accessible name at
   *  all — a screen reader announces "switch, on" and nothing else, and a test
   *  has nothing to address it by. Optional because the callers that predate it
   *  do not pass one. */
  label?: string;
}

export function Toggle({ checked, onChange, disabled, label }: ToggleProps) {
  const handleClick = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (!disabled) {
      onChange(!checked);
    }
  };

  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={handleClick}
      className={`relative inline-flex w-11 h-7 md:w-9 md:h-5 rounded-full transition-colors flex-shrink-0 focus:outline-none focus:ring-2 focus:ring-bambu-green focus:ring-offset-2 focus:ring-offset-bambu-dark ${
        disabled
          ? 'bg-bambu-dark-tertiary/50 cursor-not-allowed opacity-50'
          : checked
          ? 'bg-bambu-green cursor-pointer'
          : 'bg-bambu-dark-tertiary cursor-pointer hover:bg-bambu-dark-tertiary/80'
      }`}
    >
      <span
        className={`pointer-events-none absolute top-[3px] md:top-[2px] left-[3px] md:left-[2px] w-5 h-5 md:w-4 md:h-4 bg-white rounded-full shadow transition-transform duration-200 ease-in-out ${
          checked ? 'translate-x-4' : 'translate-x-0'
        }`}
      />
    </button>
  );
}
