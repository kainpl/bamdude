import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';

import { render } from '../utils';
import { ProjectPartsTable } from '../../components/ProjectPartsTable';
import { api } from '../../api/client';

const parts = [
  { name: 'Lid', name_key: 'lid', target_qty: 10, printed: 4, in_progress: 4, defective: 1, usable: 3, remaining: 7 },
  { name: 'stray.stl', name_key: 'stray.stl', target_qty: null, printed: 2, in_progress: 0, defective: 0, usable: 2, remaining: null },
];

describe('ProjectPartsTable', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders one row per part with the aggregates', async () => {
    vi.spyOn(api, 'getProjectParts').mockResolvedValue({ parts });

    render(<ProjectPartsTable projectId={1} canEdit />);

    expect(await screen.findByText('Lid')).toBeInTheDocument();
    expect(screen.getByTestId('part-remaining-lid').textContent).toContain('7');
  });

  it('a history row without a target shows a dash', async () => {
    vi.spyOn(api, 'getProjectParts').mockResolvedValue({ parts });

    render(<ProjectPartsTable projectId={1} canEdit />);

    expect((await screen.findByTestId('part-target-stray.stl')).textContent).toContain('—');
  });

  it('editing a target saves through the API', async () => {
    vi.spyOn(api, 'getProjectParts').mockResolvedValue({ parts });
    const update = vi.spyOn(api, 'updateProjectParts').mockResolvedValue({ parts });

    render(<ProjectPartsTable projectId={1} canEdit />);

    const input = (await screen.findByTestId('part-target-input-lid')) as HTMLInputElement;
    fireEvent.change(input, { target: { value: '12' } });
    fireEvent.blur(input);

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith(1, [{ name_key: 'lid', target_qty: 12 }])
    );
  });

  it('renders nothing when the project has no parts at all', async () => {
    vi.spyOn(api, 'getProjectParts').mockResolvedValue({ parts: [] });

    render(<ProjectPartsTable projectId={1} canEdit />);

    await waitFor(() => expect(api.getProjectParts).toHaveBeenCalled());
    // The harness's ToastProvider always renders its own viewport div, so
    // firstChild isn't a reliable null-check here — assert the table itself
    // never mounts instead.
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });
});
