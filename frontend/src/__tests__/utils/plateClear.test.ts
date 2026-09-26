/**
 * Whether the printer card offers its own "plate cleared" button.
 *
 * Reported by an operator: "the Clear plate button is missing — it always shows
 * on the H2S, but for the P2S you have to open the printer and click there",
 * in size S on the Printers page.
 *
 * The model was a coincidence. The button stepped aside for the green "Clear
 * Plate & Start Next" call to action in `PrinterQueueWidget` — which only
 * renders on an expanded card — so on a size-S card it hid itself in favour of
 * nothing. And the green CTA needs an auto-dispatchable pending item, so it was
 * the printers with queues that lost the control.
 */

import { describe, it, expect } from 'vitest';
import { shouldShowClearPlateButton } from '../../utils/plateClear';

const ready = {
  needsPlateClear: true,
  isPrintingOrPaused: false,
  greenClearCtaVisible: false,
  viewMode: 'compact' as const,
};

describe('the reported case', () => {
  it('shows on a size-S card even when a queue is waiting', () => {
    expect(shouldShowClearPlateButton({ ...ready, greenClearCtaVisible: true })).toBe(true);
  });

  it('still steps aside on an expanded card, where the green CTA is drawn', () => {
    expect(
      shouldShowClearPlateButton({ ...ready, viewMode: 'expanded', greenClearCtaVisible: true }),
    ).toBe(false);
  });

  it('shows on an expanded card with no queue to start next', () => {
    expect(shouldShowClearPlateButton({ ...ready, viewMode: 'expanded' })).toBe(true);
  });
});

describe('when there is nothing to clear', () => {
  it('stays hidden while the printer is printing', () => {
    expect(shouldShowClearPlateButton({ ...ready, isPrintingOrPaused: true })).toBe(false);
  });

  it('stays hidden when the plate does not need clearing', () => {
    expect(shouldShowClearPlateButton({ ...ready, needsPlateClear: false })).toBe(false);
  });

  it('does not ask whether the printer is connected (upstream #2864)', () => {
    // Clearing sends nothing to the printer, and with Auto Power Off "gate up,
    // printer off" is the ordinary end of a print — the card shows the answer
    // on a switched-off printer too (PrintersPageClearPlateOffline).
    expect(shouldShowClearPlateButton(ready)).toBe(true);
    expect(Object.keys(ready)).not.toContain('connected');
  });

  it('a printing printer is hidden in both sizes, queue or no queue', () => {
    for (const viewMode of ['compact', 'expanded'] as const) {
      for (const greenClearCtaVisible of [true, false]) {
        expect(
          shouldShowClearPlateButton({ ...ready, isPrintingOrPaused: true, viewMode, greenClearCtaVisible }),
        ).toBe(false);
      }
    }
  });
});

describe('the two plate answers never appear twice', () => {
  // ⚠️ Now load-bearing for two PAIRS of buttons rather than two single ones:
  // the yellow pair on the printer card and the green pair in the queue widget.
  // The operator must still see exactly one pair.
  const base = { needsPlateClear: true, isPrintingOrPaused: false };

  it('shows the card pair when the queue has nothing to offer', () => {
    // The empty-queue case: PrinterQueueWidget returns null outright on
    // totalPending === 0, so the card must carry both answers.
    expect(
      shouldShowClearPlateButton({ ...base, greenClearCtaVisible: false, viewMode: 'expanded' }),
    ).toBe(true);
  });

  it('yields to the widget pair when that one is on screen', () => {
    expect(
      shouldShowClearPlateButton({ ...base, greenClearCtaVisible: true, viewMode: 'expanded' }),
    ).toBe(false);
  });

  it('shows nothing while a print is running', () => {
    expect(
      shouldShowClearPlateButton({
        ...base,
        isPrintingOrPaused: true,
        greenClearCtaVisible: false,
        viewMode: 'expanded',
      }),
    ).toBe(false);
  });

  it('shows nothing when the plate needs no confirmation', () => {
    expect(
      shouldShowClearPlateButton({
        ...base,
        needsPlateClear: false,
        greenClearCtaVisible: false,
        viewMode: 'expanded',
      }),
    ).toBe(false);
  });
});
