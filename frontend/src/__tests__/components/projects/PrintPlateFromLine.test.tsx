import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Order, ProjectLine } from '../../../api/client';
import { PrintPlateFromLine } from '../../../components/projects/PrintPlateFromLine';

describe('PrintPlateFromLine', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("lists the product's plates, disables unsliced ones and highlights the line's material", async () => {
    vi.spyOn(api, 'getProductPlates').mockResolvedValue([
      {
        id: 1,
        library_file_id: 7,
        plate_index: 1,
        filename: 'flask.3mf',
        sliced: true,
        yield: [{ part_id: 1, name: 'flask', count: 4 }],
        unassigned: [],
        materials: ['PETG'],
        colors: [],
        print_time_seconds: 3600,
        filament_used_grams: 40,
      },
      {
        id: 2,
        library_file_id: 8,
        plate_index: 0,
        filename: 'flask.stl',
        sliced: false,
        yield: [],
        unassigned: [],
        materials: [],
        colors: [],
        print_time_seconds: null,
        filament_used_grams: null,
      },
    ] as never);
    render(
      <PrintPlateFromLine
        order={{ id: 1 } as Order}
        line={{ id: 10, product_id: 1, material: 'PETG' } as ProjectLine}
        onClose={() => {}}
      />,
    );
    expect(await screen.findByText('flask.3mf')).toBeInTheDocument();
    expect(screen.getByTestId('plate-2-print')).toBeDisabled();
    expect(screen.getByText('PETG')).toHaveClass('text-bambu-green');
  });
});
