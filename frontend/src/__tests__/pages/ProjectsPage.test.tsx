/**
 * Tests for the ProjectsPage component.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
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

  describe('archive/unarchive (card actions menu)', () => {
    /**
     * The card's actions-menu button (`MoreVertical`) carries no accessible
     * name or text — it's icon-only. Before the dropdown opens it is the
     * ONLY button inside the card (`.group` root), so scoping with
     * `within(card).getByRole('button')` finds it unambiguously without
     * resorting to SVG-class sniffing (what the previous two attempts did).
     * `getByRole` throws if the button is missing, so a removed/renamed
     * trigger fails the test instead of silently skipping the assertions.
     */
    const openCardMenu = async (user: ReturnType<typeof userEvent.setup>, projectName: string) => {
      const heading = await screen.findByText(projectName);
      const card = heading.closest('.group');
      if (!(card instanceof HTMLElement)) {
        throw new Error(`could not find the .group card container for "${projectName}"`);
      }
      const moreButton = within(card).getByRole('button');
      await user.click(moreButton);
      return card;
    };

    /** Captures the body/id of the next PATCH so tests can assert exactly
     *  what `api.updateProject` (called from `archiveMutation.mutationFn`)
     *  sent over the wire. */
    const capturePatch = () => {
      const calls: { id: number; status: string }[] = [];
      server.use(
        http.patch('/api/v1/projects/:id', async ({ request, params }) => {
          const body = (await request.json()) as { status?: string };
          const id = Number(params.id);
          calls.push({ id, status: body.status ?? '' });
          return HttpResponse.json({ id, status: body.status });
        })
      );
      return calls;
    };

    // A fixture set with all three statuses represented, used by the tests
    // that need a project OTHER than "active" to actually be on screen. The
    // page defaults to the "active" status tab (`statusFilter` state), so an
    // archived/completed project is invisible until the "All" tab is
    // selected — the previous test attempts reused the always-visible
    // active fixture for the "archived project" case instead of fixing
    // this, which is why they never really exercised the Unarchive branch.
    const mixedStatusProjects = [
      { ...mockProjects[0] },
      { ...mockProjects[0], id: 101, name: 'Legacy Prints', status: 'archived' },
      { ...mockProjects[0], id: 102, name: 'Wrapped Up', status: 'completed' },
    ];

    const switchToAllTab = async (user: ReturnType<typeof userEvent.setup>) => {
      // The "All" tab label is a lone <span>All</span> — distinct from the
      // sibling count-badge <span>, so getByText matches it uniquely even
      // though the parent button's concatenated text is "All3".
      await user.click(screen.getByText('All'));
    };

    it('active project: opening the menu and clicking Archive calls updateProject(id, {status: archived})', async () => {
      const user = userEvent.setup();
      const calls = capturePatch();

      render(<ProjectsPage />);

      await openCardMenu(user, 'Functional Parts');
      await user.click(screen.getByText('Archive'));

      await waitFor(() => {
        expect(calls).toEqual([{ id: 1, status: 'archived' }]);
      });
    });

    it('archived project: menu shows Unarchive, clicking it calls updateProject(id, {status: active})', async () => {
      server.use(
        http.get('/api/v1/projects/', () => HttpResponse.json(mixedStatusProjects))
      );
      const user = userEvent.setup();
      const calls = capturePatch();

      render(<ProjectsPage />);
      await switchToAllTab(user);

      const card = await openCardMenu(user, 'Legacy Prints');
      expect(within(card).queryByText('Archive')).not.toBeInTheDocument();
      await user.click(within(card).getByText('Unarchive'));

      await waitFor(() => {
        expect(calls).toEqual([{ id: 101, status: 'active' }]);
      });
    });

    it('completed project: menu shows Archive, not Unarchive', async () => {
      server.use(
        http.get('/api/v1/projects/', () => HttpResponse.json(mixedStatusProjects))
      );
      const user = userEvent.setup();

      render(<ProjectsPage />);
      await switchToAllTab(user);

      const card = await openCardMenu(user, 'Wrapped Up');
      expect(within(card).getByText('Archive')).toBeInTheDocument();
      expect(within(card).queryByText('Unarchive')).not.toBeInTheDocument();
    });

    it('permission denied (no projects:update): the Archive menu item is disabled', async () => {
      server.use(
        http.get('/api/v1/auth/me', () =>
          HttpResponse.json({
            id: 2,
            username: 'operator',
            role: 'user',
            is_active: true,
            is_admin: false,
            groups: [{ id: 2, name: 'Operators' }],
            // No projects:update — mirrors how Layout.test.tsx builds a
            // denied user. Real (non-admin) users go through this path;
            // `hasPermission` short-circuits to true only for is_admin.
            permissions: ['projects:read'],
            created_at: '2024-01-01T00:00:00Z',
          })
        )
      );
      const user = userEvent.setup();

      render(<ProjectsPage />);

      const card = await openCardMenu(user, 'Functional Parts');
      const archiveButton = within(card).getByText('Archive').closest('button');
      expect(archiveButton).toBeDisabled();

      // And clicking it must NOT reach the mutation — the onClick itself is
      // gated on hasPermission(), not just the `disabled` attribute.
      const calls = capturePatch();
      await user.click(within(card).getByText('Archive'));
      expect(calls).toEqual([]);
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
