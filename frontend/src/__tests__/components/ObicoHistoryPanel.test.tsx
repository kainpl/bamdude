import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { api, ApiError, type ObicoEventDetail } from '../../api/client';
import { ObicoEvidenceViewer, ObicoHistoryPanel } from '../../components/ObicoHistoryPanel';

const access = vi.hoisted(() => ({ permissions: new Set(['printers:read', 'camera:view', 'settings:update']) }));
vi.mock('../../contexts/AuthContext', async original => ({ ...await original<typeof import('../../contexts/AuthContext')>(),
  useAuth: () => ({ hasPermission: (value: string) => access.permissions.has(value) }) }));

const event = { id: 'event-1', printer_id: 1, state: 'ready' as const, class: 'warning' as const,
  task_name: 'Long job name '.repeat(10).trim(), timestamp: '2026-11-03T01:00:00Z', score: 1.55, current_p: 1.55, detections: 1 };
const detail: ObicoEventDetail = { ...event, width: 1280, height: 720, profile: 'obico_center_xywh_server_returned_v1',
  invalid_boxes: 0, raw_boxes: [['box', .85, [640, 360, 256, 256]]],
  boxes: [{ label: '<unsafe>', confidence: .85, xyxy: [.4, 232 / 720, .6, 488 / 720] }],
  facts: { frame_source: 'live-buffer', buffer_received_at: '2026-11-03T00:59:50Z', captured_at: null } };

beforeEach(() => {
  vi.restoreAllMocks();
  access.permissions = new Set(['printers:read', 'camera:view', 'settings:update']);
  vi.spyOn(api, 'getObicoHistory').mockResolvedValue({ items: [event], next_cursor: null, saved_count: 1, total_count: 7,
    oldest_saved_at: event.timestamp });
  vi.spyOn(api, 'getObicoEvent').mockResolvedValue(detail);
  vi.spyOn(api, 'getObicoEventImage').mockResolvedValue(new Blob(['jpeg'], { type: 'image/jpeg' }));
  vi.spyOn(api, 'clearObicoHistory').mockResolvedValue({ deleted: 7 });
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:evidence') });
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
  vi.stubGlobal('CSS', { ...globalThis.CSS, supports: vi.fn(() => true) });
});
afterEach(() => vi.unstubAllGlobals());

function panel(recent = [event]) {
  return render(<ObicoHistoryPanel recent={recent} printers={[{ id: 1, name: 'Printer one' }]} onRefresh={vi.fn()} />);
}

describe('saved Obico evidence', () => {
  it('returns to the first page once when a cursor expires or the session changes', async () => {
    vi.mocked(api.getObicoHistory).mockImplementation(async ({ cursor } = {}) => {
      if (cursor) throw new ApiError('History changed', 409);
      return { items: [event], next_cursor: 'expired-token', saved_count: 2, total_count: 2, oldest_saved_at: event.timestamp };
    });
    panel();
    fireEvent.click(screen.getByRole('tab', { name: 'Saved frames' }));
    const next = screen.getByRole('button', { name: 'Next' });
    await waitFor(() => expect(next).toBeEnabled());
    fireEvent.click(next);
    await waitFor(() => expect(api.getObicoHistory).toHaveBeenCalledWith({ cursor: 'expired-token' }));
    await waitFor(() => expect(api.getObicoHistory).toHaveBeenCalledTimes(3)); // first page, refused cursor, fresh first page
    await waitFor(() => expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled());
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('hides boxes explicitly when encoded orientation is unsupported', async () => {
    vi.mocked(CSS.supports).mockReturnValue(false);
    render(<ObicoEvidenceViewer selection={{ printer: 1, id: 'event-1' }} allowed onClose={vi.fn()} />);
    await screen.findByRole('img');
    expect(screen.getByRole('status')).toHaveTextContent(/orientation/);
    expect(screen.getByRole('checkbox')).toBeDisabled();
    expect(document.querySelector('svg rect')).toBeNull();
  });
  it('does not fetch a frame until the operator opens it; preserves encoded geometry and box confidence', async () => {
    panel();
    expect(api.getObicoEventImage).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'View detection' }));
    const image = await screen.findByRole('img');
    expect(image).toHaveStyle({ imageOrientation: 'none' });
    const rect = document.querySelector('svg rect')!;
    expect(Number(rect.getAttribute('y'))).toBeCloseTo(232);
    expect(Number(rect.getAttribute('height'))).toBeCloseTo(256); // no ONNX/RKNN guessing
    expect(screen.getByText('85.0%')).toBeInTheDocument();
    expect(screen.getByText('<unsafe>')).toBeInTheDocument();
    expect(document.querySelector('unsafe')).toBeNull();
    expect(screen.getByText(/exposure time is unknown/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Show detection boxes' }));
    expect(document.querySelector('svg rect')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:evidence');
  });

  it('revokes old media on event switch and unmount', async () => {
    const view = render(<ObicoEvidenceViewer selection={{ printer: 1, id: 'event-1' }} allowed onClose={vi.fn()} />);
    await screen.findByRole('img');
    view.rerender(<ObicoEvidenceViewer selection={{ printer: 1, id: 'event-2' }} allowed onClose={vi.fn()} />);
    await waitFor(() => expect(api.getObicoEventImage).toHaveBeenCalledTimes(2));
    await screen.findByRole('img');
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2);
  });

  it('does not create a URL from a late response after close', async () => {
    let finish!: (value: Blob) => void;
    vi.mocked(api.getObicoEventImage).mockReturnValue(new Promise(resolve => { finish = resolve; }));
    const view = render(<ObicoEvidenceViewer selection={{ printer: 1, id: 'event-1' }} allowed onClose={vi.fn()} />);
    await waitFor(() => expect(api.getObicoEventImage).toHaveBeenCalledOnce());
    view.unmount(); finish(new Blob(['late']));
    await Promise.resolve();
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it('drops media when permission is revoked and never asks for another capture', async () => {
    const view = render(<ObicoEvidenceViewer selection={{ printer: 1, id: 'event-1' }} allowed onClose={vi.fn()} />);
    await screen.findByRole('img');
    view.rerender(<ObicoEvidenceViewer selection={{ printer: 1, id: 'event-1' }} allowed={false} onClose={vi.fn()} />);
    expect(screen.getByText(/do not have permission/)).toBeInTheDocument();
    expect(screen.queryByRole('img')).toBeNull();
    expect(URL.revokeObjectURL).toHaveBeenCalledOnce();
    expect(api.getObicoEventImage).toHaveBeenCalledOnce();
  });

  it('handles an expired or forbidden image without a second attempt', async () => {
    vi.mocked(api.getObicoEventImage).mockRejectedValue(new Error('404'));
    render(<ObicoEvidenceViewer selection={{ printer: 1, id: 'event-1' }} allowed onClose={vi.fn()} />);
    expect(await screen.findByRole('alert')).toHaveTextContent(/no longer available/);
    expect(api.getObicoEventImage).toHaveBeenCalledOnce();
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it('uses the server count for Clear and sends nothing on Cancel', async () => {
    panel();
    fireEvent.click(screen.getByRole('tab', { name: 'Saved frames' }));
    expect(await screen.findByText(/Oldest saved frame/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Clear saved history' }));
    expect(await screen.findByText(/Delete all 7 saved events/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(api.clearObicoHistory).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Clear saved history' }));
    await screen.findByText(/Delete all 7 saved events/);
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(api.clearObicoHistory).toHaveBeenCalledOnce());
  });

  it('hides viewing and clearing without their rights, but keeps metadata', async () => {
    access.permissions = new Set(['printers:read']);
    panel();
    fireEvent.click(screen.getByRole('tab', { name: 'Saved frames' }));
    expect(await screen.findByTitle(event.task_name)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'View detection' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Clear saved history' })).toBeNull();
    expect(api.getObicoEvent).not.toHaveBeenCalled();
  });

  it('shows recorder failures separately and uses the cursor only for saved pages', async () => {
    vi.mocked(api.getObicoHistory).mockResolvedValue({ items: [event], next_cursor: 'next-token', saved_count: 2, total_count: 2,
      oldest_saved_at: event.timestamp });
    render(<ObicoHistoryPanel recent={[{ ...event, state: 'not_saved' }]} printers={[]}
      storage={{ state: 'error', reason: 'write_failed', dropped: { write_failed: 1 }, used_bytes: 0, max_bytes: 1e9, max_events: 10000, max_days: 15 }} onRefresh={vi.fn()} />);
    expect(screen.getByRole('status')).toHaveTextContent(/Live detection and its actions continue/);
    expect(screen.getByText('Frame not saved')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: 'Saved frames' }));
    const next = await screen.findByRole('button', { name: 'Next' });
    await waitFor(() => expect(next).toBeEnabled()); fireEvent.click(next);
    await waitFor(() => expect(api.getObicoHistory).toHaveBeenCalledWith({ cursor: 'next-token' }));
    fireEvent.click(screen.getByRole('tab', { name: 'Recent signals' }));
    expect(screen.getByText('Frame not saved')).toBeInTheDocument();
  });
});
