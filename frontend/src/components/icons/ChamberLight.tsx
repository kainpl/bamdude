interface ChamberLightProps {
  on: boolean;
  className?: string;
}

/**
 * Chamber light icon with on/off states.
 * Modern bulb design with radiating rays.
 * - On: Filled yellow bulb with visible rays
 * - Off: Outline only, muted color
 */
export function ChamberLight({ on, className = "w-5 h-5" }: ChamberLightProps) {
  const bulbFill = on ? "#facc15" : "none"; // yellow-400 when on
  const strokeColor = on ? "#78350f" : "currentColor"; // amber-900 when on
  const rayOpacity = on ? 1 : 0;

  return (
    <svg
      viewBox="0 0 32 32"
      fill="none"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
    >
      {/* Radiating rays */}
      <g stroke={strokeColor} opacity={rayOpacity}>
        <line x1="16" y1="2" x2="16" y2="6" />
        <line x1="6.1" y1="6.1" x2="8.9" y2="8.9" />
        <line x1="25.9" y1="6.1" x2="23.1" y2="8.9" />
        <line x1="2" y1="16" x2="6" y2="16" />
        <line x1="30" y1="16" x2="26" y2="16" />
      </g>

      {/* Bulb glass - smooth rounded shape */}
      <path
        d="M12 24v-2.3c0-.9-.4-1.7-1-2.3C9.2 17.6 8 15.4 8 13c0-4.4 3.6-8 8-8s8 3.6 8 8c0 2.4-1.2 4.6-3 6.4-.6.6-1 1.4-1 2.3V24"
        fill={bulbFill}
        stroke={strokeColor}
      />

      {/* Base rings */}
      <path d="M12 24h8" stroke={strokeColor} />
      <path d="M12 27h8" stroke={strokeColor} />
      <path d="M13 30h6" stroke={strokeColor} />
    </svg>
  );
}
