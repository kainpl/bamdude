/**
 * Tests for LibraryFileNotesButton (gh#3).
 *
 * Covers: correct icon variant for count=0 vs count>0, count badge visibility
 * in overlay variant, and popover open/close on click.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { LibraryFileNotesButton } from '../../components/LibraryFileNotesButton';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

describe('LibraryFileNotesButton', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    server.use(
      http.get('/api/v1/library/files/:id/notes', () => HttpResponse.json([])),
    );
  });

  it('renders MessageSquarePlus (plus) icon when count is 0', () => {
    render(<LibraryFileNotesButton fileId={1} initialCount={0} />);
    const btn = screen.getByTestId('library-file-notes-button-1');
    expect(btn).toBeInTheDocument();
    // lucide icons render as svg with the icon name in the class name
    const svg = btn.querySelector('svg');
    expect(svg?.classList.toString()).toMatch(/message-square-plus/);
  });

  it('renders MessageSquare (plain) icon when count > 0', () => {
    render(<LibraryFileNotesButton fileId={1} initialCount={3} variant="overlay" />);
    const btn = screen.getByTestId('library-file-notes-button-1');
    const svg = btn.querySelector('svg');
    expect(svg?.classList.toString()).toMatch(/message-square/);
    expect(svg?.classList.toString()).not.toMatch(/message-square-plus/);
  });

  it('shows count badge on overlay variant when count > 0', () => {
    render(<LibraryFileNotesButton fileId={1} initialCount={5} variant="overlay" />);
    const btn = screen.getByTestId('library-file-notes-button-1');
    expect(btn).toHaveTextContent('5');
  });

  it('shows count badge on inline variant too', () => {
    // As of the list-mode parity update, inline variant mirrors overlay:
    // plus-icon when empty, plain icon + count when populated.
    render(<LibraryFileNotesButton fileId={1} initialCount={5} variant="inline" />);
    const btn = screen.getByTestId('library-file-notes-button-1');
    expect(btn).toHaveTextContent('5');
  });

  it('opens popover on click', async () => {
    const user = userEvent.setup();
    render(<LibraryFileNotesButton fileId={1} initialCount={0} />);

    // Popover not rendered yet
    expect(screen.queryByPlaceholderText('Type your note…')).not.toBeInTheDocument();

    await user.click(screen.getByTestId('library-file-notes-button-1'));
    // Fetch empty → create form appears
    await waitFor(() => {
      expect(screen.getByPlaceholderText('Type your note…')).toBeInTheDocument();
    });
  });
});
