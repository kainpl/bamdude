import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { UsageProjection } from '../../components/UsageProjection';
import { api } from '../../api/client';

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
    // the split marker is an icon now — its text lives in the tooltip/label
    expect(screen.getByLabelText('printers.usageProjection.split')).toBeInTheDocument();
  });

  it('renders nothing for an idle printer', () => {
    const { container } = renderWithQuery(<UsageProjection printerId={1} printing={false} />);
    expect(container.firstChild).toBeNull();
  });

  it('does not show the prior run when the same printer starts a new archive', async () => {
    const priorRun = await api.getUsageProjection(1);
    let finishNewRun!: (value: typeof priorRun) => void;
    vi.mocked(api.getUsageProjection)
      .mockResolvedValueOnce(priorRun)
      .mockImplementationOnce(() => new Promise((resolve) => { finishNewRun = resolve; }));

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { rerender } = render(
      <QueryClientProvider client={client}>
        <UsageProjection printerId={1} printing archiveId={5} />
      </QueryClientProvider>,
    );
    await screen.findByTestId('usage-projection');

    rerender(
      <QueryClientProvider client={client}>
        <UsageProjection printerId={1} printing archiveId={6} />
      </QueryClientProvider>,
    );
    expect(screen.queryByTestId('usage-projection')).toBeNull();
    finishNewRun({ ...priorRun, archive_id: 6 });
    await screen.findByTestId('usage-projection');
  });

  it('stays blank until a late archive attaches, then shows that run only', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tree = (archiveId: number | null) => <QueryClientProvider client={client}>
      <UsageProjection printerId={1} printing archiveId={archiveId} />
    </QueryClientProvider>;
    const view = render(tree(null));
    expect(screen.queryByTestId('usage-projection')).toBeNull();
    view.rerender(tree(5));
    await screen.findByTestId('usage-projection');
    view.rerender(<QueryClientProvider client={client}>
      <UsageProjection printerId={1} printing={false} archiveId={5} />
    </QueryClientProvider>);
    expect(screen.queryByTestId('usage-projection')).toBeNull();
  });
});
