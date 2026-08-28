/**
 * The wait indicator every page shares.
 *
 * ⚠️ The thing being replaced was a line of grey text reading "Loading" with
 * nothing moving beside it — indistinguishable from a page that has given up,
 * because the only way to tell them apart was to wait and see whether it
 * changed. On a farm with a long archive that wait is seconds long.
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import { LoadingBlock } from '../../components/LoadingBlock';

describe('LoadingBlock', () => {
  it('says what is being waited for', () => {
    render(<LoadingBlock label="Loading statistics..." />);

    expect(screen.getByText('Loading statistics...')).toBeInTheDocument();
  });

  it('shows something that moves', () => {
    // ⚠️ The whole point. A label alone is what this replaces, so a version
    // that renders only the label would pass every other test here.
    const { container } = render(<LoadingBlock label="Loading" />);

    expect(container.querySelector('.animate-spin')).not.toBeNull();
  });

  it('announces itself to a screen reader without reading out the spinner', () => {
    const { container } = render(<LoadingBlock label="Loading" />);

    expect(screen.getByRole('status')).toHaveTextContent('Loading');
    expect(container.querySelector('[aria-hidden="true"]')).not.toBeNull();
  });

  it('takes the surrounding context when the page default does not fit', () => {
    // The stream overlay sits on black and a panel inside a card wants less
    // vertical room than a whole page does.
    const { container } = render(<LoadingBlock label="Loading" className="text-gray-400" />);

    const block = container.firstElementChild as HTMLElement;
    expect(block.className).toContain('text-gray-400');
    expect(block.className).not.toContain('py-16');
  });
});
