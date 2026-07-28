import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { PlateMarkers } from '../../components/PlateObjectMarkers';
import { markerPosition, type PlateObject } from '../../components/plateDialogLayout';

const objects: PlateObject[] = [
  { id: 941, name: 'part', x: 0.25, y: 0.25, norm: true, skipped: false },
  { id: 942, name: 'part', x: 0.75, y: 0.75, norm: true, skipped: false },
];
const t = (key: string) => key;

describe('PlateMarkers', () => {
  it('renders one marker per object showing the raw identify_id', () => {
    render(<PlateMarkers objects={objects} t={t} />);
    expect(screen.getByText('941')).toBeInTheDocument();
    expect(screen.getByText('942')).toBeInTheDocument();
  });

  it('is inert with no onSkip — the read-only preview must never skip', () => {
    const onSkip = vi.fn();
    render(<PlateMarkers objects={objects} t={t} />);
    fireEvent.click(screen.getByText('941'));
    expect(onSkip).not.toHaveBeenCalled();
    expect(screen.getByText('941').closest('button')).toBeDisabled();
  });

  it('calls onSkip when interactive', () => {
    const onSkip = vi.fn();
    render(<PlateMarkers objects={objects} t={t} canSkip={() => true} onSkip={onSkip} />);
    fireEvent.click(screen.getByText('941'));
    expect(onSkip).toHaveBeenCalledWith({ id: 941, name: 'part' });
  });

  it('renders nothing for an empty plate', () => {
    const { container } = render(<PlateMarkers objects={[]} t={t} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe('markerPosition', () => {
  it('uses the normalised pick centroid when there is one', () => {
    expect(markerPosition(objects[0], 0, 2, null)).toEqual({ x: 25, y: 25 });
  });

  it('falls back to a grid when the object has no coordinates at all', () => {
    // Positions are meaningless here, but every object must still land inside
    // the plate box so it stays reachable — the list is the real UI.
    const orphan: PlateObject = { id: 7, name: 'x', x: null, y: null, skipped: false };
    const { x, y } = markerPosition(orphan, 3, 9, null);
    expect(x).toBeGreaterThan(0);
    expect(x).toBeLessThan(100);
    expect(y).toBeGreaterThan(0);
    expect(y).toBeLessThan(100);
  });
});
