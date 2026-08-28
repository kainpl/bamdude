import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';
import { FamilyPicker } from '../../components/FamilyPicker';

vi.mock('../../api/client', async (importOriginal) => {
  const original = await importOriginal<typeof import('../../api/client')>();
  return {
    ...original,
    api: {
      ...original.api,
      getFilamentFamilies: vi.fn().mockResolvedValue([
        {
          filament_id: 'GFG99',
          ecosystem: 'bambu',
          alias: 'Generic PETG',
          vendor: 'Generic',
          filament_type: 'PETG',
          origin: 'system',
        },
        {
          filament_id: 'P122e532',
          ecosystem: 'bambu',
          alias: 'test PETG Basic',
          vendor: 'test',
          filament_type: 'PETG',
          origin: 'cloud_bambu',
        },
      ]),
      triggerFilamentPresetSync: vi.fn().mockResolvedValue({ queued: true }),
    },
  };
});

const renderPicker = (onChange = vi.fn()) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <FamilyPicker value={null} onChange={onChange} />
    </QueryClientProvider>,
  );
  return onChange;
};

describe('FamilyPicker', () => {
  it('lists families with alias and vendor, custom ones included', async () => {
    renderPicker();
    fireEvent.click(screen.getByRole('combobox'));
    await waitFor(() => expect(screen.getByText('Generic PETG')).toBeInTheDocument());
    expect(screen.getByText('test PETG Basic')).toBeInTheDocument();
  });

  it('reports the picked family id', async () => {
    const onChange = renderPicker();
    fireEvent.click(screen.getByRole('combobox'));
    await waitFor(() => screen.getByText('test PETG Basic'));
    fireEvent.click(screen.getByText('test PETG Basic'));
    expect(onChange).toHaveBeenCalledWith('P122e532', expect.objectContaining({ alias: 'test PETG Basic' }));
  });
});
