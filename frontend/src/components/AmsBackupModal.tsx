/**
 * AMS Filament Backup status modal (upstream Bambuddy #1762).
 *
 * Opens from the backup badge on the printer card. Shows the global toggle
 * and a BambuStudio-style ring graphic per backup pair — each ring is the
 * rotation order the firmware follows when the active slot runs out.
 *
 * On dual-extruder printers each ring carries a small "R" / "L" badge,
 * because the firmware cannot cross extruders even with the global backup
 * bit set.
 *
 * BamDude divergence: the toggle is a prop. Upstream drives it through the
 * dedicated ``/printers/{id}/ams-backup`` route it added alongside this
 * modal; we deliberately never ported that route (see audit row D1 of
 * 0.2.4.7-0.2.4.8) because our AMS settings dialog already writes the same
 * flag through ``POST /printers/{id}/ams/settings`` with
 * ``action: 'auto_switch_filament'``, and our state comes from the richer
 * ``PrinterState.ams_auto_switch_filament`` (cfg bit 18 AND home_flag bit
 * 10) rather than upstream's single field. The caller wires both.
 *
 * Theme-aware via CSS variables, matching AMSHistoryModal — adapts to every
 * background variant the user has picked.
 */
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { Modal } from './Modal';
import { Toggle } from './Toggle';
import type { BackupCompatibilityApplyResult } from '../api/client';
import {
  resolveBackupGroups,
  normalizeColor,
  type AmsUnitLike,
  type BackupGroup,
} from '../utils/amsHelpers';

interface AmsBackupModalProps {
  isOpen: boolean;
  state: boolean | null;
  amsUnits: AmsUnitLike[] | undefined;
  amsExtruderMap: Record<string, number> | undefined;
  firmwareGroups: Record<string, number[][]> | null | undefined;
  isDualNozzle: boolean;
  canToggle: boolean;
  pending: boolean;
  onToggle: (next: boolean) => void;
  onClose: () => void;
  /**
   * Bulk re-advertise of the slots the backup-compatibility policy covers.
   * Absent whenever the caller has nothing to offer here — the dialog is
   * complete without it.
   */
  compat?: {
    policyEnabled: boolean;
    canApply: boolean;
    onPreview: () => Promise<BackupCompatibilityApplyResult>;
    onApply: () => Promise<BackupCompatibilityApplyResult>;
  };
}

/**
 * Compact slot label like "A·3" / "HT·1" — the ring is small, every char
 * counts toward readability.
 */
function formatSlotLabel(amsId: number, slotIdx: number, totalTraysOnUnit: number): string {
  const isHt = totalTraysOnUnit === 1 || amsId >= 128;
  const normalizedId = amsId >= 128 ? amsId - 128 : amsId;
  const letter = String.fromCharCode(65 + normalizedId);
  return isHt ? `HT·${slotIdx + 1}` : `${letter}·${slotIdx + 1}`;
}

/** Pick a readable text colour for a given filament hex. */
function pickContrastTextColor(rgbaHex: string | null | undefined): string {
  const s = (rgbaHex || '').replace('#', '').slice(0, 6);
  if (s.length !== 6) return '#FFFFFF';
  const r = parseInt(s.slice(0, 2), 16);
  const g = parseInt(s.slice(2, 4), 16);
  const b = parseInt(s.slice(4, 6), 16);
  if ([r, g, b].some(Number.isNaN)) return '#FFFFFF';
  const luma = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
  return luma > 0.55 ? '#1A1A1A' : '#FFFFFF';
}

function BackupRing({
  group,
  trayCountByAms,
  innerBg,
  textPrimary,
  textSecondary,
  showExtruderBadge,
  extruderLabel,
}: {
  group: BackupGroup;
  trayCountByAms: Map<number, number>;
  innerBg: string;
  textPrimary: string;
  textSecondary: string;
  showExtruderBadge: boolean;
  extruderLabel: string;
}) {
  const filamentHex = normalizeColor(group.trayColor || undefined);
  const ringTextColor = pickContrastTextColor(group.trayColor);
  // The pill background that sits behind each slot label — keeps text legible
  // regardless of the filament fill colour.
  const labelPillBg = ringTextColor === '#FFFFFF' ? 'rgba(0,0,0,0.45)' : 'rgba(255,255,255,0.7)';
  const n = group.members.length;

  // Geometry: -100..100 viewport. Outer ring 92, inner cutout 56.
  // Slot labels sit on the colour band at radius 76.
  const labelRadius = 76;

  return (
    <div className="relative flex flex-col items-center">
      {showExtruderBadge && (
        <span
          className="absolute -top-1 -left-1 z-10 w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold shadow"
          style={{
            backgroundColor: textPrimary,
            color: innerBg,
          }}
          aria-label={extruderLabel}
          title={extruderLabel}
        >
          {extruderLabel}
        </span>
      )}
      <svg viewBox="-100 -100 200 200" className="w-44 h-44">
        {/* Subtle outer ring — gives a crisp edge on light AND dark themes. */}
        <circle cx="0" cy="0" r="95" fill="none" stroke={textSecondary} strokeOpacity="0.25" strokeWidth="1" />
        {/* Colour band */}
        <circle cx="0" cy="0" r="92" fill={filamentHex} />
        {/* Inner cutout */}
        <circle cx="0" cy="0" r="56" fill={innerBg} />
        {/* Inner ring border for definition between centre and colour band */}
        <circle cx="0" cy="0" r="56" fill="none" stroke={textSecondary} strokeOpacity="0.3" strokeWidth="1" />
        {/* Centre: material name */}
        <text
          x="0"
          y="-4"
          textAnchor="middle"
          dominantBaseline="middle"
          fontSize="14"
          fontWeight="700"
          fill={textPrimary}
        >
          {group.displayName || '—'}
        </text>
        {/* Centre: rotation count */}
        <text
          x="0"
          y="16"
          textAnchor="middle"
          dominantBaseline="middle"
          fontSize="11"
          fontWeight="500"
          fill={textSecondary}
        >
          {`${n}× ↻`}
        </text>
        {/* Slot labels around the ring, each on a pill for legibility */}
        {group.members.map((m, i) => {
          const angleDeg = (i * 360) / n - 90;
          const rad = (angleDeg * Math.PI) / 180;
          const x = labelRadius * Math.cos(rad);
          const y = labelRadius * Math.sin(rad);
          const label = formatSlotLabel(m.amsId, m.slotIdx, trayCountByAms.get(m.amsId) ?? 4);
          // Approximate pill width based on char count (each digit ≈ 6.5 px @ 12 px font).
          const pillWidth = Math.max(22, label.length * 7 + 8);
          return (
            <g key={`${m.amsId}-${m.slotIdx}`}>
              <rect
                x={x - pillWidth / 2}
                y={y - 9}
                width={pillWidth}
                height={18}
                rx={9}
                ry={9}
                fill={labelPillBg}
              />
              <text
                x={x}
                y={y}
                textAnchor="middle"
                dominantBaseline="middle"
                fontSize="12"
                fontWeight="700"
                fill={ringTextColor}
              >
                {label}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

export function AmsBackupModal({
  isOpen,
  state,
  amsUnits,
  amsExtruderMap,
  firmwareGroups,
  isDualNozzle,
  canToggle,
  pending,
  onToggle,
  onClose,
  compat,
}: AmsBackupModalProps) {
  const { t } = useTranslation();
  // Declared above the `isOpen` early return — rules of hooks.
  const [preview, setPreview] = useState<BackupCompatibilityApplyResult | null>(null);
  const [result, setResult] = useState<BackupCompatibilityApplyResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The dialog only stops RENDERING when it closes — the caller keeps it
  // mounted — so a preview left behind would greet the next visitor as if it
  // still described the slots, which have moved on since.
  useEffect(() => {
    if (!isOpen) {
      setPreview(null);
      setResult(null);
      setError(null);
    }
  }, [isOpen]);

  if (!isOpen) return null;

  // Theme-aware tokens, matching AMSHistoryModal.
  const modalBg = 'var(--bg-secondary)';
  const sectionBg = 'var(--bg-primary)';
  const borderColor = 'var(--border-color)';
  const textPrimary = 'var(--text-primary)';
  const textSecondary = 'var(--text-secondary)';

  // An explicit left-side AMS mapping or a firmware group for extruder 1 is
  // enough to label a one-AMS X2D correctly; waiting for a second AMS would
  // silently render that configuration as a single-nozzle printer.
  const effectiveDualNozzle = (() => {
    if (!isDualNozzle) return false;
    const distinctValues = new Set<number>();
    for (const ams of amsUnits || []) {
      const raw = amsExtruderMap?.[String(ams.id)];
      if (raw === undefined) continue;
      distinctValues.add(Number(raw));
    }
    const reportedLeft = Object.keys(firmwareGroups || {}).some((extruder) => Number(extruder) === 1);
    return distinctValues.size > 1 || distinctValues.has(1) || reportedLeft;
  })();

  const { groups, usesFallback } = resolveBackupGroups(
    amsUnits,
    amsExtruderMap,
    isDualNozzle,
    firmwareGroups,
  );
  const trayCountByAms = new Map<number, number>(
    (amsUnits || []).map((u) => [u.id, u.tray.length]),
  );

  // Only pairs are rendered — lone slots are deliberately suppressed.
  const pairs = groups.filter((g) => g.members.length >= 2);

  // "The firmware did not merge these slots" is only sayable once the printer
  // has actually told us its grouping. With no AMS reported at all there are no
  // slots to have merged, and `usesFallback` answers false for want of an
  // extruder to be missing a group for — so ask the payload directly.
  const firmwareReportedGroups = Object.keys(firmwareGroups || {}).length > 0;

  const isOn = state === true;
  const isUnknown = state === null;

  return (
    <Modal onClose={onClose} title={t('printers.amsBackup.modalTitle')} size="2xl" bodyClassName="flex flex-col">
      <div
        className="flex items-center justify-between px-5 py-3 border-b"
        style={{ borderColor, backgroundColor: sectionBg }}
      >
        <div className="min-w-0 mr-3">
          <div className="text-sm font-medium" style={{ color: textPrimary }}>
            {isUnknown
              ? t('printers.amsBackup.stateUnknown')
              : isOn
                ? t('printers.amsBackup.stateOn')
                : t('printers.amsBackup.stateOff')}
          </div>
          <p className="text-xs mt-0.5" style={{ color: textSecondary }}>
            {t('printers.amsBackup.modalHelp')}
          </p>
        </div>
        <Toggle
          checked={isOn}
          onChange={onToggle}
          disabled={!canToggle || isUnknown || pending}
        />
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-6">
        <p className="text-xs text-center mb-5" style={{ color: textSecondary }}>
          {usesFallback
            ? t('printers.amsBackup.firmwareEstimate')
            : t('printers.amsBackup.firmwareReported')}
        </p>
        {pairs.length === 0 ? (
          <p
            className="text-sm text-center py-8"
            style={{ color: textSecondary }}
          >
            {t('printers.amsBackup.modalNoPairs')}
          </p>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-6 justify-items-center">
            {pairs.map((g) => (
              <BackupRing
                key={g.key}
                group={g}
                trayCountByAms={trayCountByAms}
                innerBg={modalBg}
                textPrimary={textPrimary}
                textSecondary={textSecondary}
                showExtruderBadge={effectiveDualNozzle}
                extruderLabel={
                  g.extruder === 0
                    ? t('printers.amsBackup.extruderRightShort')
                    : t('printers.amsBackup.extruderLeftShort')
                }
              />
            ))}
          </div>
        )}

        {compat?.policyEnabled && (
          <div className="mt-6 border-t pt-4" style={{ borderColor }}>
            {pairs.length === 0 && !usesFallback && firmwareReportedGroups && (
              <p className="text-xs mb-3" style={{ color: textSecondary }}>{t('printers.amsCompat.firmwareDidNotMerge')}</p>
            )}
            <div className="flex items-center justify-between gap-3">
              <p className="text-xs" style={{ color: textSecondary }}>{t('printers.amsCompat.applyIntro')}</p>
              <button
                type="button"
                disabled={!compat.canApply || busy}
                className="px-3 py-1.5 text-sm rounded-lg bg-bambu-dark-tertiary text-white disabled:opacity-50 shrink-0"
                onClick={async () => {
                  setBusy(true); setError(null); setResult(null);
                  try { setPreview(await compat.onPreview()); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
                }}
              >
                {t('printers.amsCompat.applyButton')}
              </button>
            </div>
            {error && <p className="text-xs text-red-400 mt-2" role="alert">{error}</p>}
            {preview && (
              <div className="mt-3 space-y-1">
                {preview.rows.map((r) => (
                  <div key={`${r.ams_id}-${r.tray_id}`} className="flex items-center gap-2 text-xs" style={{ color: textPrimary }}>
                    <span className="w-8 font-mono">{r.slot}</span>
                    <span className="flex-1 truncate">{r.spool}</span>
                    <span className="inline-block w-3 h-3 rounded-full border shrink-0" style={{ backgroundColor: `#${r.actual.tray_color.slice(0, 6)}` }} />
                    <span>{r.actual.tray_info_idx}</span>
                    <span style={{ color: textSecondary }}>→</span>
                    <span className="inline-block w-3 h-3 rounded-full border shrink-0" style={{ backgroundColor: `#${r.advertised.tray_color.slice(0, 6)}` }} />
                    <span>{r.advertised.tray_info_idx}</span>
                    <span style={{ color: textSecondary }}>
                      {r.action === 'apply'
                        ? t('printers.amsCompat.rowApply')
                        : r.action === 'revert'
                          ? t('printers.amsCompat.rowRevert')
                          : r.reasons.map((x) => t(`printers.amsCompat.reason.${x}`)).join(', ')}
                    </span>
                  </div>
                ))}
                {!result && (
                  <button
                    type="button"
                    disabled={busy || (preview.would_apply ?? 0) === 0}
                    className="mt-2 px-3 py-1.5 text-sm rounded-lg bg-bambu-green text-white disabled:opacity-50"
                    onClick={async () => {
                      setBusy(true); setError(null);
                      try { setResult(await compat.onApply()); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
                    }}
                  >
                    {t('printers.amsCompat.confirmButton', { count: preview.would_apply ?? 0 })}
                  </button>
                )}
                {result && <p className="text-xs mt-2" role="status" style={{ color: textPrimary }}>{t('printers.amsCompat.applied', { count: result.applied })}</p>}
              </div>
            )}
          </div>
        )}
      </div>
    </Modal>
  );
}
