/**
 * Tests for FolderReadmePanel (#1268).
 */

import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { FolderReadmePanel } from '../../components/FolderReadmePanel';
import { server } from '../mocks/server';

describe('FolderReadmePanel', () => {
  it('renders nothing when the folder has no markdown (404)', async () => {
    server.use(
      http.get('/api/v1/library/folders/:id/readme', () =>
        HttpResponse.json({ detail: 'No markdown' }, { status: 404 }),
      ),
    );
    render(<FolderReadmePanel folderId={1} />);
    // Wait briefly so the query has time to resolve, then confirm no panel
    // chrome leaked into the DOM (the test render util mounts toast/provider
    // wrappers, so we can't assert `container.firstChild === null`).
    await waitFor(() => {
      expect(screen.queryByText('Truncated')).not.toBeInTheDocument();
      expect(document.querySelector('button[type="button"] svg.lucide-file-text')).toBeNull();
    });
  });

  it('renders markdown content and the filename when present', async () => {
    server.use(
      http.get('/api/v1/library/folders/:id/readme', () =>
        HttpResponse.json({
          filename: 'README.md',
          content: '# Robot model\n\nA cute robot.',
          truncated: false,
        }),
      ),
    );
    render(<FolderReadmePanel folderId={42} />);
    expect(await screen.findByText('README.md')).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'Robot model' })).toBeInTheDocument();
    expect(screen.getByText('A cute robot.')).toBeInTheDocument();
  });

  it('shows a Truncated chip when the API flags the content as clipped', async () => {
    server.use(
      http.get('/api/v1/library/folders/:id/readme', () =>
        HttpResponse.json({
          filename: 'description.md',
          content: 'very long content',
          truncated: true,
        }),
      ),
    );
    render(<FolderReadmePanel folderId={7} />);
    expect(await screen.findByText('Truncated')).toBeInTheDocument();
  });


  describe('collapsible rail (#2520)', () => {
    const withReadme = () =>
      server.use(
        http.get('/api/v1/library/folders/:id/readme', () =>
          HttpResponse.json({ filename: 'README.md', content: '# Docs', truncated: false }),
        ),
      );

    it('collapses to a reopen control and remembers the choice', async () => {
      // As a full-width block the README pushed the model cards below the
      // fold, so hiding it has to stick — across folder switches and reloads,
      // not just for the current render.
      localStorage.removeItem('fileManager.readmeCollapsed');
      withReadme();
      const { unmount } = render(<FolderReadmePanel folderId={1} />);
      await screen.findByText('README.md');

      await userEvent.click(screen.getByLabelText('Hide the folder description'));
      expect(screen.queryByText('# Docs')).not.toBeInTheDocument();
      expect(localStorage.getItem('fileManager.readmeCollapsed')).toBe('1');
      // The reopen affordance is still there (mobile bar + desktop strip both
      // render; CSS picks one).
      expect(screen.getAllByLabelText('Show the folder description').length).toBeGreaterThan(0);

      unmount();

      // A fresh mount — a different folder, or a reload — stays collapsed.
      render(<FolderReadmePanel folderId={2} />);
      await waitFor(() => {
        expect(screen.getAllByLabelText('Show the folder description').length).toBeGreaterThan(0);
      });
    });

    it('starts expanded when nothing was remembered', async () => {
      localStorage.removeItem('fileManager.readmeCollapsed');
      withReadme();
      render(<FolderReadmePanel folderId={3} />);
      expect(await screen.findByLabelText('Hide the folder description')).toBeInTheDocument();
    });
  });
});
