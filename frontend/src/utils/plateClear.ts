export type PrinterCardSize = 'compact' | 'expanded';

interface ClearPlateButtonInput {
  connected: boolean | undefined;
  /** ``require_plate_clear`` is on for this printer AND it is awaiting a clear. */
  needsPlateClear: boolean;
  isPrintingOrPaused: boolean;
  /** Whether `PrinterQueueWidget` is about to draw its green "Clear Plate &
   *  Start Next" call to action for this printer. */
  greenClearCtaVisible: boolean;
  viewMode: PrinterCardSize;
}

/**
 * Whether the printer card shows its own small "plate cleared" button.
 *
 * ⚠️ **It defers to the green CTA only where that CTA exists.**
 * `PrinterQueueWidget` — which draws it — is rendered inside the expanded
 * branch of the card and nowhere else. Suppressing the yellow button on a
 * size-S card therefore removed the control and put nothing in its place: a
 * finished printer could not be cleared from the fleet view at all, and the
 * operator had to open the printer to do it.
 *
 * ⚠️ **The trigger is the queue, not the model.** The green CTA needs at least
 * one auto-dispatchable pending item, so a printer with work waiting lost the
 * button while an idle one kept it. Reported as "the button is always there on
 * the H2S and never on the P2S", which is what that looks like on a farm where
 * one model happens to carry the queues.
 */
export function shouldShowClearPlateButton({
  connected,
  needsPlateClear,
  isPrintingOrPaused,
  greenClearCtaVisible,
  viewMode,
}: ClearPlateButtonInput): boolean {
  if (!connected || !needsPlateClear || isPrintingOrPaused) return false;
  return !(viewMode === 'expanded' && greenClearCtaVisible);
}
