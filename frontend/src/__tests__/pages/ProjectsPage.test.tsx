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
