import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { UsageProjection } from '../../components/UsageProjection';

vi.mock('../../api/client', () => ({
  api: {
    getUsageProjection: vi.fn().mockResolvedValue({
      active: true,
      archive_id: 5,
      layer_num: 100,
      total_layers: 200,
      slots: [
        {
          slot_id: 1,
          type: 'PLA',
          color: '#FF0000',
          estimate_g: 300,
          consumed_g: 150,
          segments: [
            { start_layer: 0, spool_id: 7, spoolman_spool_id: null, consumed_g: 120 },
            { start_layer: 80, spool_id: 9, spoolman_spool_id: null, consumed_g: 30 },
          ],
        },
      ],
    }),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, unknown>) =>
      opts ? `${key} ${JSON.stringify(opts)}` : key,
  }),
}));

function renderWithQuery(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe('UsageProjection', () => {
  it('shows consumed-so-far and the split marker when segments exist', async () => {
    renderWithQuery(<UsageProjection printerId={1} printing />);
    await waitFor(() => {
      expect(screen.getByTestId('usage-projection')).toBeInTheDocument();
    });
    expect(screen.getByTestId('usage-projection').textContent).toContain('soFar');
    expect(screen.getByTestId('usage-projection').textContent).toContain('split');
  });

  it('renders nothing for an idle printer', () => {
    const { container } = renderWithQuery(<UsageProjection printerId={1} printing={false} />);
    expect(container.firstChild).toBeNull();
  });
});
