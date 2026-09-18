import { StrictMode } from 'react';
import { act, fireEvent, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render } from '../utils';
import { EmbeddedCameraViewer } from '../../components/EmbeddedCameraViewer';
import { printerSource } from '../../utils/cameraSource';

describe('floating camera lifecycle', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(global, 'fetch').mockResolvedValue(new Response('{}'));
  });
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });
  const viewer = (id = 42) => <EmbeddedCameraViewer key={id} source={printerSource(id)} name={`Camera ${id}`} onClose={() => {}} />;
  const image = () => screen.getByAltText('Camera stream') as HTMLImageElement;
  const settle = () => act(async () => { await Promise.resolve(); });

  it('cancels the replacement stream after refresh then unmount, including StrictMode', async () => {
    const { unmount } = render(<StrictMode>{viewer()}</StrictMode>);
    await settle();
    const first = image();
    expect(first.src).toContain('/camera/stream?');
    fireEvent.load(first);
    fireEvent.click(screen.getByTitle('Refresh stream'));
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    const replacement = image();
    expect(replacement).not.toBe(first);
    expect(first.src).toMatch(/^data:image\/gif;/);
    expect(replacement.src).toContain('/camera/stream?');
    unmount();
    expect(replacement.src).toMatch(/^data:image\/gif;/);
  });

  it('cancels on minimize, restores on expand and releases the replacement on close', async () => {
    const { unmount } = render(viewer());
    await settle();
    const first = image();
    fireEvent.click(screen.getByTitle('Minimize'));
    expect(first.src).toMatch(/^data:image\/gif;/);
    expect(screen.queryByAltText('Camera stream')).toBeNull();
    fireEvent.click(screen.getByTitle('Expand'));
    const restored = image();
    expect(restored.src).toContain('/camera/stream?');
    unmount();
    expect(restored.src).toMatch(/^data:image\/gif;/);
  });

  it('cancels the previous printer when the single popup switches its key', async () => {
    const { rerender } = render(viewer(1));
    await settle();
    const old = image();
    rerender(viewer(2));
    await settle();
    expect(old.src).toMatch(/^data:image\/gif;/);
    expect(screen.getAllByAltText('Camera stream')).toHaveLength(1);
    expect(image().src).toContain('/printers/2/');
  });
});
