import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import { OrderModal } from '../../../components/projects/OrderModal';

// A full `Order` as the detail page would pass it — `due_date` is a datetime
// string (backend `ProjectResponse.due_date` is `datetime`), never a bare
// `YYYY-MM-DD`.
const order = {
  id: 5,
  name: 'Ten flasks',
  customer_id: 2,
  customer_name: 'ACME',
  description: 'Existing description',
  color: '#00ae42',
  status: 'active',
  notes: null,
  attachments: null,
  tags: null,
  due_date: '2026-09-10T00:00:00',
  priority: 'normal',
  price: 120,
  url: 'https://example.com',
  cover_image_filename: null,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
  lines: [],
  procurement: [],
  figures: {},
  other_archive_ids: [],
} as never;

describe('OrderModal', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getCustomers').mockResolvedValue([{ id: 2, name: 'ACME', figures: {} }] as never);
  });

  it('shows the stored due date and sends no due_date when the field is untouched', async () => {
    const update = vi.spyOn(api, 'updateOrder').mockResolvedValue(order);
    render(<OrderModal order={order} onClose={() => {}} />);

    // The API sends a full datetime; the date input must show only the date part.
    expect(screen.getByLabelText(/due date/i)).toHaveValue('2026-09-10');

    fireEvent.click(screen.getByRole('button', { name: /save/i }));

    // Nothing was touched, so the PATCH body carries no fields at all —
    // in particular no `due_date`, which a raw-datetime-vs-trimmed-date
    // comparison would have flagged as "changed".
    await waitFor(() => expect(update).toHaveBeenCalledWith(5, {}));
  });
});
