/**
 * Hiding one stack entry on one printer until the printer drops it.
 *
 * The firmware owns hms[]; Clear empties only the print_error register. A P2S
 * farm carried a code Bambu ships with no text in every push for weeks, and
 * the card's red pip could not be answered (2026-09-04). The modal offers to
 * hide such an entry — by its FULL 16-char code, never by "no description" —
 * and lists hidden ones with a way back.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { screen, cleanup, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { HMSErrorModal } from '../../components/HMSErrorModal';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import type { HMSError, Permission } from '../../api/client';

// The measured P2S entry: 0500_0600_0002_0070, level 2, no text anywhere.
const untexturedStackEntry: HMSError = {
  attr: 0x05000600,
  code: '0x20070',
  module: 5,
  severity: 2,
  full_code: '0500060000020070',
};

// An 8-char print_error fault: Clear works for it, so no Hide is offered.
const registerFault: HMSError = {
  attr: 0x0500,
  code: '0x4030',
  module: 5,
  severity: 2,
  full_code: '05004030',
};

const defaultProps = {
  printerName: 'P2S-01',
  onClose: vi.fn(),
  printerId: 1,
  serialNumber: '22E00A000000001',
  hasPermission: vi.fn().mockReturnValue(true) as unknown as (permission: Permission) => boolean,
};

describe('HMSErrorModal — hiding a stack entry', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/hms/descriptions', () => HttpResponse.json({ device: '22E', lang: 'en', descriptions: {} })),
    );
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('offers Hide for a 16-char stack entry and posts its full code', async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    server.use(
      http.post('/api/v1/printers/1/hms/mute', async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({ success: true });
      }),
    );

    render(<HMSErrorModal {...defaultProps} errors={[untexturedStackEntry]} />);

    await user.click(await screen.findByText('Hide until it goes away'));

    await waitFor(() => {
      expect(bodies).toEqual([{ full_code: '0500060000020070' }]);
    });
  });

  it('offers no Hide for an 8-char print_error fault — Clear already answers it', async () => {
    render(<HMSErrorModal {...defaultProps} errors={[registerFault]} />);

    await screen.findByText('Clear Errors');
    expect(screen.queryByText('Hide until it goes away')).not.toBeInTheDocument();
  });

  it('lists hidden entries with their code and a way back', async () => {
    const user = userEvent.setup();
    const bodies: unknown[] = [];
    server.use(
      http.post('/api/v1/printers/1/hms/unmute', async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({ success: true });
      }),
    );

    render(<HMSErrorModal {...defaultProps} errors={[]} mutedErrors={[untexturedStackEntry]} />);

    expect(await screen.findByText('1 entry hidden until the printer drops it')).toBeInTheDocument();
    expect(screen.getByText('[0500-0600-0002-0070]')).toBeInTheDocument();
    expect(screen.getByText('Bambu has not described this code yet')).toBeInTheDocument();

    await user.click(screen.getByText('Show again'));

    await waitFor(() => {
      expect(bodies).toEqual([{ full_code: '0500060000020070' }]);
    });
  });

  it('hides nothing and offers nothing without the control permission', async () => {
    const noControl = vi.fn().mockReturnValue(false) as unknown as (permission: Permission) => boolean;
    render(
      <HMSErrorModal {...defaultProps} hasPermission={noControl} errors={[untexturedStackEntry]} mutedErrors={[untexturedStackEntry]} />,
    );

    await screen.findByText('1 entry hidden until the printer drops it');
    expect(screen.queryByText('Hide until it goes away')).not.toBeInTheDocument();
    expect(screen.queryByText('Show again')).not.toBeInTheDocument();
  });
});
