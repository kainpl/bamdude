/**
 * The bar must vanish for a denominator of 0 — not leave a bare "0" behind.
 *
 * `0 && <jsx>` evaluates to 0 and React renders the NUMBER, so every guard on a
 * count that can legitimately be zero paints a stray zero where the block used
 * to be (#ProjectDetailPageProgress). The gate here is `max <= 0`, and the
 * detector below walks TEXT nodes because *ByText queries walk elements and
 * cannot see a bare "0" sitting among an element's other children.
 */

import { describe, it, expect } from 'vitest';
import { screen } from '@testing-library/react';

import { render } from '../../utils';
import { strayZeroTextNodes } from '../../domHelpers';
import { ProgressBar } from '../../../components/projects/ProgressBar';

describe('ProgressBar', () => {
  it('renders nothing at all when the denominator is zero', () => {
    render(
      <div data-testid="host">
        <ProgressBar value={0} max={0} />
      </div>
    );
    expect(screen.getByTestId('host').children).toHaveLength(0);
    expect(strayZeroTextNodes()).toHaveLength(0);
  });

  it('caps the fill at 100% and prints value / max', () => {
    render(<ProgressBar value={7} max={5} testId="bar" />);
    expect(screen.getByTestId('bar-fill').style.width).toBe('100%');
    expect(screen.getByText('7 / 5')).toBeInTheDocument();
  });
});
