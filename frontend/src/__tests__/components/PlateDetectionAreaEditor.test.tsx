import { describe, it, expect, vi } from 'vitest';
import { screen, fireEvent } from '@testing-library/react';
import { render } from '../utils';
import { PlateDetectionAreaEditor } from '../../components/PlateDetectionAreaEditor';

const roi = { x: 0, y: .2, w: .8, h: .6 };
const polygon = [{ x: .1, y: .1 }, { x: .8, y: .1 }, { x: .6, y: .8 }];
function setup(points?: typeof polygon, saving = false) {
  const onSave = vi.fn();
  render(<PlateDetectionAreaEditor imageUrl="data:image/jpeg;base64,test" roi={roi} polygon={points}
    saving={saving} onSave={onSave} onCancel={vi.fn()} />);
  return onSave;
}
describe('plate detection area editor', () => {
  it('keeps rectangle default, including coordinates on image edges', () => {
    const save = setup();
    expect(screen.getByRole('button', { name: 'Rectangle' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(save).toHaveBeenCalledWith(roi, null);
  });
  it('saves the polygon and permits keyboard adjustment', () => {
    const save = setup(polygon);
    fireEvent.keyDown(screen.getByRole('button', { name: 'Vertex 1' }), { key: 'ArrowRight' });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(save).toHaveBeenCalledWith(roi, [{ x: .10500000000000001, y: .1 }, ...polygon.slice(1)]);
  });
  it('switching to rectangle explicitly clears the saved polygon', () => {
    const save = setup(polygon);
    fireEvent.click(screen.getByRole('button', { name: 'Rectangle' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(save).toHaveBeenCalledWith(roi, null);
  });
  it('cannot save an unfinished new contour', () => {
    const save = setup(polygon);
    fireEvent.click(screen.getByRole('button', { name: 'Draw a new contour' }));
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(save).not.toHaveBeenCalled();
  });
  it('does not permit edits while saving', () => {
    const save = setup(polygon, true);
    expect(screen.getByRole('button', { name: 'Rectangle' })).toBeDisabled();
    fireEvent.keyDown(screen.getByRole('button', { name: 'Vertex 1' }), { key: 'Delete' });
    expect(screen.getAllByRole('button', { name: /Vertex/ })).toHaveLength(3);
    expect(save).not.toHaveBeenCalled();
  });
  it('draws a contour in normalized image coordinates and drags a vertex', () => {
    const save = setup(polygon);
    fireEvent.click(screen.getByRole('button', { name: 'Draw a new contour' }));
    const svg = screen.getByAltText('Original camera frame').parentElement!.querySelector('svg')!;
    vi.spyOn(svg, 'getBoundingClientRect').mockReturnValue({ left: 100, top: 50, width: 400, height: 200 } as DOMRect);
    svg.setPointerCapture = vi.fn();
    svg.hasPointerCapture = vi.fn().mockReturnValue(true);
    svg.releasePointerCapture = vi.fn();
    const pointer = (target: Element, type: string, x: number, y: number) => {
      fireEvent(target, new MouseEvent(type, { bubbles: true, clientX: x, clientY: y }));
    };
    pointer(svg, 'pointerdown', 140, 70);
    pointer(svg, 'pointerdown', 420, 70);
    pointer(svg, 'pointerdown', 340, 210);
    pointer(screen.getByRole('button', { name: 'Vertex 1' }), 'pointerdown', 140, 70);
    pointer(svg, 'pointermove', 180, 90);
    pointer(svg, 'pointerup', 180, 90);
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(save).toHaveBeenCalledWith(roi, [{ x: .2, y: .2 }, { x: .8, y: .1 }, { x: .6, y: .8 }]);
    expect(svg.releasePointerCapture).toHaveBeenCalled();
  });
  it('cancel leaves the saved contour untouched', () => {
    const save = vi.fn();
    const cancel = vi.fn();
    render(<PlateDetectionAreaEditor imageUrl="test.jpg" roi={roi} polygon={polygon}
      saving={false} onSave={save} onCancel={cancel} />);
    fireEvent.click(screen.getByRole('button', { name: 'Draw a new contour' }));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(cancel).toHaveBeenCalledOnce();
    expect(save).not.toHaveBeenCalled();
  });
});
