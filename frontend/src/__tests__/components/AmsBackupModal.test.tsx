import { describe, it, expect, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
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

  it('explains that the firmware, not BamDude, decides grouping when the policy is on and nothing paired', () => {
    render(
      <AmsBackupModal isOpen state={true} amsUnits={[]} amsExtruderMap={undefined} firmwareGroups={{ '0': [] }} isDualNozzle={false}
        canToggle pending={false} onToggle={() => {}} onClose={() => {}}
        compat={{ policyEnabled: true, canApply: false, onPreview: vi.fn(), onApply: vi.fn() }} />,
    );
    expect(screen.getByText(/firmware did not merge/i)).toBeInTheDocument();
  });
});
