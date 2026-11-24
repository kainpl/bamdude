import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { api } from '../../api/client';
import { SpoolLabelScanner } from '../../components/SpoolLabelScanner';

const decode = vi.hoisted(() => vi.fn());
vi.mock('jsqr', () => ({ default: decode }));
vi.mock('../../api/client', () => ({
  getAuthToken: vi.fn(),
  setAuthToken: vi.fn(),
  api: { getAuthStatus: vi.fn().mockResolvedValue({ auth_enabled: false }), getSpool: vi.fn() },
}));

describe('spool label scanner', () => {
  const key = 'a'.repeat(32);
  const qr = `https://printed.example/inventory?spool=42&src=local&v=2&key=${key}`;

  beforeEach(() => {
    decode.mockReset();
    vi.mocked(api.getSpool).mockReset();
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:test');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    function canvasContext(this: HTMLCanvasElement) {
      return {
        drawImage: vi.fn(),
        getImageData: () => ({ data: new Uint8ClampedArray(), width: this.width, height: this.height }),
      };
    }
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
      canvasContext as unknown as typeof HTMLCanvasElement.prototype.getContext,
    );
    vi.stubGlobal('Image', class {
      naturalWidth = 4000;
      naturalHeight = 3000;
      onload?: () => void;
      set src(_url: string) { this.onload?.(); }
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  const photo = () => {
    const input = document.querySelector('input[type="file"]')!;
    fireEvent.change(input, { target: { files: [new File(['image'], 'label.jpg', { type: 'image/jpeg' })] } });
  };

  it('retries a large photo at a bounded higher resolution and resolves its proof through the local API', async () => {
    const spool = { id: 42, material: 'PLA' } as Awaited<ReturnType<typeof api.getSpool>>;
    vi.mocked(api.getSpool).mockResolvedValue(spool);
    decode.mockReturnValueOnce(null).mockReturnValueOnce({ data: qr });
    const onScan = vi.fn();
    render(<SpoolLabelScanner target="A1" onScan={onScan} onClose={vi.fn()} />);
    photo();

    await waitFor(() => expect(onScan).toHaveBeenCalledWith({ spool, key }));
    expect(api.getSpool).toHaveBeenCalledWith(42, key);
    expect(decode.mock.calls.map(([, width, height]) => [width, height])).toEqual([[640, 480], [2400, 1800]]);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:test');
  });

  it.each([
    'https://printed.example/inventory?spool=42',
    `${qr}&spool=43`,
    qr.replace('src=local', 'src=spoolman'),
  ])('requires an unambiguous local proof in %s', async (payload) => {
    decode.mockReturnValue({ data: payload });
    const onScan = vi.fn();
    render(<SpoolLabelScanner target="A1" onScan={onScan} onClose={vi.fn()} />);
    photo();

    expect(await screen.findByRole('alert')).toHaveTextContent(/reprint/i);
    expect(api.getSpool).not.toHaveBeenCalled();
    expect(onScan).not.toHaveBeenCalled();
  });
});
