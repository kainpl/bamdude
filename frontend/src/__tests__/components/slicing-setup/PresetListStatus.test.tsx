import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { UnifiedPresetsResponse } from '../../../api/client';
import { PresetListStatus } from '../../../components/slicing-setup/PresetListStatus';
import type { PresetCatalogue } from '../../../components/slicing-setup/usePresetCatalogue';

// R10: the cloud banners say only what they know; a status they do not know
// (an older server sends none) shows nothing rather than breaking the form.
const catalogue = (data: Partial<UnifiedPresetsResponse>): PresetCatalogue =>
  ({ data: data as UnifiedPresetsResponse, isLoading: false, isError: false, refresh: vi.fn(), refreshing: false }) as PresetCatalogue;

describe('PresetListStatus', () => {
  it('a missing or unknown status shows no banner', () => {
    render(<PresetListStatus catalogue={catalogue({ cloud_status: 'weird' as never })} />);
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeInTheDocument();
  });

  it('an expired sign-in is said', () => {
    render(<PresetListStatus catalogue={catalogue({ cloud_status: 'expired', orca_cloud_status: 'ok' })} />);
    expect(screen.getByRole('status')).toHaveTextContent(/Bambu Cloud/);
  });
});
