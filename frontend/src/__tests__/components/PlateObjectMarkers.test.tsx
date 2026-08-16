import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { PlateMarkers } from '../../components/PlateObjectMarkers';
import { type PlateObject } from '../../components/plateDialogLayout';

const objects: PlateObject[] = [
  { id: 941, name: 'part', x: 0.25, y: 0.25, norm: true, skipped: false, marker: { x: 25, y: 25 } },
  { id: 942, name: 'part', x: 0.75, y: 0.75, norm: true, skipped: false, marker: { x: 75, y: 75 } },
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

  it('drops an object the server sent no marker for, keeping the rest', () => {
    // Rolling upgrade: a browser holding this bundle can be talking to a
    // backend that predates the server-side marker. Losing one pin is a
    // degradation; destructuring undefined would take the whole dialog down.
    const mixed = [{ ...objects[0], marker: undefined }, objects[1]] as unknown as PlateObject[];
    render(<PlateMarkers objects={mixed} t={t} />);
    expect(screen.queryByText('941')).not.toBeInTheDocument();
    expect(screen.getByText('942')).toBeInTheDocument();
  });
});
