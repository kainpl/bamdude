import { describe, expect, it } from 'vitest';
import { screen } from '@testing-library/react';
import { render } from '../utils';
import { AutoQueueReturnInfo } from '../../components/AutoQueueReturnInfo';

describe('automatic failed return facts', () => {
  it('keeps a returned print visibly separate from a new attempt', () => {
    render(<AutoQueueReturnInfo returned ordinal={2} />);
    const link = screen.getByRole('link', { name: /Returned to Auto Queue.*2\/2/i });
    expect(link).toHaveAttribute('href', '#auto-queue');
    expect(link).toHaveClass('min-h-11');
  });

  it('shows the durable count and the limit refusal without hover', () => {
    render(<AutoQueueReturnInfo count={2} reason="return_limit" />);
    expect(screen.getByText('Automatic returns: 2/2')).toBeVisible();
    expect(screen.getByText(/limit of two automatic returns was reached/)).toBeVisible();
  });

  it('shows a historical marker even when its receipt is missing', () => {
    render(<AutoQueueReturnInfo returned />);
    expect(screen.getByRole('link', { name: 'Returned to Auto Queue' })).toBeVisible();
  });
});
