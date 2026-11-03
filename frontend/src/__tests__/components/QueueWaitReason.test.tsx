import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import i18n from '../../i18n';
import { QueueWaitReason } from '../../components/QueueWaitReason';
import { monitorPrinter } from '../../features/monitor/fixtures';
import { tileState } from '../../features/monitor/state';

const access = vi.hoisted(() => ({ allowed: true }));
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => ({ hasAllPermissions: () => access.allowed }) }));
const item = { waiting_reason: 'SERVER ENGLISH MUST NOT WIN', waiting_reason_code: 'dispatch_runtime_conflict', printer_id: 17 };

afterEach(async () => { access.allowed = true; await i18n.changeLanguage('en'); });

describe('a held print context is a distinct wait', () => {
  it.each([
    ['en', 'A previous print context is blocking this start', 'Check print states'],
    ['uk', 'Запуск заблоковано контекстом попереднього друку', 'Перевірити стани друку'],
  ])('renders the code in %s with an addressed read-only link', async (language, label, action) => {
    await i18n.changeLanguage(language);
    render(<QueueWaitReason item={item} />);
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.queryByText(item.waiting_reason)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: action })).toHaveAttribute('href', '/system?printer_id=17#print-state');
  });

  it('does not offer the diagnostics link without its read permissions', () => {
    access.allowed = false;
    render(<QueueWaitReason item={item} />);
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.getByText(/Ask an operator/)).toBeInTheDocument();
  });

  it('retains legacy text without classifying it by English words', () => {
    render(<QueueWaitReason item={{ waiting_reason: 'custom legacy text' }} />);
    expect(screen.getByText('custom legacy text')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('draws attention without calling the problem filament or printing', () => {
    const printer = monitorPrinter({ state: 'IDLE' });
    printer.queue!.waiting = { code: 'dispatch_runtime_conflict', since: null, until: null };
    expect(tileState(printer, 'queues')).toMatchObject({ kind: 'waiting', tone: 'warning', attention: true });
  });
});
