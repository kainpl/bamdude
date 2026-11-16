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
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import { render } from '../utils';
import { api, type SlicerHealth } from '../../api/client';
import { SlicerOutdatedNote } from '../../components/SlicerOutdatedNote';
import { SliceModal } from '../../components/SliceModal';
import { SliceJobTrackerProvider } from '../../contexts/SliceJobTrackerContext';
import i18n from '../../i18n';
import { server } from '../mocks/server';

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

  it('says it in Ukrainian too, with the same command', async () => {
    health({ orcaslicer: older });
    const english = i18n.t('slicerHealth.sidecarOutdated');
    await i18n.changeLanguage('uk');
    try {
      render(<SlicerOutdatedNote kind="orcaslicer" />);
      const note = await screen.findByRole('status');
      expect(note.textContent).toContain('docker compose --profile all build');
      expect(note.textContent).not.toBe(english);
      // a translation, not the key and not the English fallback
      expect(note.textContent).toMatch(/[а-яіїє]/i);
    } finally {
      await i18n.changeLanguage('en');
    }
  });
});

describe('SliceModal — the slicer picker says when the picked container is older', () => {
  const none = { printer: [], process: [], filament: [] };

  it('under the cards, for the picked slicer, and not once the other one is picked', async () => {
    const user = userEvent.setup();
    localStorage.setItem('bamdude:slicer-pick:libraryFile:1', 'orcaslicer');
    health({ orcaslicer: older, bambu_studio: { ...current, url: 'http://bambu:3001' } });
    server.use(
      http.get('*/api/v1/library/files/:id/plates', () => HttpResponse.json({ plates: [{ index: 1, name: 'Plate 1' }] })),
      http.get('*/api/v1/library/files/:id/filament-requirements', () =>
        HttpResponse.json({ file_id: 1, filename: 'Cube.3mf', filaments: [] }),
      ),
      http.get('*/api/v1/slicer/presets', () =>
        HttpResponse.json({
          local: {
            printer: [{ id: 'p', name: 'Bambu Lab X1 Carbon 0.4 nozzle', source: 'local' }],
            process: [{ id: 'q', name: '0.20mm Mine', source: 'local' }],
            filament: [{ id: 'f', name: 'My PLA', source: 'local', filament_type: 'PLA' }],
          },
          orca_cloud: none,
          cloud: none,
          standard: none,
          cloud_status: 'ok',
          orca_cloud_status: 'ok',
        }),
      ),
      http.get('*/api/v1/slicer/printer-models', () => HttpResponse.json({})),
      http.get('*/api/v1/slicer/preset-values', () => HttpResponse.json({ resolved: false, values: {}, reason: 'x' })),
      http.get('*/api/v1/slicer-pipelines/', () => HttpResponse.json({ pipelines: [] })),
    );
    try {
      render(
        <SliceJobTrackerProvider>
          <SliceModal source={{ kind: 'libraryFile', id: 1, filename: 'Cube.3mf' }} onClose={() => undefined} />
        </SliceJobTrackerProvider>,
      );

      const picker = (await screen.findByText('Slice with')).closest('fieldset') as HTMLElement;
      const note = await within(picker).findByText(/older than this BamDude needs/);
      expect(note).toBeInTheDocument();
      // It informs and never blocks: the dialog is still usable.
      await waitFor(() => expect(screen.getByRole('button', { name: 'Slice' })).toBeEnabled());

      await user.click(within(picker).getByRole('button', { name: /BambuStudio/ }));

      await waitFor(() => expect(within(picker).queryByText(/older than this BamDude needs/)).toBeNull());
    } finally {
      localStorage.clear();
    }
  });
});
