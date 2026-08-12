import { Droplets, Gauge, Thermometer, Wind } from 'lucide-react';

// Not a component file: no JSX here, only a lookup that hands back a lucide
// component. Two places draw readings — the group headers and the sidebar
// popover — and a second copy of this map is how one of them would quietly
// stop recognising a quantity the other had learned.
const ICONS: Record<string, typeof Thermometer> = {
  temperature: Thermometer,
  humidity: Droplets,
  co2: Wind,
  pm25: Wind,
};

/** The icon for a quantity, with a fallback.
 *
 * The FALLBACK is the point: a quantity added to `measurements.py` later
 * appears with a default icon instead of vanishing from the display. */
export function iconFor(kind: string): typeof Thermometer {
  return ICONS[kind] ?? Gauge;
}
