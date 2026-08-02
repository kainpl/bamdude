import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { PrinterLocationSelect } from '../components/PrinterLocationSelect';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock('../api/client', () => ({
  api: {
    getPrinterLocations: vi.fn().mockResolvedValue({
      locations: [{ id: 1, name: 'Shop 2', printer_count: 0, sensor_count: 0, queued_count: 0 }],
    }),
    createPrinterLocation: vi.fn(),
  },
}));

function renderIt(value: number | null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <PrinterLocationSelect value={value} onChange={() => {}} />
    </QueryClientProvider>,
  );
}

describe('PrinterLocationSelect', () => {
  it('offers what already exists instead of a text box', async () => {
    renderIt(null);

    expect(await screen.findByRole('option', { name: 'Shop 2' })).toBeInTheDocument();
  });

  it('has an explicit "no location" choice, since a printer without one is normal', async () => {
    renderIt(null);

    expect(await screen.findByRole('option', { name: 'printers.ungrouped' })).toBeInTheDocument();
  });

  it('shows the place a printer is already in', async () => {
    renderIt(1);

    // The select exists before its options do, so waiting on the option is
    // what makes this assert the selected value rather than the loading state.
    await screen.findByRole('option', { name: 'Shop 2' });

    expect(screen.getByRole('combobox')).toHaveValue('1');
  });
});
