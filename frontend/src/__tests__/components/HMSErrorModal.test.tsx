/**
 * Tests for the HMSErrorModal component.
 */

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { screen, fireEvent, cleanup, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { HMSErrorModal, filterKnownHMSErrors, HMS_MQTT_VERIFY_FAILED } from '../../components/HMSErrorModal';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import type { HMSError, Permission } from '../../api/client';

// Error code 0300_400C = "The task was canceled." (known code in the database)
const knownError: HMSError = {
  attr: 0x0300,
  code: '0x400C',
  module: 0,
  severity: 2,
};

// Error code FFFF_FFFF = unknown (not in the database)
const unknownError: HMSError = {
  attr: 0xFFFF,
  code: '0xFFFF',
  module: 0,
  severity: 1,
};

describe('the catalogue is no longer in the bundle', () => {
  // ⚠️ A source check, because nothing else would fail if it came back. The
  // constant was 118 KB for 853 entries — the full catalogue in that shape is
  // megabytes shipped to every browser, for data that changes when Bambu ships
  // a firmware and not when we deploy.
  it('HMSErrorModal.tsx carries no ERROR_DESCRIPTIONS', async () => {
    const fs = await import('fs');
    const source = fs.readFileSync('src/components/HMSErrorModal.tsx', 'utf8');
    expect(source).not.toContain('const ERROR_DESCRIPTIONS');
  });

  it('still exports the one code it explains in its own words', () => {
    // BamDude's advice for a verification failure is "enable Developer Mode",
    // which is ours and not Bambu's — it must survive the catalogue move.
    expect(HMS_MQTT_VERIFY_FAILED).toBe('0500050000010007');
  });
});

describe('filterKnownHMSErrors', () => {
  // ⚠️ The filter used to keep an error only if the catalogue described it or
  // the firmware offered actions. Anything else vanished — and the printer card
  // stayed green while the machine was reporting a fault. A live X2D refused to
  // record a timelapse because the card was full, said so over MQTT, and
  // BamDude showed nothing anywhere.
  const uncatalogued: HMSError = {
    attr: 0x05000100,
    code: '0x00030004',
    module: 0,
    severity: 2,
    full_code: '0500010000030004',
    actions: [],
  };

  it('keeps an error that has neither a description nor actions', () => {
    expect(filterKnownHMSErrors([uncatalogued])).toHaveLength(1);
  });

  it('still keeps the ones that do have actions', () => {
    expect(filterKnownHMSErrors([{ ...uncatalogued, actions: ['STOP_PRINTING'] }])).toHaveLength(1);
  });

  it('keeps every error it is given', () => {
    expect(filterKnownHMSErrors([uncatalogued, knownError, unknownError])).toHaveLength(3);
  });
});

describe('HMSErrorModal', () => {
  const defaultProps = {
    printerName: 'Test Printer',
    errors: [knownError],
    onClose: vi.fn(),
    printerId: 1,
    // ⚠️ 20P is an X2D. The descriptions are per model now, so a modal with no
    // serial has no catalogue to describe anything with — which is exactly what
    // the "unrecognised code" fallback is for, and is asserted separately.
    serialNumber: '20P6BJ640901852',
    hasPermission: vi.fn().mockReturnValue(true) as unknown as (permission: Permission) => boolean,
  };

  beforeEach(() => {
    // The catalogue the modal fetches for that model. Only the codes these
    // tests exercise — the real file has 5 372.
    server.use(
      http.get('/api/v1/hms/descriptions', () =>
        HttpResponse.json({
          device: '20P',
          lang: 'en',
          descriptions: {
            '0300400C': 'The task was canceled.',
            '07008011': 'AMS filament ran out. Please insert a new filament into the same AMS slot.',
          },
        })
      )
    );
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  describe('rendering', () => {
    it('renders the modal title with printer name', () => {
      render(<HMSErrorModal {...defaultProps} />);
      expect(screen.getByText('Errors - Test Printer')).toBeInTheDocument();
    });

    it('shows error description for known error codes', async () => {
      // ⚠️ Awaited now: the catalogue is fetched per model rather than bundled,
      // so the text lands a moment after the modal does. Cached from then on —
      // only the first open for a given model and language pays for it.
      render(<HMSErrorModal {...defaultProps} />);
      expect(await screen.findByText('The task was canceled.')).toBeInTheDocument();
    });

    it('shows an unknown error rather than the empty state', () => {
      // The empty state now means what it says: nothing was reported. It used
      // to also mean "everything reported was unrecognised", which is the
      // opposite of empty and is exactly how a real fault went unnoticed.
      render(<HMSErrorModal {...defaultProps} errors={[unknownError]} />);
      expect(screen.queryByText('No errors')).not.toBeInTheDocument();
      expect(
        screen.getByText('Unknown HMS code — see the Bambu Lab wiki for details.')
      ).toBeInTheDocument();
    });

    it('shows no errors message when errors array is empty', () => {
      render(<HMSErrorModal {...defaultProps} errors={[]} />);
      expect(screen.getByText('No errors')).toBeInTheDocument();
    });
  });

  // upstream #2587 — the firmware's runout text says "insert into the same AMS
  // slot", which is wrong under AMS Filament Backup: the firmware won't re-accept
  // the depleted slot and advances to the next compatible one.
  describe('runout guidance', () => {
    const runoutError: HMSError = {
      attr: 0x0700,
      code: '0x8011',
      module: 0,
      severity: 2,
    };
    const genericRunoutText = 'AMS filament ran out. Please insert a new filament into the same AMS slot.';

    it('keeps the generic firmware text when no guidance is supplied', async () => {
      render(<HMSErrorModal {...defaultProps} errors={[runoutError]} />);
      expect(await screen.findByText(genericRunoutText)).toBeInTheDocument();
    });

    it('names both the expected and the ran-out slot when both resolve', () => {
      render(
        <HMSErrorModal
          {...defaultProps}
          errors={[runoutError]}
          runoutGuidance={{ expectedSlotLabel: 'AMS-A · Slot 3', ranOutSlotLabel: 'AMS-A · Slot 2' }}
        />
      );
      expect(screen.queryByText(genericRunoutText)).not.toBeInTheDocument();
      expect(screen.getByText(/AMS-A · Slot 2/)).toBeInTheDocument();
      expect(screen.getByText(/AMS-A · Slot 3/)).toBeInTheDocument();
    });

    it('names only the expected slot when the ran-out slot is unknown', () => {
      render(
        <HMSErrorModal
          {...defaultProps}
          errors={[runoutError]}
          runoutGuidance={{ expectedSlotLabel: 'AMS-A · Slot 3', ranOutSlotLabel: null }}
        />
      );
      expect(screen.getByText(/waiting for compatible filament in AMS-A · Slot 3/)).toBeInTheDocument();
    });

    it('falls back to honest "check the printer" copy when nothing resolves', () => {
      render(
        <HMSErrorModal
          {...defaultProps}
          errors={[runoutError]}
          runoutGuidance={{ expectedSlotLabel: null, ranOutSlotLabel: null }}
        />
      );
      expect(screen.getByText(/could not determine which slot the printer now expects/)).toBeInTheDocument();
    });

    it('leaves non-runout error codes untouched', async () => {
      render(
        <HMSErrorModal
          {...defaultProps}
          runoutGuidance={{ expectedSlotLabel: 'AMS-A · Slot 3', ranOutSlotLabel: 'AMS-A · Slot 2' }}
        />
      );
      expect(await screen.findByText('The task was canceled.')).toBeInTheDocument();
    });
  });

  describe('clear errors button', () => {
    it('shows clear button when there are known errors', () => {
      render(<HMSErrorModal {...defaultProps} />);
      expect(screen.getByText('Clear Errors')).toBeInTheDocument();
    });

    it('hides clear button when there are no known errors', () => {
      render(<HMSErrorModal {...defaultProps} errors={[]} />);
      expect(screen.queryByText('Clear Errors')).not.toBeInTheDocument();
    });

    it('offers Clear for an unknown code too', () => {
      // ⚠️ Clearing is how an operator dismisses a fault they have dealt with.
      // Withholding it from precisely the faults BamDude cannot name left them
      // stuck on the card with no way to acknowledge them.
      render(<HMSErrorModal {...defaultProps} errors={[unknownError]} />);
      expect(screen.getByText('Clear Errors')).toBeInTheDocument();
    });

    it('disables clear button when user lacks permission', () => {
      const noPermission = vi.fn().mockReturnValue(false) as unknown as (permission: Permission) => boolean;
      render(<HMSErrorModal {...defaultProps} hasPermission={noPermission} />);
      expect(screen.getByText('Clear Errors').closest('button')).toBeDisabled();
    });

    it('calls API and closes modal on successful clear', async () => {
      const user = userEvent.setup();
      const onClose = vi.fn();

      server.use(
        http.post('/api/v1/printers/1/hms/clear', () => {
          return HttpResponse.json({ success: true, message: 'HMS errors cleared' });
        })
      );

      render(<HMSErrorModal {...defaultProps} onClose={onClose} />);

      await user.click(screen.getByText('Clear Errors'));

      await waitFor(() => {
        expect(onClose).toHaveBeenCalledTimes(1);
      });
    });

    it('shows error toast on failed clear', async () => {
      const user = userEvent.setup();
      const onClose = vi.fn();

      server.use(
        http.post('/api/v1/printers/1/hms/clear', () => {
          return HttpResponse.json({ detail: 'Failed' }, { status: 500 });
        })
      );

      render(<HMSErrorModal {...defaultProps} onClose={onClose} />);

      await user.click(screen.getByText('Clear Errors'));

      await waitFor(() => {
        expect(onClose).not.toHaveBeenCalled();
      });
    });
  });

  describe('uncataloged-but-actionable faults (#1840)', () => {
    // Uncataloged code (FFFF_FFFF not in ERROR_DESCRIPTIONS) that nonetheless
    // carries firmware actions — e.g. H2C 0500_809C. Must surface so the action
    // button can render, with the unknown-code fallback description.
    const actionableUncataloged: HMSError = {
      attr: 0xFFFF,
      code: '0xFFFF',
      module: 0,
      severity: 3,
      actions: ['IGNORE_RESUME'],
      full_code: 'FFFFFFFF',
    };

    it('surfaces an uncataloged fault that carries firmware actions', () => {
      render(<HMSErrorModal {...defaultProps} errors={[actionableUncataloged]} />);
      // Not the empty state — the fault is shown even without a catalog entry.
      expect(screen.queryByText('No errors')).not.toBeInTheDocument();
      // Falls back to the unknown-code description.
      expect(
        screen.getByText('Unknown HMS code — see the Bambu Lab wiki for details.')
      ).toBeInTheDocument();
      // And still renders the action button so the user can act on it.
      expect(screen.getByText('Ignore this and Resume')).toBeInTheDocument();
    });

    it('surfaces an uncataloged fault even with no actions', () => {
      // ⚠️ This asserted the opposite until 2026-08-15, to keep "junk-echo
      // noise" off the card. That noise is real — but it is filtered at the
      // SOURCE and always was: bambu_mqtt drops anything below 0x4000 as a
      // status indicator, and drops the named cancel echoes (_HMS_USER_ACTION_
      // CODES: 0300_400C, 0500_400E) at parse time, so they never reach state
      // at all. The frontend rule was a second, blunter net over a precise one,
      // and it also caught real faults: a live X2D refused to record a
      // timelapse because the card was full and BamDude showed nothing.
      render(<HMSErrorModal {...defaultProps} errors={[unknownError]} />);
      expect(screen.queryByText('No errors')).not.toBeInTheDocument();
    });
  });

  describe('interactions', () => {
    it('calls onClose when X button is clicked', async () => {
      const user = userEvent.setup();
      const onClose = vi.fn();
      render(<HMSErrorModal {...defaultProps} onClose={onClose} />);

      // The X button is the button with the X icon in the header
      const closeButtons = screen.getAllByRole('button');
      // First button is the X close button in the header
      await user.click(closeButtons[0]);
      expect(onClose).toHaveBeenCalledTimes(1);
    });

    it('calls onClose when Escape key is pressed', () => {
      const onClose = vi.fn();
      render(<HMSErrorModal {...defaultProps} onClose={onClose} />);

      fireEvent.keyDown(window, { key: 'Escape' });
      expect(onClose).toHaveBeenCalledTimes(1);
    });
  });

  /**
   * "MQTT command verification failed" — the printer refusing every control
   * command and saying so. Its meaning lives in attr's low half (0500) and
   * code's high half (0001), both discarded by the MMMM_EEEE short form, which
   * collapses it to a useless "0500_0007". That is why it was received and then
   * dropped: uncataloged, no actions, filtered out — so the one message that
   * explained why nothing printed was the one message hidden (#2732).
   */
  describe('an error only its full code can express', () => {
    const verifyFailed: HMSError = {
      attr: 0x05000500,
      code: '0x00010007',
      module: 0,
      severity: 1,
      full_code: '0500050000010007',
    };

    it('survives the filter that drops uncataloged action-less errors', () => {
      expect(filterKnownHMSErrors([verifyFailed])).toHaveLength(1);
    });

    it('the short code alone still matches no description', () => {
      // Guards the reason this needed a full-code lookup at all: if someone
      // "simplifies" it back to the short form, this is what they get.
      //
      // ⚠️ Asserted through what the operator sees, not through the filter.
      // The filter used to drop this error entirely, and that is exactly what
      // it must no longer do — but the lookup still has to miss, or the
      // full-code path was pointless.
      const shortOnly: HMSError = { ...verifyFailed, full_code: undefined };
      render(<HMSErrorModal {...defaultProps} errors={[shortOnly]} />);

      expect(screen.queryByText(/could not verify it/i)).not.toBeInTheDocument();
    });

    it('shows the explanation and our own remedy, not Bambu\'s', () => {
      render(<HMSErrorModal {...defaultProps} errors={[verifyFailed]} />);

      expect(screen.getByText(/could not verify it/i)).toBeInTheDocument();
      // Bambu's wiki says to update Studio or Handy, which is no help to
      // someone printing from BamDude.
      expect(screen.getByText(/Developer Mode/i)).toBeInTheDocument();
    });

    it('shows the four-group code the printer itself displays', async () => {
      render(<HMSErrorModal {...defaultProps} errors={[verifyFailed]} />);

      expect(await screen.findByText('[0500-0500-0001-0007]')).toBeInTheDocument();
    });

    it('leaves ordinary short-coded errors on their short form', () => {
      render(<HMSErrorModal {...defaultProps} errors={[knownError]} />);

      expect(screen.getByText('[0300-400C]')).toBeInTheDocument();
    });
  });
});
