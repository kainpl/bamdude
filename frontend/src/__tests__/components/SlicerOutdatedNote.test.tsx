/**
 * The Slice dialog says when the slicer container is older than this BamDude
 * needs (G12, decision 6).
 *
 * The slicer containers are built from our sidecar fork whenever the operator
 * rebuilds them — on their own schedule, whatever BamDude they serve. The
 * health answer carries `sidecar_outdated`; the note under the slicer cards
 * turns it into the one action that fixes it. An older container is still a
 * working one, so the note informs and never blocks.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';

import { render } from '../utils';
import { api, type SlicerHealth } from '../../api/client';
import { SlicerOutdatedNote } from '../../components/SlicerOutdatedNote';
import modalSource from '../../components/SliceModal.tsx?raw';
import en from '../../i18n/locales/en';
import uk from '../../i18n/locales/uk';

function health(answers: Partial<Record<'orcaslicer' | 'bambu_studio', SlicerHealth>>) {
  vi.spyOn(api, 'getSlicerHealth').mockImplementation(async (slicer) => {
    return answers[slicer] ?? { healthy: false, url: null, error: 'not configured' };
  });
}

const current: SlicerHealth = {
  healthy: true,
  url: 'http://orca:3003',
  version: '2.5.0',
  sidecar_version: 1,
  sidecar_required: 1,
  sidecar_outdated: false,
};
const older: SlicerHealth = { ...current, sidecar_version: null, sidecar_outdated: true };

afterEach(() => {
  vi.restoreAllMocks();
});

describe('SlicerOutdatedNote', () => {
  it('tells the operator to rebuild the containers when the picked slicer is older', async () => {
    health({ orcaslicer: older });
    render(<SlicerOutdatedNote kind="orcaslicer" />);
    const note = await screen.findByRole('status');
    expect(note.textContent).toContain('older than this BamDude needs');
    expect(note.textContent).toContain('docker compose --profile all build');
  });

  it('says nothing for a current container', async () => {
    health({ orcaslicer: current });
    render(<SlicerOutdatedNote kind="orcaslicer" />);
    await waitFor(() => expect(api.getSlicerHealth).toHaveBeenCalled());
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('speaks for the slicer that is picked, not the other one', async () => {
    health({ orcaslicer: older, bambu_studio: current });
    render(<SlicerOutdatedNote kind="bambu_studio" />);
    await waitFor(() => expect(api.getSlicerHealth).toHaveBeenCalledWith('bambu_studio'));
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('says nothing about a container that is offline', async () => {
    // "Rebuild" is advice for a container that answered; the card already says Offline.
    health({ orcaslicer: { healthy: false, url: 'http://orca:3003', error: 'unreachable' } });
    render(<SlicerOutdatedNote kind="orcaslicer" />);
    await waitFor(() => expect(api.getSlicerHealth).toHaveBeenCalled());
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('says nothing while no slicer is picked', () => {
    health({ orcaslicer: older });
    render(<SlicerOutdatedNote kind={null} />);
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('is part of the slicer picker of the Slice dialog', () => {
    expect(modalSource).toContain('<SlicerOutdatedNote kind={value} />');
  });

  it('has its sentence in both languages', () => {
    expect(en.slicerHealth.sidecarOutdated).toContain('docker compose --profile all build');
    expect(uk.slicerHealth.sidecarOutdated).toContain('docker compose --profile all build');
    expect(uk.slicerHealth.sidecarOutdated).not.toBe(en.slicerHealth.sidecarOutdated);
  });
});
