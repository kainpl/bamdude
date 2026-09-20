import { describe, it, expect, vi } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { AmsBackupModal } from '../../components/AmsBackupModal';
import type { BackupCompatibilityApplyResult } from '../../api/client';

const preview: BackupCompatibilityApplyResult = {
  dry_run: true, applied: 0, skipped: 1, would_apply: 1,
  rows: [
    { ams_id: 0, tray_id: 0, slot: 'A1', source: 'internal', spool: '#7 Bambu PETG', action: 'apply', reasons: [], published: null, kprofile: null,
      actual: { tray_info_idx: 'GFG00', tray_type: 'PETG', tray_color: 'FF0000FF', cols: [], setting_id: 'GFSG00' },
      advertised: { tray_info_idx: 'GFG99', tray_type: 'PETG', tray_color: '000000FF', cols: [], setting_id: 'GFSG99_00' } },
    { ams_id: 0, tray_id: 1, slot: 'A2', source: 'internal', spool: '#8 Bambu PETG', action: 'skip', reasons: ['rfid_slot_excluded'], published: null, kprofile: null,
      actual: { tray_info_idx: 'GFG00', tray_type: 'PETG', tray_color: '0000FFFF', cols: [], setting_id: 'GFSG00' },
      advertised: { tray_info_idx: 'GFG00', tray_type: 'PETG', tray_color: '0000FFFF', cols: [], setting_id: 'GFSG00' } },
  ],
};

describe('AmsBackupModal bulk apply', () => {
  it('previews, then applies only after confirmation', async () => {
    const onPreview = vi.fn().mockResolvedValue(preview);
    const onApply = vi.fn().mockResolvedValue({ ...preview, dry_run: false, applied: 1, rows: preview.rows.map((r) => ({ ...r, published: r.action === 'apply' ? true : null })) });
    render(
      <AmsBackupModal isOpen state={true} amsUnits={[]} amsExtruderMap={undefined} firmwareGroups={{}} isDualNozzle={false}
        canToggle pending={false} onToggle={() => {}} onClose={() => {}}
        compat={{ policyEnabled: true, canApply: true, onPreview, onApply }} />,
    );
    await userEvent.click(screen.getByRole('button', { name: /apply to assigned slots/i }));
    await waitFor(() => expect(onPreview).toHaveBeenCalledTimes(1));
    expect(screen.getByText('A1')).toBeInTheDocument();
    expect(screen.getByText(/RFID/i)).toBeInTheDocument();
    expect(onApply).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: /confirm/i }));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/1 slot/i)).toBeInTheDocument();
  });

  it('reports the apply per row, including a slot the printer refused', async () => {
    const onPreview = vi.fn().mockResolvedValue(preview);
    // The apply recomputes every row: A2 is projected this time, and the
    // printer refuses it. Only A1 counts toward `applied`.
    const onApply = vi.fn().mockResolvedValue({
      ...preview,
      dry_run: false,
      applied: 1,
      skipped: 1,
      rows: [
        { ...preview.rows[0], published: true },
        { ...preview.rows[1], action: 'apply' as const, reasons: [], published: false },
      ],
    });
    render(
      <AmsBackupModal isOpen state={true} amsUnits={[]} amsExtruderMap={undefined} firmwareGroups={{}} isDualNozzle={false}
        canToggle pending={false} onToggle={() => {}} onClose={() => {}}
        compat={{ policyEnabled: true, canApply: true, onPreview, onApply }} />,
    );
    await userEvent.click(screen.getByRole('button', { name: /apply to assigned slots/i }));
    await userEvent.click(await screen.findByRole('button', { name: /confirm/i }));
    expect(await screen.findByText(/not sent/i)).toBeInTheDocument();
    expect(screen.getByText(/1 slot/i)).toBeInTheDocument();
    // The dry-run rows are gone: A2's preview reason is not on screen any more.
    expect(screen.queryByText(/RFID/i)).not.toBeInTheDocument();
  });

  it('forgets a preview the user walked away from', async () => {
    const onPreview = vi.fn().mockResolvedValue(preview);
    const compat = { policyEnabled: true, canApply: true, onPreview, onApply: vi.fn() };
    const props = {
      state: true, amsUnits: [], amsExtruderMap: undefined, firmwareGroups: {}, isDualNozzle: false,
      canToggle: true, pending: false, onToggle: () => {}, onClose: () => {}, compat,
    };
    const { rerender } = render(<AmsBackupModal isOpen {...props} />);
    await userEvent.click(screen.getByRole('button', { name: /apply to assigned slots/i }));
    expect(await screen.findByText('A1')).toBeInTheDocument();
    // The caller keeps the dialog mounted, so closing must clear the plan.
    rerender(<AmsBackupModal isOpen={false} {...props} />);
    rerender(<AmsBackupModal isOpen {...props} />);
    expect(screen.queryByText('A1')).not.toBeInTheDocument();
  });

  it('ignores a preview that lands after the dialog was closed', async () => {
    let land: (r: BackupCompatibilityApplyResult) => void = () => {};
    const onPreview = vi.fn().mockImplementation(
      () => new Promise<BackupCompatibilityApplyResult>((resolve) => { land = resolve; }),
    );
    const compat = { policyEnabled: true, canApply: true, onPreview, onApply: vi.fn() };
    const props = {
      state: true, amsUnits: [], amsExtruderMap: undefined, firmwareGroups: {}, isDualNozzle: false,
      canToggle: true, pending: false, onToggle: () => {}, onClose: () => {}, compat,
    };
    const { rerender } = render(<AmsBackupModal isOpen {...props} />);
    await userEvent.click(screen.getByRole('button', { name: /apply to assigned slots/i }));
    rerender(<AmsBackupModal isOpen={false} {...props} />);
    await act(async () => { land(preview); });
    rerender(<AmsBackupModal isOpen {...props} />);
    expect(screen.queryByText('A1')).not.toBeInTheDocument();
    // …and the round trip nobody is waiting for did not leave the dialog inert.
    expect(screen.getByRole('button', { name: /apply to assigned slots/i })).toBeEnabled();
  });

  it('says when the list is only the half of the farm Spoolman was not needed for', async () => {
    // A halved list reads exactly like a complete one, and the operator would
    // take "2 slots" for the whole printer.
    const onPreview = vi.fn().mockResolvedValue({ ...preview, spoolman_unavailable: true });
    render(
      <AmsBackupModal isOpen state={true} amsUnits={[]} amsExtruderMap={undefined} firmwareGroups={{}} isDualNozzle={false}
        canToggle pending={false} onToggle={() => {}} onClose={() => {}}
        compat={{ policyEnabled: true, canApply: true, onPreview, onApply: vi.fn() }} />,
    );
    await userEvent.click(screen.getByRole('button', { name: /apply to assigned slots/i }));
    expect(await screen.findByText(/Spoolman could not be reached/i)).toBeInTheDocument();
  });

  it('explains that the firmware, not BamDude, decides grouping when the policy is on and nothing paired', () => {
    render(
      <AmsBackupModal isOpen state={true} amsUnits={[]} amsExtruderMap={undefined} firmwareGroups={{ '0': [] }} isDualNozzle={false}
        canToggle pending={false} onToggle={() => {}} onClose={() => {}}
        compat={{ policyEnabled: true, canApply: false, onPreview: vi.fn(), onApply: vi.fn() }} />,
    );
    expect(screen.getByText(/firmware did not merge/i)).toBeInTheDocument();
  });
});
