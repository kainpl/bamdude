import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { api } from '../../../api/client';
import { SlicerChoice } from '../../../components/slicing-setup/SlicerChoice';

// The family's slicer cards (R7): live health, the outdated note, the selected
// card announced, offline cards disabled.
function renderChoice(health: { orca: boolean; bambu: boolean; outdated?: boolean }, value: 'orcaslicer' | 'bambu_studio' | null) {
  vi.spyOn(api, 'getSlicerHealth').mockImplementation(async (kind) => ({
    healthy: kind === 'orcaslicer' ? health.orca : health.bambu,
    version: '2.3',
    error: null,
    sidecar_outdated: Boolean(health.outdated),
  }) as never);
  const onChange = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <SlicerChoice value={value} onChange={onChange} />
    </QueryClientProvider>,
  );
  return onChange;
}

afterEach(() => vi.restoreAllMocks());

describe('SlicerChoice', () => {
  it('offline card disabled', async () => {
    renderChoice({ orca: true, bambu: false }, 'orcaslicer');
    await waitFor(() => expect(screen.getByRole('button', { name: /OrcaSlicer/ })).toBeEnabled());
    expect(screen.getByRole('button', { name: /BambuStudio/ })).toBeDisabled();
  });

  it('aria-pressed on the selected card', async () => {
    renderChoice({ orca: true, bambu: true }, 'bambu_studio');
    await waitFor(() => expect(screen.getByRole('button', { name: /BambuStudio/ })).toHaveAttribute('aria-pressed', 'true'));
    expect(screen.getByRole('button', { name: /OrcaSlicer/ })).toHaveAttribute('aria-pressed', 'false');
  });

  it('outdated note under the cards', async () => {
    renderChoice({ orca: true, bambu: true, outdated: true }, 'orcaslicer');
    await waitFor(() => expect(screen.getByRole('status')).toBeInTheDocument());
  });

  it('keyboard: Tab reaches each healthy card', async () => {
    const onChange = renderChoice({ orca: true, bambu: true }, null);
    await waitFor(() => expect(screen.getByRole('button', { name: /OrcaSlicer/ })).toBeEnabled());
    const user = userEvent.setup();
    await user.tab();
    expect(screen.getByRole('button', { name: /OrcaSlicer/ })).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(onChange).toHaveBeenCalledWith('orcaslicer');
    await user.tab();
    expect(screen.getByRole('button', { name: /BambuStudio/ })).toHaveFocus();
  });
});
