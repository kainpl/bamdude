/**
 * A snapshot tile shows when its frame arrived («Updated: …»). On a printer's tile
 * the open-card button holds the bottom-right corner, so the time sits left of it
 * rather than under it, as the clock alone (the sentence stays its accessible name),
 * and on a narrow tile it gives way to the printer's name. A tile without the button
 * (a kiosk) keeps the corner and the sentence, and so does every other surface of
 * `CameraSnapshotImage`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import { render } from '../utils';
import { CameraTile } from '../../components/CameraTile';
import { printerSource } from '../../utils/cameraSource';

function snapshotTile(onOpenPrinterCard?: () => void) {
  return render(
    <CameraTile
      source={printerSource(5)}
      name="Farm-5"
      mode="snapshot"
      snapshotIntervalMs={8000}
      connected
      statusMode="compact"
      printerState="IDLE"
      streamToken="wall-test"
      onOpenPrinterCard={onOpenPrinterCard}
    />,
  );
}

describe('the snapshot time on a cam wall tile', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response('jpeg', { status: 200 }))));
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = vi.fn(() => 'blob:snapshot');
      static revokeObjectURL = vi.fn();
    });
    Object.defineProperty(HTMLImageElement.prototype, 'decode', {
      configurable: true,
      value: vi.fn().mockResolvedValue(undefined),
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('sits left of the open-card button as the clock alone, and gives way on a narrow tile', async () => {
    snapshotTile(vi.fn());

    const time = await screen.findByLabelText(/^Updated: /);
    expect(time.textContent).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    expect(time).toHaveClass('right-11', '@max-[15rem]:hidden');
    expect(time).not.toHaveClass('right-2');
    // The name stops short of the time and the button; of the button alone where
    // the time gives way.
    expect(screen.getByText('Farm-5')).toHaveClass('pr-24', '@max-[15rem]:pr-9');
  });

  it('keeps the corner on a tile without the button', async () => {
    snapshotTile();

    const time = await screen.findByText(/^Updated: /);
    expect(time).toHaveClass('right-2');
    expect(time).not.toHaveClass('@max-[15rem]:hidden');
    expect(time).not.toHaveAttribute('aria-label');
    expect(screen.getByText('Farm-5')).toHaveClass('pr-36');
  });
});
