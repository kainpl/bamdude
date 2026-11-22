import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { api, type SlicerPipeline } from '../../../api/client';
import { PipelineSelect } from '../../../components/slicing-setup/PipelineSelect';

// R11, §9: sets are applied only with pipelines:read; saving needs write too.
const auth = { permissions: new Set<string>() };
vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({ hasPermission: (p: string) => auth.permissions.has(p) }),
}));

const pipeline = (id: number, name: string) => ({ id, name, printer_preset: { source: 'standard', id: 'P' } }) as SlicerPipeline;

function renderSelect(props: Partial<Parameters<typeof PipelineSelect>[0]> = {}) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <PipelineSelect onApply={() => {}} {...props} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  auth.permissions = new Set(['pipelines:read']);
});
afterEach(() => vi.restoreAllMocks());

describe('PipelineSelect', () => {
  it('no pipelines request without pipelines:read', async () => {
    auth.permissions = new Set();
    const spy = vi.spyOn(api, 'listSlicerPipelines').mockResolvedValue({ pipelines: [] } as never);
    const { container } = renderSelect();
    await new Promise((r) => setTimeout(r, 20));
    expect(spy).not.toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
  });

  it('save needs read and write', async () => {
    vi.spyOn(api, 'listSlicerPipelines').mockResolvedValue({ pipelines: [] } as never);
    const save = { onSave: async () => {}, pending: false, canSave: true };
    const { unmount } = renderSelect({ save });
    await waitFor(() => expect(screen.getByRole('combobox')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /save these settings/i })).toBeNull();
    unmount();
    auth.permissions = new Set(['pipelines:read', 'pipelines:write']);
    renderSelect({ save });
    expect(await screen.findByRole('button', { name: /save these settings/i })).toBeInTheDocument();
  });

  it('filterSets narrows the list', async () => {
    vi.spyOn(api, 'listSlicerPipelines').mockResolvedValue({ pipelines: [pipeline(1, 'keep'), pipeline(2, 'drop')] } as never);
    renderSelect({ filterSets: (s) => s.name === 'keep' });
    await waitFor(() => expect(screen.getByRole('option', { name: 'keep' })).toBeInTheDocument());
    expect(screen.queryByRole('option', { name: 'drop' })).toBeNull();
  });

  it('the save row closes only after the save resolves', async () => {
    auth.permissions = new Set(['pipelines:read', 'pipelines:write']);
    vi.spyOn(api, 'listSlicerPipelines').mockResolvedValue({ pipelines: [] } as never);
    let fail = true;
    const onSave = vi.fn(() => (fail ? Promise.reject(new Error('no')) : Promise.resolve()));
    renderSelect({ save: { onSave, pending: false, canSave: true } });
    const user = (await import('@testing-library/user-event')).default.setup();
    await user.click(await screen.findByRole('button', { name: /save these settings/i }));
    await user.type(screen.getByRole('textbox'), 'Set A');
    await user.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith('Set A'));
    expect(screen.getByRole('textbox')).toHaveValue('Set A');
    fail = false;
    await user.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.queryByRole('textbox')).toBeNull());
  });
});
