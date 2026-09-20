import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { OpenMonitorButton } from '../../../features/monitor/OpenMonitorButton';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

describe('detached monitor entry', () => {
  afterEach(() => vi.restoreAllMocks());
  it('offers a normal tab link when the browser blocks the popup', () => {
    vi.spyOn(window, 'open').mockReturnValue(null);
    render(<OpenMonitorButton view="queues" sort="tag" />);
    fireEvent.click(screen.getByRole('button', { name: 'monitor.open' }));
    expect(screen.getByRole('link')).toHaveAttribute('href', '/monitor?view=queues&group=tag');
    expect(screen.getByRole('link')).toHaveAttribute('rel', 'noopener noreferrer');
  });
  it('opens the independent view and detaches the opener', () => {
    const child = { opener: window };
    const open = vi.spyOn(window, 'open').mockReturnValue(child as unknown as Window);
    render(<OpenMonitorButton view="printers" sort="eta" />);
    fireEvent.click(screen.getByRole('button', { name: 'monitor.open' }));
    expect(open).toHaveBeenCalledWith('/monitor?view=printers&sort=eta', '_blank', expect.stringContaining('popup'));
    expect(child.opener).toBeNull();
  });
});
