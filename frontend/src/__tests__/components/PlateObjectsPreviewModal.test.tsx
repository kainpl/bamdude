/**
 * Tests for the read-only plate object preview.
 *
 * The behaviours worth pinning are the ones a future edit would plausibly get
 * wrong: that the dialog never grows a skip control, that it still lists objects
 * when skipping is forbidden (the two axes are independent), and that a library
 * file opens on the first plate that actually holds something.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { PlateObjectsPreviewModal } from '../../components/PlateObjectsPreviewModal';
import { api } from '../../api/client';
import type { PlateMetadata } from '../../types/plates';

vi.mock('../../api/client', () => ({
  setAuthToken: vi.fn(),
  getAuthToken: vi.fn(() => 'test-admin-token'),
  api: {
    getPlateObjects: vi.fn(),
    getLibraryFilePlates: vi.fn(),
    getSettings: vi.fn().mockResolvedValue({}),
  },
}));

const payload = (over = {}) => ({
  plate_index: 1,
  objects: [
    { id: 941, name: 'bracket', x: 0.25, y: 0.25, norm: true },
    { id: 942, name: 'bracket', x: 0.75, y: 0.75, norm: true },
  ],
  bbox_all: null,
  positions_approximate: false,
  skip_objects_supported: false,
  has_top_view: true,
  ...over,
});

describe('PlateObjectsPreviewModal', () => {
  beforeEach(() => {
    // Call history, not just return values — the closed-dialog test asserts
    // that nothing was fetched, which the previous test's calls would defeat.
    vi.clearAllMocks();
    vi.mocked(api.getPlateObjects).mockResolvedValue(payload());
    vi.mocked(api.getLibraryFilePlates).mockResolvedValue({
      file_id: 1,
      filename: 'f.3mf',
      plates: [],
      is_multi_plate: false,
    });
  });

  it('lists the objects even when skipping is forbidden, and says why', async () => {
    render(<PlateObjectsPreviewModal source="archive" id={1} isOpen onClose={() => {}} />);
    // Each id appears twice on purpose: once as a plate marker, once as the
    // list's ID badge. Both show the raw identify_id the printer's own screen
    // uses, so they must agree.
    await waitFor(() => expect(screen.getAllByText('941')).toHaveLength(2));
    expect(screen.getAllByText('942')).toHaveLength(2);
    expect(screen.getByText(/Object skipping is unavailable/)).toBeInTheDocument();
  });

  it('says skipping is available when both slicer flags were on', async () => {
    vi.mocked(api.getPlateObjects).mockResolvedValue(payload({ skip_objects_supported: true }));
    render(<PlateObjectsPreviewModal source="archive" id={1} isOpen onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText(/Object skipping is available/)).toBeInTheDocument());
  });

  it('never renders a skip control', async () => {
    render(<PlateObjectsPreviewModal source="archive" id={1} isOpen onClose={() => {}} />);
    await waitFor(() => expect(screen.getAllByText('941')).toHaveLength(2));
    expect(screen.queryByRole('button', { name: /skip/i })).not.toBeInTheDocument();
    // Markers exist but are inert. Both instances carry the same name — copies
    // of one model share it in the slicer too; the id is what distinguishes
    // them — so this is getAll, not get.
    for (const marker of screen.getAllByLabelText('bracket')) {
      expect(marker).toBeDisabled();
    }
  });

  it('asks the archive endpoint without a plate parameter', async () => {
    render(<PlateObjectsPreviewModal source="archive" id={7} isOpen onClose={() => {}} />);
    await waitFor(() => expect(api.getPlateObjects).toHaveBeenCalled());
    // The archive answers for its own plate_index; a caller-chosen plate would
    // put another plate's object ids in front of a reprint.
    expect(api.getLibraryFilePlates).not.toHaveBeenCalled();
  });

  it('opens a library file on the first plate that has objects', async () => {
    vi.mocked(api.getLibraryFilePlates).mockResolvedValue({
      file_id: 9,
      filename: 'multi.3mf',
      is_multi_plate: true,
      // Only the two fields the plate strip and the auto-plate choice read.
      plates: [
        { index: 1, object_count: 0 },
        { index: 2, object_count: 0 },
        { index: 3, object_count: 4 },
      ] as PlateMetadata[],
    });
    render(<PlateObjectsPreviewModal source="library" id={9} isOpen onClose={() => {}} />);
    await waitFor(() => expect(api.getPlateObjects).toHaveBeenCalledWith('library', 9, 3));
  });

  it('shows the empty state rather than an empty grid', async () => {
    vi.mocked(api.getPlateObjects).mockResolvedValue(payload({ objects: [] }));
    render(<PlateObjectsPreviewModal source="archive" id={1} isOpen onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText(/No objects found on this plate/)).toBeInTheDocument());
  });

  it('suppresses the plate image entirely when the file has no top view', async () => {
    vi.mocked(api.getPlateObjects).mockResolvedValue(payload({ has_top_view: false }));
    render(<PlateObjectsPreviewModal source="archive" id={1} isOpen onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText(/no plate preview image/i)).toBeInTheDocument());
    // Markers over a ¾ render would sit convincingly on the wrong parts.
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });

  it('renders nothing when closed', () => {
    render(<PlateObjectsPreviewModal source="archive" id={1} isOpen={false} onClose={() => {}} />);
    // The shared render() wrapper mounts a toast viewport, so an empty-container
    // assertion would never hold — check for the dialog's own content instead.
    expect(screen.queryByText('Objects on the plate')).not.toBeInTheDocument();
    expect(api.getPlateObjects).not.toHaveBeenCalled();
  });
});
