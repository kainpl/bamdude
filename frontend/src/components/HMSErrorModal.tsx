// HMS Error Modal.
//
// ⚠️ The descriptions used to live here — 853 entries from ha-bambulab, 118 KB
// of constant in a React component, the same text for every printer model. They
// now come from the API, per model, out of Bambu's own catalogue: see
// backend/app/data/hms/README.md for why the same code can mean two different
// things on two machines.
import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { X, AlertTriangle, AlertCircle, Info, ExternalLink, Loader2, Trash2, Eye, EyeOff } from 'lucide-react';
import type { HMSError, Permission } from '../api/client';
import { api } from '../api/client';
import { useToast } from '../contexts/ToastContext';

interface HMSErrorModalProps {
  printerName: string;
  errors: HMSError[];
  onClose: () => void;
  printerId: number;
  // The printer's serial number. Its first three characters select which
  // model's catalogue to describe these errors with — the same key the backend
  // uses for HMS actions.
  serialNumber?: string;
  hasPermission: (permission: Permission) => boolean;
  // Stack entries the operator hid on this printer (status.hms_muted). Kept
  // out of `errors` so badges and notifications stay quiet; listed here so
  // they can be un-hidden. Absent on an older backend.
  mutedErrors?: HMSError[];
  // Runout guidance for a PAUSED print (upstream #2587). When set, AMS-runout
  // errors are re-described to name the physical slot the firmware now expects,
  // instead of the generic "insert into the same slot" text (which is wrong under
  // AMS Filament Backup). Slot labels are pre-formatted (e.g. "AMS-A · Slot 3");
  // null when the slot could not be resolved → an honest "check the printer".
  runoutGuidance?: {
    expectedSlotLabel: string | null;
    ranOutSlotLabel: string | null;
  } | null;
}

// AMS per-slot filament-runout short codes (module 0x07). These pause the print
// waiting for a specific slot — the ones the runout guidance re-describes.
// Printer-side / external runout (0300_8004) has no AMS slot and is excluded.
const AMS_RUNOUT_SHORT_CODES = new Set([
  '0700_8011', '0701_8011', '0702_8011', '0703_8011', '0704_8011',
  '0705_8011', '0706_8011', '0707_8011', '07FF_8011',
]);

// "MQTT command verification failed" — the firmware's authorization check
// refusing a control command. Keyed by its full 16-char code on purpose: this
// error's meaning lives in attr's low half (0500) and code's high half (0001),
// both of which getShortCode() discards, so the short form is a useless
// "0500_0007". Before #2732 that meant filterKnownHMSErrors dropped it, and the
// user was never shown the one message explaining why nothing printed.
export const HMS_MQTT_VERIFY_FAILED = '0500050000010007';


function getSeverityInfo(severity: number): { label: string; color: string; bgColor: string; Icon: typeof AlertTriangle } {
  switch (severity) {
    case 1:
      return { label: 'Fatal', color: 'text-red-500', bgColor: 'bg-red-100 dark:bg-red-500/20', Icon: AlertTriangle };
    case 2:
      return { label: 'Serious', color: 'text-red-700 dark:text-red-400', bgColor: 'bg-red-100 dark:bg-red-500/15', Icon: AlertTriangle };
    case 3:
      return { label: 'Warning', color: 'text-orange-700 dark:text-orange-400', bgColor: 'bg-orange-100 dark:bg-orange-500/20', Icon: AlertCircle };
    case 4:
    default:
      return { label: 'Info', color: 'text-blue-700 dark:text-blue-400', bgColor: 'bg-blue-100 dark:bg-blue-500/20', Icon: Info };
  }
}

function getShortCode(attr: number, code: number): string {
  // Convert attr and code to short format: XXXX_YYYY
  // attr contains the module info, code contains the error number
  const module = ((attr >> 16) & 0xFFFF) || ((attr >> 8) & 0xFF) << 8 | (attr & 0xFF);
  const codeNum = code & 0xFFFF;
  return `${module.toString(16).padStart(4, '0').toUpperCase()}_${codeNum.toString(16).padStart(4, '0').toUpperCase()}`;
}

// Catalogue lookup against the map fetched for this printer's model.
//
// ⚠️ Full code first because it is lossless; the short code collapses
// information and can collide. And the backend's short keys carry NO
// separator, while ours are XXXX_YYYY — dropping that is the difference
// between matching 692 codes and matching none.
//
// Returns undefined for an uncatalogued error so callers can tell "no
// description" from "described as blank".
function lookupDescription(
  catalogue: Record<string, string> | undefined,
  fullCode: string | undefined,
  shortCode: string,
): string | undefined {
  if (!catalogue) return undefined;
  if (fullCode && catalogue[fullCode] !== undefined) return catalogue[fullCode];
  return catalogue[shortCode.replace('_', '')];
}

/**
 * `REMOVE_CLOSE_BTN` is not an action — it is a dialog modifier.
 *
 * BambuStudio's `DeviceErrorDialog.hpp` spells it out: `REMOVE_CLOSE_BTN = 39,
 * // special case, do not show close button`. BS scans the id list, sets a flag
 * when it sees 39, and hides the dialog's close affordance; it never renders a
 * button for it. We rendered one, labelled with the raw constant, that posted an
 * action the printer has no idea about.
 *
 * Kept in `error.actions` rather than stripped at the backend, because the
 * backend list is the catalogue verbatim and the "hide close" instruction is
 * real — it is only *this* surface that must not draw it.
 */
export const HMS_DIALOG_MODIFIER_ACTIONS = new Set(['REMOVE_CLOSE_BTN']);

export function renderableActions(actions: string[] | undefined): string[] {
  return (actions ?? []).filter((a) => !HMS_DIALOG_MODIFIER_ACTIONS.has(a));
}

/**
 * Every error the printer reports is shown.
 *
 * ⚠️ This used to keep an error only if the catalogue described it OR the
 * firmware supplied actions. The intent was to suppress transient noise after a
 * cancelled print; the effect was that any fault we could not name disappeared
 * and the printer card stayed green. Measured on a live X2D: the machine
 * refused to record a timelapse because the card was full, reported it over
 * MQTT, BambuStudio showed it on its Assistant tab, and BamDude showed nothing
 * anywhere.
 *
 * Kept as a function rather than deleted — nine call sites pass through it, and
 * a suppression rule, if one is ever justified, belongs in one place instead of
 * scattered across them. Any such rule must name a specific known-transient
 * code. "We have no text for this one" is not a reason to hide a fault.
 */
export function filterKnownHMSErrors(errors: HMSError[]): HMSError[] {
  return errors;
}

function getHMSHomeUrl(): string {
  return `https://wiki.bambulab.com/en/hms/home`;
}

export function HMSErrorModal({ printerName, errors, onClose, printerId, serialNumber, hasPermission, runoutGuidance, mutedErrors = [] }: HMSErrorModalProps) {
  const { t, i18n } = useTranslation();

  // ⚠️ Static per model and language, and megabytes on the wire — never let
  // this refetch on a re-render or on every modal open.
  const devicePrefix = (serialNumber ?? '').slice(0, 3).toUpperCase();
  const { data: catalogue, isLoading: catalogueLoading } = useQuery({
    queryKey: ['hms-descriptions', devicePrefix, i18n.language],
    queryFn: () => api.getHMSDescriptions(devicePrefix, i18n.language),
    staleTime: Infinity,
    gcTime: Infinity,
    enabled: devicePrefix.length === 3,
  });
  const descriptions = catalogue?.descriptions;
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const clearMutation = useMutation({
    mutationFn: () => api.clearHMSErrors(printerId),
    onSuccess: () => {
      showToast(t('hmsErrors.clearSuccess'), 'success');
      onClose();
    },
    onError: () => {
      showToast(t('hmsErrors.clearFailed'), 'error');
    },
  });

  const activateActionMutation = useMutation({
    mutationFn: (data: { action: string; print_error: string; job_id: string | null }) =>
      api.executeHMSAction(printerId, data),
    onSuccess: () => {
      // Scope the invalidation to THIS printer — the prefix form would refresh every
      // printer card on the page when only one printer's state actually changed.
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printerId] });
      showToast(t('hmsErrors.actionSuccess', 'Action sent to printer'), 'success');
      onClose();
    },
    onError: (error: Error) => {
      showToast(`${t('hmsErrors.actionFailed', 'Failed to send action')}: ${error.message}`, 'error');
    },
  });

  // Hide / un-hide one stack entry on this printer until the printer drops it.
  // The firmware owns hms[] — Clear empties the print_error register and
  // nothing else — so an entry the printer keeps re-sending (a P2S code Bambu
  // ships with no text, 2026-09-04) could not be answered at all. The mute is
  // per printer and per FULL code, never per short code or "no description".
  const muteMutation = useMutation({
    mutationFn: (fullCode: string) => api.muteHMSError(printerId, fullCode),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printerId] });
      showToast(t('hmsErrors.hideSuccess'), 'success');
    },
    onError: (error: Error) => showToast(error.message || t('hmsErrors.hideFailed'), 'error'),
  });
  const unmuteMutation = useMutation({
    mutationFn: (fullCode: string) => api.unmuteHMSError(printerId, fullCode),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printerId] });
    },
    onError: (error: Error) => showToast(error.message || t('hmsErrors.unhideFailed'), 'error'),
  });
  const canHide = (error: HMSError) => error.full_code?.length === 16 && hasPermission('printers:control');

  // Surface cataloged errors and uncataloged-but-actionable errors. Mirrors
  // filterKnownHMSErrors so the modal and the badge counts agree by construction.
  const knownErrors = filterKnownHMSErrors(errors);

  // Close on Escape key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg shadow-xl max-w-lg w-full max-h-[80vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
          <div className="flex items-center gap-2">
            <AlertTriangle className="w-5 h-5 text-orange-600 dark:text-orange-400" />
            <h2 className="text-lg font-semibold text-white">{t('hmsErrors.title', { name: printerName })}</h2>
          </div>
          <button
            onClick={onClose}
            className="p-1 hover:bg-bambu-dark-tertiary rounded-lg transition-colors"
          >
            <X className="w-5 h-5 text-bambu-gray" />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-4">
          {knownErrors.length === 0 ? (
            <div className="text-center py-8 text-bambu-gray">
              <AlertCircle className="w-12 h-12 mx-auto mb-3 opacity-30" />
              <p>{t('hmsErrors.noErrors')}</p>
            </div>
          ) : (
            <div className="space-y-3">
              {knownErrors.map((error, index) => {
                const { label, color, bgColor, Icon } = getSeverityInfo(error.severity);
                const codeNum = parseInt(error.code.replace('0x', ''), 16) || 0;
                const shortCode = getShortCode(error.attr, codeNum);
                // Runout guidance (upstream #2587): for an AMS per-slot runout on
                // a paused print, name the slot the firmware now expects rather
                // than the misleading generic "insert into the same slot" text.
                // ⚠️ Ours counts as a match too. This decides whether the code
                // is shown in the four-group form the printer itself displays,
                // and HMS_MQTT_VERIFY_FAILED is described by US rather than by
                // the catalogue — asking the catalogue alone dropped it back to
                // a short form that means nothing.
                const matchedFullCode =
                  !!error.full_code &&
                  (error.full_code === HMS_MQTT_VERIFY_FAILED || descriptions?.[error.full_code] !== undefined);
                // ⚠️ Ours wins over Bambu's for this one: their wiki says
                // "update Studio or Handy", which is no help to somebody
                // printing from BamDude.
                let description =
                  error.full_code === HMS_MQTT_VERIFY_FAILED
                    ? t('hmsErrors.mqttVerifyFailedDescription')
                    : (lookupDescription(descriptions, error.full_code, shortCode) ??
                      // "Unknown" is a claim, not a placeholder: while the
                      // megabytes-large catalogue is still on the wire the
                      // honest text is "loading" — asserting unknown first
                      // made every first HMS of a session flash the wrong
                      // text and blink into the real one (live, 2026-08-25).
                      (catalogueLoading ? t('hmsErrors.descriptionLoading') : t('hmsErrors.unknownCode')));
                // The remedy is ours, not Bambu's — their wiki says "update
                // Studio or Handy", which is no help to someone printing from
                // BamDude. Same override shape as the runout guidance below.
                const remedy =
                  error.full_code === HMS_MQTT_VERIFY_FAILED ? t('hmsErrors.mqttVerifyFailedRemedy') : null;
                if (runoutGuidance && AMS_RUNOUT_SHORT_CODES.has(shortCode)) {
                  if (runoutGuidance.expectedSlotLabel && runoutGuidance.ranOutSlotLabel) {
                    description = t('hmsErrors.runoutExpectedSlot', {
                      expected: runoutGuidance.expectedSlotLabel,
                      ranOut: runoutGuidance.ranOutSlotLabel,
                    });
                  } else if (runoutGuidance.expectedSlotLabel) {
                    description = t('hmsErrors.runoutExpectedSlotOnly', {
                      expected: runoutGuidance.expectedSlotLabel,
                    });
                  } else {
                    description = t('hmsErrors.runoutSlotUnknown');
                  }
                }
                const hmsHomeUrl = getHMSHomeUrl();
                // An error matched on its full code is shown in the four-group
                // form the printer's own screen uses, because the short form is
                // exactly the lossy rendering that hid it in the first place.
                const displayCode = matchedFullCode
                  ? (error.full_code!.match(/.{1,4}/g) ?? [shortCode]).join('-')
                  : shortCode.replace('_', '-');

                return (
                  <div
                    key={`${error.code}-${index}`}
                    className={`p-4 rounded-lg ${bgColor} border border-white/10`}
                  >
                    <div className="flex items-start gap-3">
                      <Icon className={`w-5 h-5 ${color} flex-shrink-0 mt-0.5`} />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-1">
                          <span className={`font-mono text-sm ${color}`}>[{displayCode}]</span>
                          <span className={`text-xs px-2 py-0.5 rounded-full ${bgColor} ${color}`}>
                            {label}
                          </span>
                        </div>
                        <p className="text-sm text-bambu-gray mb-2">{description}</p>
                        {remedy && <p className="text-sm text-white mb-2">{remedy}</p>}
                        {renderableActions(error.actions).length > 0 && hasPermission('printers:control') && (
                          <div className="flex flex-wrap gap-2 my-2">
                            {renderableActions(error.actions).map((action) => {
                              const pendingVars = activateActionMutation.variables;
                              const isThisPending =
                                activateActionMutation.isPending
                                && pendingVars?.action === action
                                && pendingVars?.print_error === (error.full_code || shortCode.replace('_', ''));
                              return (
                                <button
                                  key={action}
                                  disabled={activateActionMutation.isPending}
                                  onClick={() => {
                                    // full_code is the firmware-matching key (16 chars for
                                    // hms[]-array faults, 8 for print_error). Fall back to the
                                    // 8-char shortCode for older backends. See #1830.
                                    activateActionMutation.mutate({
                                      action,
                                      print_error: error.full_code || shortCode.replace('_', ''),
                                      job_id: error.job_id ?? null,
                                    });
                                  }}
                                  // Static hover/active classes — Tailwind's JIT can't
                                  // resolve `hover:${var}` template literals, so the previous
                                  // severity-tinted hover never reached the compiled CSS and
                                  // the action buttons read as inert badges. White-on-tint
                                  // reads as a clear affordance against any severity container.
                                  className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-white/10 hover:bg-white/20 active:bg-white/30 text-white border border-white/20 hover:border-white/30 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex-shrink-0"
                                >
                                  {isThisPending && <Loader2 className="w-4 h-4 animate-spin" />}
                                  {t(`hmsErrors.actions.${action}`, action)}
                                </button>
                              );
                            })}
                          </div>
                        )}
                        <div className="flex items-center justify-between gap-3">
                          <a
                            href={hmsHomeUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex items-center gap-1 text-xs text-bambu-green hover:underline"
                          >
                            <ExternalLink className="w-3 h-3" />
                            {t('hmsErrors.viewOnWiki')}
                          </a>
                          {canHide(error) && (
                            <button
                              type="button"
                              onClick={() => muteMutation.mutate(error.full_code!)}
                              disabled={muteMutation.isPending}
                              title={t('hmsErrors.hideHint')}
                              className="inline-flex items-center gap-1 text-xs text-bambu-gray hover:text-white disabled:opacity-50"
                            >
                              <EyeOff className="w-3 h-3" />
                              {t('hmsErrors.hide')}
                            </button>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {/* Entries hidden on this printer until the printer drops them. Shown
              dimmed with their code and text, and a way back — hiding is a
              decision about one incident, not a filter. */}
          {mutedErrors.length > 0 && (
            <div className="mt-4 pt-3 border-t border-bambu-dark-tertiary">
              <p className="text-xs text-bambu-gray mb-2">
                {t('hmsErrors.hiddenTitle', { count: mutedErrors.length })}
              </p>
              <div className="space-y-2">
                {mutedErrors.map((error, index) => {
                  const codeNum = parseInt(error.code.replace('0x', ''), 16) || 0;
                  const shortCode = getShortCode(error.attr, codeNum);
                  const displayCode = error.full_code
                    ? (error.full_code.match(/.{1,4}/g) ?? [shortCode]).join('-')
                    : shortCode.replace('_', '-');
                  const text = lookupDescription(descriptions, error.full_code, shortCode);
                  return (
                    <div
                      key={`muted-${error.full_code || error.code}-${index}`}
                      className="p-3 rounded-lg bg-bambu-dark border border-white/5 opacity-70 flex items-start justify-between gap-3"
                    >
                      <div className="min-w-0">
                        <span className="font-mono text-xs text-bambu-gray">[{displayCode}]</span>
                        <p className="text-xs text-bambu-gray truncate">{text ?? t('hmsErrors.noDescriptionYet')}</p>
                      </div>
                      {hasPermission('printers:control') && (
                        <button
                          type="button"
                          onClick={() => unmuteMutation.mutate(error.full_code!)}
                          disabled={unmuteMutation.isPending}
                          className="inline-flex items-center gap-1 text-xs text-bambu-green hover:underline flex-shrink-0"
                        >
                          <Eye className="w-3 h-3" />
                          {t('hmsErrors.unhide')}
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="p-4 border-t border-bambu-dark-tertiary flex items-center justify-between gap-3">
          <p className="text-xs text-bambu-gray">
            {t('hmsErrors.clearInstructions')}
          </p>
          {knownErrors.length > 0 && (
            <button
              onClick={() => clearMutation.mutate()}
              disabled={!hasPermission('printers:control') || clearMutation.isPending}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400 hover:bg-red-200 dark:hover:bg-red-500/30 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex-shrink-0"
            >
              {clearMutation.isPending ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Trash2 className="w-4 h-4" />
              )}
              {t('hmsErrors.clearErrors')}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
