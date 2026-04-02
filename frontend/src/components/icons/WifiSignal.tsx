interface WifiSignalProps {
  signal: number | null | undefined;  // dBm value
  className?: string;
}

/**
 * WiFi signal icon with 4 bars that fill based on signal strength.
 * - 4 bars: >= -50 dBm (excellent)
 * - 3 bars: >= -60 dBm (good)
 * - 2 bars: >= -70 dBm (fair)
 * - 1 bar:  < -70 dBm (weak)
 * - 0 bars: no signal data
 */
export function WifiSignal({ signal, className = "w-4 h-4" }: WifiSignalProps) {
  let bars = 0;
  if (signal != null) {
    if (signal >= -50) bars = 4;
    else if (signal >= -60) bars = 3;
    else if (signal >= -70) bars = 2;
    else bars = 1;
  }

  const activeColor = "#00ae42";  // bambu-green
  const inactiveColor = "#4a4a4a";  // dark gray

  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
    >
      {/* Dot at bottom */}
      <circle
        cx="12"
        cy="20"
        r="1"
        fill={bars >= 1 ? activeColor : inactiveColor}
        stroke={bars >= 1 ? activeColor : inactiveColor}
      />
      {/* First arc (smallest) */}
      <path
        d="M8.5 16.5a5 5 0 0 1 7 0"
        stroke={bars >= 2 ? activeColor : inactiveColor}
        fill="none"
      />
      {/* Second arc */}
      <path
        d="M5 13a10 10 0 0 1 14 0"
        stroke={bars >= 3 ? activeColor : inactiveColor}
        fill="none"
      />
      {/* Third arc (largest) */}
      <path
        d="M1.5 9.5a15 15 0 0 1 21 0"
        stroke={bars >= 4 ? activeColor : inactiveColor}
        fill="none"
      />
    </svg>
  );
}
