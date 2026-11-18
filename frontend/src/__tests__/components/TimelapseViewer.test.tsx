/**
 * The timelapse viewer and editor keep a portrait video on the screen (G10 G, M31).
 *
 * jsdom lays nothing out, so what is pinned here is that both components give
 * their video the capped box from `timelapse/videoBox` and nothing that undoes
 * it. The boxes themselves are measured in a real browser — see the unit's
 * evidence package.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { TimelapseViewer } from '../../components/TimelapseViewer';
import { TimelapseEditorModal } from '../../components/TimelapseEditorModal';
import { TIMELAPSE_EDITOR_VIDEO_CLASS, TIMELAPSE_VIEWER_VIDEO_CLASS } from '../../components/timelapse/videoBox';

function classes(value: string): string[] {
  return value.split(/\s+/).filter(Boolean);
}

describe('the video boxes', () => {
  it.each([
    ['viewer', TIMELAPSE_VIEWER_VIDEO_CLASS],
    ['editor', TIMELAPSE_EDITOR_VIDEO_CLASS],
  ])('the %s box is capped in height and keeps the whole picture', (_name, value) => {
    const list = classes(value);
    expect(list).toContain('w-full');
    expect(list).toContain('object-contain');
    expect(list.some((item) => item.startsWith('max-h-'))).toBe(true);
    // Nothing that would let the video take its own height again or crop it.
    expect(list.some((item) => item.startsWith('h-') || item === 'object-cover' || item === 'aspect-video')).toBe(false);
  });

  it('the viewer leaves room for its controls inside a window that is at most 90vh tall', () => {
    expect(classes(TIMELAPSE_VIEWER_VIDEO_CLASS)).toContain('max-h-[calc(90vh-16rem)]');
    // Wider windows keep their controls on one line and give the difference to the video.
    expect(classes(TIMELAPSE_VIEWER_VIDEO_CLASS)).toContain('sm:max-h-[calc(90vh-12rem)]');
  });
});

describe('TimelapseViewer', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('gives its video the capped box and lets its controls wrap', () => {
    render(<TimelapseViewer src="/video.mp4" title="Lamp" downloadFilename="lamp.mp4" onClose={vi.fn()} />);
    const video = screen.getByTestId('timelapse-viewer-video');
    expect(classes(video.className)).toEqual(classes(TIMELAPSE_VIEWER_VIDEO_CLASS));
    expect(video.getAttribute('style')).toBeNull();
    expect(classes(screen.getByTestId('timelapse-viewer-controls').className)).toContain('flex-wrap');
  });
});

describe('TimelapseEditorModal', () => {
  it('gives its video the capped box', async () => {
    server.use(
      http.get('/api/v1/archives/5/timelapse/info', () =>
        HttpResponse.json({ duration: 10, width: 1080, height: 1920, fps: 30, codec: 'h264', file_size: 1, has_audio: false }),
      ),
      http.get('/api/v1/archives/5/timelapse/thumbnails', () => HttpResponse.json({ thumbnails: [], timestamps: [] })),
      http.get('/api/v1/archives/5/timelapse/edit-jobs', () => HttpResponse.json({ jobs: [] })),
    );
    render(<TimelapseEditorModal archiveId={5} timelapseSrc="/video.mp4" onClose={vi.fn()} />);
    const video = await waitFor(() => screen.getByTestId('timelapse-editor-video'));
    expect(classes(video.className)).toEqual(classes(TIMELAPSE_EDITOR_VIDEO_CLASS));
    expect(video.getAttribute('style')).toBeNull();
  });
});
