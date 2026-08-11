import homeGlyph from '../assets/printer/monitor_axis_home.svg';

/**
 * BambuStudio's round X/Y pad.
 *
 * ⚠️ **Drawn, not an image.** Studio's `AxisCtrlButton` is a `wxPanel` with a
 * custom `paintEvent` — there is no joystick bitmap in its resources to take, so
 * copying the look means reproducing the geometry. The house in the middle IS
 * one of its bitmaps (`monitor_axis_home.svg`), so that one is the real asset.
 *
 * The geometry is two concentric rings cut into four quadrants, each pointing
 * the way it moves: the outer ring is the 10 mm step, the inner ring is 1 mm.
 *
 * ⚠️ **The X between the quadrants is the backing disc showing through**, which
 * is why that disc is the dark colour and not a tint of the sectors. Filling the
 * diagonals with their own shapes in the same colour was tried and is worse: the
 * diagonal becomes a flat blot and the two rings merge into one disc.
 */

// SVG user units. The component scales to whatever box it is given.
const SIZE = 200;
const C = SIZE / 2;
const R_OUTER = 96;
const R_MID = 60;
const R_HUB = 30;
// Half-width of the divider, in degrees, taken off each quadrant edge. Thin on
// purpose: it reads as the X BambuStudio draws. A wide one was tried and made
// the diagonals a flat blot with no structure left in them.
const WEDGE = 3;

type Dir = 'up' | 'down' | 'left' | 'right';

// SVG angles run clockwise from east, so "up" is -90.
const CENTRE_ANGLE: Record<Dir, number> = { right: 0, down: 90, left: 180, up: -90 };

function polar(angleDeg: number, radius: number): [number, number] {
  const a = (angleDeg * Math.PI) / 180;
  return [C + radius * Math.cos(a), C + radius * Math.sin(a)];
}

/** A closed annular sector between two angles and two radii. */
function ring(a1: number, a2: number, rInner: number, rOuter: number): string {
  const [x1, y1] = polar(a1, rInner);
  const [x2, y2] = polar(a1, rOuter);
  const [x3, y3] = polar(a2, rOuter);
  const [x4, y4] = polar(a2, rInner);
  return [
    `M ${x1} ${y1}`,
    `L ${x2} ${y2}`,
    `A ${rOuter} ${rOuter} 0 0 1 ${x3} ${y3}`,
    `L ${x4} ${y4}`,
    `A ${rInner} ${rInner} 0 0 0 ${x1} ${y1}`,
    'Z',
  ].join(' ');
}

const quadrant = (dir: Dir, rInner: number, rOuter: number) =>
  ring(CENTRE_ANGLE[dir] - 45 + WEDGE, CENTRE_ANGLE[dir] + 45 - WEDGE, rInner, rOuter);

export interface AxisJoystickProps {
  /** Called with the axis and the signed millimetres BambuStudio would send. */
  onMove: (axis: 'x' | 'y', distance: number) => void;
  onHome: () => void;
  disabled?: boolean;
  /** Blocks the eight direction sectors but leaves Home reachable — being
   *  unhomed is precisely the thing Home fixes. */
  movesDisabled?: boolean;
  homeLabel: string;
  className?: string;
}

const SECTORS: { dir: Dir; axis: 'x' | 'y'; step: number; ring: 'outer' | 'inner' }[] = [
  { dir: 'up', axis: 'y', step: 10, ring: 'outer' },
  { dir: 'down', axis: 'y', step: -10, ring: 'outer' },
  { dir: 'left', axis: 'x', step: -10, ring: 'outer' },
  { dir: 'right', axis: 'x', step: 10, ring: 'outer' },
  { dir: 'up', axis: 'y', step: 1, ring: 'inner' },
  { dir: 'down', axis: 'y', step: -1, ring: 'inner' },
  { dir: 'left', axis: 'x', step: -1, ring: 'inner' },
  { dir: 'right', axis: 'x', step: 1, ring: 'inner' },
];

const AXIS_LABELS: { dir: Dir; text: string }[] = [
  { dir: 'up', text: 'Y' },
  { dir: 'down', text: '-Y' },
  { dir: 'left', text: '-X' },
  { dir: 'right', text: 'X' },
];

// Step sizes ride the diagonals: the upper-right one carries the positives,
// the lower-left one the negatives, outer ring first.
const STEP_LABELS = [
  { angle: -45, radius: (R_MID + R_OUTER) / 2, text: '+10' },
  { angle: -45, radius: (R_HUB + R_MID) / 2 + 3, text: '+1' },
  { angle: 135, radius: (R_HUB + R_MID) / 2 + 3, text: '-1' },
  { angle: 135, radius: (R_MID + R_OUTER) / 2, text: '-10' },
];

export function AxisJoystick({
  onMove,
  onHome,
  disabled = false,
  movesDisabled = false,
  homeLabel,
  className = '',
}: AxisJoystickProps) {
  const blocked = disabled || movesDisabled;
  const sectorFill = blocked
    ? 'fill-bambu-dark-tertiary/50 cursor-not-allowed'
    : 'fill-bambu-dark-tertiary hover:fill-bambu-green/40 cursor-pointer transition-colors';

  return (
    <svg viewBox={`0 0 ${SIZE} ${SIZE}`} className={`select-none ${className}`} role="group">
      <circle cx={C} cy={C} r={R_OUTER} className="fill-bambu-dark-secondary" />

      {SECTORS.map(({ dir, axis, step, ring: which }) => {
        const [rInner, rOuter] = which === 'outer' ? [R_MID, R_OUTER] : [R_HUB, R_MID];
        return (
          <path
            key={`${dir}-${which}`}
            d={quadrant(dir, rInner, rOuter)}
            onClick={() => !blocked && onMove(axis, step)}
            aria-label={`${axis.toUpperCase()} ${step > 0 ? '+' : ''}${step}`}
            role="button"
            aria-disabled={blocked}
            className={sectorFill}
          />
        );
      })}

      {/* The boundary between the 10 mm ring and the 1 mm ring. Neither version
          before this drew it, and without it the two rings read as one disc —
          the numbers are then the only thing saying where the step changes. */}
      <circle
        cx={C}
        cy={C}
        r={R_MID}
        fill="none"
        strokeWidth={3}
        className="stroke-bambu-dark-secondary"
        pointerEvents="none"
      />

      {AXIS_LABELS.map(({ dir, text }) => {
        const [x, y] = polar(CENTRE_ANGLE[dir], (R_MID + R_OUTER) / 2);
        return (
          <text
            key={text}
            x={x}
            y={y}
            textAnchor="middle"
            dominantBaseline="central"
            className={`text-[15px] font-semibold ${blocked ? 'fill-bambu-gray/40' : 'fill-white'}`}
            pointerEvents="none"
          >
            {text}
          </text>
        );
      })}

      {STEP_LABELS.map(({ angle, radius, text }) => {
        const [x, y] = polar(angle, radius);
        return (
          <text
            key={text}
            x={x}
            y={y}
            textAnchor="middle"
            dominantBaseline="central"
            transform={`rotate(${angle + 90} ${x} ${y})`}
            className="text-[10px] fill-bambu-gray"
            pointerEvents="none"
          >
            {text}
          </text>
        );
      })}

      {/* Home. Deliberately NOT gated on `movesDisabled`: the reason the moves
          are blocked is that the printer needs homing, and this is the button
          that does it. */}
      <circle
        cx={C}
        cy={C}
        r={R_HUB}
        onClick={() => !disabled && onHome()}
        aria-label={homeLabel}
        role="button"
        aria-disabled={disabled}
        className={
          disabled
            ? 'fill-bambu-dark-tertiary/50 cursor-not-allowed'
            : 'fill-bambu-dark-tertiary hover:fill-bambu-green/40 cursor-pointer transition-colors'
        }
      />
      <image
        href={homeGlyph}
        x={C - 16}
        y={C - 16}
        width={32}
        height={32}
        opacity={disabled ? 0.4 : 1}
        pointerEvents="none"
      />
    </svg>
  );
}
