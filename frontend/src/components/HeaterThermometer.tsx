// Heater thermometer icon — filled + glowing when the heater is actively on,
// an empty outline when it's idle. Shared by the printer card and the Heater
// History window so both render the same live heater state.
export interface HeaterThermometerProps {
  className?: string;
  color: string; // The color class (e.g., "text-orange-400")
  isHeating: boolean;
}

export function HeaterThermometer({ className, color, isHeating }: HeaterThermometerProps) {
  // Extract the actual color from Tailwind class for SVG fill
  const colorMap: Record<string, string> = {
    'text-orange-400': '#fb923c',
    'text-blue-400': '#60a5fa',
    'text-green-400': '#4ade80',
  };
  const fillColor = colorMap[color] || '#888';

  // Glow style when heating
  const glowStyle = isHeating
    ? {
        filter: `drop-shadow(0 0 4px ${fillColor}) drop-shadow(0 0 8px ${fillColor})`,
      }
    : {};

  if (isHeating) {
    // Filled thermometer with glow - heater is ON
    return (
      <svg className={className} style={glowStyle} viewBox="0 0 12 20" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="4.5" y="3" width="3" height="9.5" fill={fillColor} rx="0.5" />
        <circle cx="6" cy="15" r="2" fill={fillColor} />
        <path
          d="M6 0.5C4.6 0.5 3.5 1.6 3.5 3V12.1C2.6 12.8 2 13.9 2 15C2 17.2 3.8 19 6 19C8.2 19 10 17.2 10 15C10 13.9 9.4 12.8 8.5 12.1V3C8.5 1.6 7.4 0.5 6 0.5Z"
          stroke={fillColor}
          strokeWidth="1"
          fill="none"
        />
      </svg>
    );
  }

  // Empty thermometer - heater is OFF
  return (
    <svg className={className} viewBox="0 0 12 20" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path
        d="M6 0.5C4.6 0.5 3.5 1.6 3.5 3V12.1C2.6 12.8 2 13.9 2 15C2 17.2 3.8 19 6 19C8.2 19 10 17.2 10 15C10 13.9 9.4 12.8 8.5 12.1V3C8.5 1.6 7.4 0.5 6 0.5Z"
        stroke={fillColor}
        strokeWidth="1"
        fill="none"
      />
      <circle cx="6" cy="15" r="2.5" stroke={fillColor} strokeWidth="1" fill="none" />
    </svg>
  );
}
