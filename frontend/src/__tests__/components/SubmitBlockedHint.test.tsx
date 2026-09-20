/**
 * Tests for SubmitBlockedHint — the line that says why a submit button is grey.
 */

import { describe, it, expect, afterEach } from 'vitest';
import { screen, cleanup } from '@testing-library/react';
import { render } from '../utils';
import { SubmitBlockedHint } from '../../components/SubmitBlockedHint';

describe('SubmitBlockedHint', () => {
  afterEach(cleanup);

  it('renders nothing when the form is ready to submit', () => {
    // Check for the hint itself rather than an empty container: the shared
    // render wrapper mounts providers around it.
    const { container } = render(<SubmitBlockedHint missing={[]} />);
    expect(container.querySelector('p')).toBeNull();
  });

  it('names the fields still standing in the way', () => {
    render(<SubmitBlockedHint missing={['Username', 'Email']} />);
    expect(screen.getByText('Still needed: Username, Email')).toBeInTheDocument();
  });
});
