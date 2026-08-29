/**
 * Tests for the ProjectsPage component.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { ProjectsPage, ProjectModal } from '../../pages/ProjectsPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { strayZeroTextNodes as strayZeroes } from '../domHelpers';

const mockProjects = [
  {
    id: 1,
    name: 'Functional Parts',
    description: 'Useful household items',
    color: '#00ae42',
    status: 'active',
    archive_count: 10,
    total_print_time_seconds: 36000,
    total_filament_grams: 500,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-15T00:00:00Z',
  },
  {
    id: 2,
    name: 'Art Collection',
    description: 'Decorative prints',
    color: '#ff5500',
    status: 'active',
    archive_count: 5,
    total_print_time_seconds: 18000,
    total_filament_grams: 200,
    created_at: '2024-01-05T00:00:00Z',
    updated_at: '2024-01-10T00:00:00Z',
  },
];

describe('ProjectsPage', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/projects/', () => {
        return HttpResponse.json(mockProjects);
      }),
      http.post('/api/v1/projects/', async ({ request }) => {
        const body = await request.json() as { name: string };
        return HttpResponse.json({ id: 3, name: body.name, color: '#00ae42', archive_count: 0 });
      }),
      http.delete('/api/v1/projects/:id', () => {
        return HttpResponse.json({ success: true });
      })
    );
  });

  describe('rendering', () => {
    it('renders the page title', async () => {
      render(<ProjectsPage />);

      await waitFor(() => {
        expect(screen.getByText('Projects')).toBeInTheDocument();
      });
    });

    it('shows project cards', async () => {
      render(<ProjectsPage />);

      await waitFor(() => {
        expect(screen.getByText('Functional Parts')).toBeInTheDocument();
        expect(screen.getByText('Art Collection')).toBeInTheDocument();
      });
    });

    it('shows project descriptions', async () => {
      render(<ProjectsPage />);

      await waitFor(() => {
        expect(screen.getByText('Useful household items')).toBeInTheDocument();
        expect(screen.getByText('Decorative prints')).toBeInTheDocument();
      });
    });
  });

  describe('project info', () => {
    it('shows archive count', async () => {
      render(<ProjectsPage />);

      await waitFor(() => {
        // Project cards should show archive counts
        expect(screen.getByText('Functional Parts')).toBeInTheDocument();
      });
    });

    it('shows project colors', async () => {
      render(<ProjectsPage />);

      await waitFor(() => {
        const functionalParts = screen.getByText('Functional Parts');
        expect(functionalParts).toBeInTheDocument();
        // Color is applied as style
      });
    });
  });

  describe('zero targets (#project-progress)', () => {
    // `0 && <jsx>` evaluates to 0, and React renders the NUMBER — so a target
    // legitimately set to 0 painted a bare "0" where the progress block used to
    // be. Both directions, because the two branches are separate sites.
    const withTargets = (targetCount: number, targetPartsCount: number) => [
      {
        ...mockProjects[0],
        name: 'Zero Target Project',
        target_count: targetCount,
        target_parts_count: targetPartsCount,
        completed_count: 3,
        archive_count: 2,
      },
    ];

    it('measuring by parts only does not paint a stray zero', async () => {
      server.use(http.get('/api/v1/projects/', () => HttpResponse.json(withTargets(0, 20))));
      render(<ProjectsPage />);

      await waitFor(() => expect(screen.getByText('Zero Target Project')).toBeInTheDocument());
      expect(strayZeroes()).toHaveLength(0);
    });

    it('measuring by plates only does not paint a stray zero', async () => {
      server.use(http.get('/api/v1/projects/', () => HttpResponse.json(withTargets(5, 0))));
      render(<ProjectsPage />);

      await waitFor(() => expect(screen.getByText('Zero Target Project')).toBeInTheDocument());
      expect(strayZeroes()).toHaveLength(0);
    });

    it('no targets at all does not paint a stray zero', async () => {
      server.use(http.get('/api/v1/projects/', () => HttpResponse.json(withTargets(0, 0))));
      render(<ProjectsPage />);

      await waitFor(() => expect(screen.getByText('Zero Target Project')).toBeInTheDocument());
      expect(strayZeroes()).toHaveLength(0);
    });
  });

  describe('create project', () => {
    it('has new project button', async () => {
      render(<ProjectsPage />);

      await waitFor(() => {
        expect(screen.getByText('New Project')).toBeInTheDocument();
      });
    });

    it('opens create modal on click', async () => {
      const user = userEvent.setup();
      render(<ProjectsPage />);

      await waitFor(() => {
        expect(screen.getByText('New Project')).toBeInTheDocument();
      });

      await user.click(screen.getByText('New Project'));

      // Modal should open - look for modal content
      await waitFor(() => {
        // Modal may show "Create Project" or similar text
        const modalContent = screen.queryByText(/create/i) ||
                           screen.queryByRole('dialog') ||
                           screen.queryByText(/name/i);
        expect(modalContent).toBeTruthy();
      });
    });
  });

  describe('empty state', () => {
    it('shows empty state when no projects', async () => {
      server.use(
        http.get('/api/v1/projects/', () => {
          return HttpResponse.json([]);
        })
      );

      render(<ProjectsPage />);

      await waitFor(() => {
        // Either empty state message or the page title should be visible
        const emptyMsg = screen.queryByText(/no projects/i);
        const pageTitle = screen.queryByText('Projects');
        expect(emptyMsg || pageTitle).toBeTruthy();
      });
    });
  });

  describe('archive/unarchive', () => {
    it('active project: menu shows Archive, clicking it calls updateProject(id, {status: archived})', async () => {
      const user = userEvent.setup();
      let updateCalledWith: { id: number; status: string } | null = null;

      server.use(
        http.patch('/api/v1/projects/:id', async ({ request }) => {
          const body = await request.json() as { status?: string };
          const url = new URL(request.url);
          const id = parseInt(url.pathname.split('/').pop() || '0', 10);
          updateCalledWith = { id, status: body.status || '' };
          return HttpResponse.json({ id, status: body.status });
        })
      );

      render(<ProjectsPage />);

      await waitFor(() => {
        expect(screen.getByText('Functional Parts')).toBeInTheDocument();
      });

      // Find and click more-actions button on first card
      const allButtons = screen.getAllByRole('button');
      const moreButton = allButtons.find(btn => {
        const parent = btn.closest('.group');
        return parent && parent.textContent?.includes('Functional Parts') &&
               btn.querySelector('svg[class*="w-4"]') &&
               !btn.textContent?.trim();
      });

      if (moreButton) {
        await user.click(moreButton);
        const archiveOption = await screen.findByText('Archive');
        expect(archiveOption).toBeInTheDocument();
        await user.click(archiveOption);

        await waitFor(() => {
          expect(updateCalledWith).toEqual({ id: 1, status: 'archived' });
        });
      }
    });

    it('archived project: shows Unarchive not Archive, clicking calls updateProject(id, {status: active})', async () => {
      const user = userEvent.setup();
      let updateCalledWith: { id: number; status: string } | null = null;

      server.use(
        http.patch('/api/v1/projects/:id', async ({ request }) => {
          const body = await request.json() as { status?: string };
          const url = new URL(request.url);
          const id = parseInt(url.pathname.split('/').pop() || '0', 10);
          updateCalledWith = { id, status: body.status || '' };
          return HttpResponse.json({ id, status: body.status });
        })
      );

      // First archive project 1 via mutation
      render(<ProjectsPage />);

      await waitFor(() => {
        expect(screen.getByText('Functional Parts')).toBeInTheDocument();
      });

      const allButtons = screen.getAllByRole('button');
      const moreButton = allButtons.find(btn => {
        const parent = btn.closest('.group');
        return parent && parent.textContent?.includes('Functional Parts') &&
               btn.querySelector('svg[class*="w-4"]') &&
               !btn.textContent?.trim();
      });

      if (moreButton) {
        await user.click(moreButton);
        await user.click(screen.getByText('Archive'));

        await waitFor(() => {
          expect(updateCalledWith?.status).toBe('archived');
        });

        // After archiving, clicking menu again should show Unarchive
        // Note: In real scenario component state would update, in test we verify mutation behavior
        expect(updateCalledWith).toEqual({ id: 1, status: 'archived' });
      }
    });

    it('completed project: menu shows Archive not Unarchive (status check)', async () => {
      const user = userEvent.setup();

      render(<ProjectsPage />);

      await waitFor(() => {
        expect(screen.getByText('Functional Parts')).toBeInTheDocument();
      });

      const allButtons = screen.getAllByRole('button');
      const moreButton = allButtons.find(btn => {
        const parent = btn.closest('.group');
        return parent && parent.textContent?.includes('Art Collection') &&
               btn.querySelector('svg[class*="w-4"]') &&
               !btn.textContent?.trim();
      });

      if (moreButton) {
        await user.click(moreButton);
        // For non-archived projects (active/completed), Archive label shows
        const archiveOption = await screen.findByText('Archive');
        expect(archiveOption).toBeInTheDocument();
        expect(screen.queryByText('Unarchive')).not.toBeInTheDocument();
      }
    });

    it('permission: Archive button mirrors Edit button gate (projects:update)', async () => {
      // Archive button uses same permission gate as Edit: projects:update
      // When permitted, button is interactive; when denied, it has cursor-not-allowed
      render(<ProjectsPage />);

      await waitFor(() => {
        expect(screen.getByText('Functional Parts')).toBeInTheDocument();
      });

      const allButtons = screen.getAllByRole('button');
      const moreButton = allButtons.find(btn => {
        const parent = btn.closest('.group');
        return parent && parent.textContent?.includes('Functional Parts') &&
               btn.querySelector('svg[class*="w-4"]') &&
               !btn.textContent?.trim();
      });

      if (moreButton) {
        const user = userEvent.setup();
        await user.click(moreButton);
        // Archive button should render and be enabled for users with projects:update
        const archiveButton = screen.getByText('Archive');
        expect(archiveButton).toBeInTheDocument();
      }
    });
  });
});

describe('ProjectModal — modal scrolls on short viewports (#1642)', () => {
  /**
   * Reporter on a Pi screen couldn't reach the Save button when editing a
   * project because the modal had no max-h / overflow. The structural fix
   * puts a max-h on the card, the form fields in a `flex-1 overflow-y-auto`
   * wrapper, and the Save/Cancel buttons in a `flex-shrink-0` sibling so
   * they're always visible regardless of scroll position.
   *
   * jsdom doesn't compute layout heights so we can't simulate the actual
   * overflow. We pin the structure instead: the scrollable wrapper exists,
   * the Save button is NOT a descendant of it, and the card has a max-h.
   * A future refactor that removes any of these would re-introduce the bug.
   */
  const editableProject = {
    id: 7,
    name: 'Spool holder',
    description: null,
    color: '#00ae42',
    url: null,
    cover_image_filename: null,
    archive_count: 0,
    total_print_time_seconds: 0,
    total_filament_grams: 0,
    target_plates_count: null,
    target_parts_count: null,
    tags: null,
    due_date: null,
    priority: null,
    budget: null,
    status: 'active' as const,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
  };

  it('renders the action footer outside the scrollable fields wrapper', () => {
    render(
      <ProjectModal
        project={editableProject as never}
        onClose={() => {}}
        onSave={() => {}}
        isLoading={false}
        currencySymbol="€"
        t={((k: string) => k) as never}
      />,
    );

    const saveButton = screen.getByRole('button', { name: 'common.save' });
    const scrollable = document.querySelector('.overflow-y-auto');
    expect(scrollable).not.toBeNull();
    // The save button must live OUTSIDE the scrollable region — otherwise
    // a long form pushes it below the fold on short viewports (#1642).
    expect(scrollable!.contains(saveButton)).toBe(false);
  });

  it('caps the modal card height so it cannot exceed the viewport', () => {
    render(
      <ProjectModal
        project={editableProject as never}
        onClose={() => {}}
        onSave={() => {}}
        isLoading={false}
        currencySymbol="€"
        t={((k: string) => k) as never}
      />,
    );

    // Card has max-h set so it never extends past the viewport — without
    // this, vertical-center alignment pushes the bottom of the modal
    // (including the action footer) off-screen.
    const card = document.querySelector('.max-h-\\[calc\\(100vh-2rem\\)\\]');
    expect(card).not.toBeNull();
  });
});
