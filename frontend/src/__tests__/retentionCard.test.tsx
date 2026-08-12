import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { RetentionCard } from '../components/settings/RetentionCard';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

describe('RetentionCard', () => {
  it('offers all four windows, not only the new ones', () => {
    render(<RetentionCard values={{}} onSave={() => {}} saving={false} />);

    expect(screen.getAllByRole('spinbutton')).toHaveLength(4);
  });

  it('falls back to thirty days when a value has never been set', () => {
    render(<RetentionCard values={{}} onSave={() => {}} saving={false} />);

    expect(screen.getByLabelText('settings.retention.ams')).toHaveValue(30);
  });

  it('shows what is stored rather than the default', () => {
    render(<RetentionCard values={{ ams_history_retention_days: 7 }} onSave={() => {}} saving={false} />);

    expect(screen.getByLabelText('settings.retention.ams')).toHaveValue(7);
  });

  it('sends every field, so an untouched one is not lost', () => {
    const onSave = vi.fn();
    render(<RetentionCard values={{ ams_history_retention_days: 7 }} onSave={onSave} saving={false} />);

    fireEvent.change(screen.getByLabelText('settings.retention.plugPower'), { target: { value: '14' } });
    fireEvent.click(screen.getByRole('button'));

    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({ ams_history_retention_days: 7, plug_power_history_retention_days: 14 }),
    );
  });
});
