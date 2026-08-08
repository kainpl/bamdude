/**
 * Tests for the HMSErrorModal component.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { screen, fireEvent, cleanup, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { HMSErrorModal, filterKnownHMSErrors } from '../../components/HMSErrorModal';
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

describe('HMSErrorModal', () => {
  const defaultProps = {
    printerName: 'Test Printer',
    errors: [knownError],
    onClose: vi.fn(),
    printerId: 1,
    hasPermission: vi.fn().mockReturnValue(true) as unknown as (permission: Permission) => boolean,
  };

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  describe('rendering', () => {
    it('renders the modal title with printer name', () => {
      render(<HMSErrorModal {...defaultProps} />);
      expect(screen.getByText('Errors - Test Printer')).toBeInTheDocument();
    });

    it('shows error description for known error codes', () => {
      render(<HMSErrorModal {...defaultProps} />);
      expect(screen.getByText('The task was canceled.')).toBeInTheDocument();
    });

    it('shows no errors message when all errors are unknown', () => {
      render(<HMSErrorModal {...defaultProps} errors={[unknownError]} />);
      expect(screen.getByText('No errors')).toBeInTheDocument();
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

    it('keeps the generic firmware text when no guidance is supplied', () => {
      render(<HMSErrorModal {...defaultProps} errors={[runoutError]} />);
      expect(screen.getByText(genericRunoutText)).toBeInTheDocument();
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

    it('leaves non-runout error codes untouched', () => {
      render(
        <HMSErrorModal
          {...defaultProps}
          runoutGuidance={{ expectedSlotLabel: 'AMS-A · Slot 3', ranOutSlotLabel: 'AMS-A · Slot 2' }}
        />
      );
      expect(screen.getByText('The task was canceled.')).toBeInTheDocument();
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

    it('hides clear button when all errors are unknown codes', () => {
      render(<HMSErrorModal {...defaultProps} errors={[unknownError]} />);
      expect(screen.queryByText('Clear Errors')).not.toBeInTheDocument();
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

    it('still drops an uncataloged fault that carries no actions (junk-echo noise)', () => {
      render(<HMSErrorModal {...defaultProps} errors={[unknownError]} />);
      expect(screen.getByText('No errors')).toBeInTheDocument();
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

    it('the short code alone still matches nothing', () => {
      // Guards the reason this needed a full-code lookup at all: if someone
      // "simplifies" it back to the short form, this is what they get.
      const shortOnly: HMSError = { ...verifyFailed, full_code: undefined };
      expect(filterKnownHMSErrors([shortOnly])).toHaveLength(0);
    });

    it('shows the explanation and our own remedy, not Bambu\'s', () => {
      render(<HMSErrorModal {...defaultProps} errors={[verifyFailed]} />);

      expect(screen.getByText(/could not verify it/i)).toBeInTheDocument();
      // Bambu's wiki says to update Studio or Handy, which is no help to
      // someone printing from BamDude.
      expect(screen.getByText(/Developer Mode/i)).toBeInTheDocument();
    });

    it('shows the four-group code the printer itself displays', () => {
      render(<HMSErrorModal {...defaultProps} errors={[verifyFailed]} />);

      expect(screen.getByText('[0500-0500-0001-0007]')).toBeInTheDocument();
    });

    it('leaves ordinary short-coded errors on their short form', () => {
      render(<HMSErrorModal {...defaultProps} errors={[knownError]} />);

      expect(screen.getByText('[0300-400C]')).toBeInTheDocument();
    });
  });
});
