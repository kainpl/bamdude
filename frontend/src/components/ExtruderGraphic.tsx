import leftActiveEmpty from '../assets/printer/left_extruder_active_empty.svg';
import leftActiveFilled from '../assets/printer/left_extruder_active_filled.svg';
import leftIdleEmpty from '../assets/printer/left_extruder_unactive_empty.svg';
import leftIdleFilled from '../assets/printer/left_extruder_unactive_filled.svg';
import rightActiveEmpty from '../assets/printer/right_extruder_active_empty.svg';
import rightActiveFilled from '../assets/printer/right_extruder_active_filled.svg';
import rightIdleEmpty from '../assets/printer/right_extruder_unactive_empty.svg';
import rightIdleFilled from '../assets/printer/right_extruder_unactive_filled.svg';
import singleEmpty from '../assets/printer/monitor_extruder_empty_load.png';
import singleFilled from '../assets/printer/monitor_extruder_filled_load.png';

/**
 * The extruder as BambuStudio draws it, using Studio's own artwork.
 *
 * The assets are copied verbatim from `resources/images/` in the mirrored
 * BambuStudio checkout — both projects are AGPL-3.0, and BamDude already ships
 * Studio's printer configs and calibration models the same way.
 *
 * ⚠️ **Each of the four states means something, so none of them is decoration.**
 * Comparing the files: `filled` differs from `empty` by a single green ellipse —
 * the filament indicator — and `unactive` is the whole drawing wrapped in
 * `opacity: 0.4`. So:
 *
 *   active / unactive  <- which extruder is selected
 *   filled / empty     <- whether that extruder actually has filament
 *
 * The second one is read from the printer (`ext_has_filament`, the same bit the
 * AMS firmware guard uses), not assumed. A picture that always shows filament
 * would be a small lie told very often.
 */

const DUAL = {
  left: { activeFilled: leftActiveFilled, activeEmpty: leftActiveEmpty, idleFilled: leftIdleFilled, idleEmpty: leftIdleEmpty },
  right: { activeFilled: rightActiveFilled, activeEmpty: rightActiveEmpty, idleFilled: rightIdleFilled, idleEmpty: rightIdleEmpty },
};

function pick(side: 'left' | 'right', active: boolean, filled: boolean): string {
  const set = DUAL[side];
  if (active) return filled ? set.activeFilled : set.activeEmpty;
  return filled ? set.idleFilled : set.idleEmpty;
}

interface Props {
  isDualNozzle: boolean;
  /** 0 = main / right, 1 = deputy / left — the same indices the API uses. */
  selected: number;
  hasFilament: Record<number, boolean>;
  className?: string;
}

export function ExtruderGraphic({ isDualNozzle, selected, hasFilament, className = '' }: Props) {
  if (!isDualNozzle) {
    return (
      <img
        src={hasFilament[0] ? singleFilled : singleEmpty}
        alt=""
        aria-hidden="true"
        className={`h-[62px] w-auto ${className}`}
      />
    );
  }

  // ⚠️ Extruder 1 is the LEFT one and 0 is the right — the same mapping the
  // rest of the app uses for `active_extruder`, and the reason the images are
  // not simply laid out in index order.
  return (
    <div className={`flex items-end ${className}`} aria-hidden="true">
      <img src={pick('left', selected === 1, !!hasFilament[1])} alt="" className="h-[62px] w-auto" />
      <img src={pick('right', selected === 0, !!hasFilament[0])} alt="" className="h-[62px] w-auto" />
    </div>
  );
}
