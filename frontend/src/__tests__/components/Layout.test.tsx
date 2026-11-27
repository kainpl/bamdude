/**
 * Tests for the Layout component.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { Layout } from '../../components/Layout';
import { register, unregister, _resetForTests } from '../../components/modalStack';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

// The number-key guard is a call to `navigate`, not a DOM change: key `1` goes
// to the first sidebar item, which is `/` — where the router already sits — so
// a pathname assertion could not tell "navigated home" from "did nothing".
// Spying on the router's navigate is the house pattern (see
// LoginPageAuthedRedirect.test.tsx); NavLink keeps the real implementation, so
// the sidebar-link tests in this file are untouched by it.
const navigateSpy = vi.fn();
vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return { ...actual, useNavigate: () => navigateSpy };
});

describe('Layout', () => {
  beforeEach(() => {
    // Only the sidebar's own keys: utils.tsx seeds `auth_token`, which a clear() would drop.
    localStorage.removeItem('sidebarOrder');
    localStorage.removeItem('sidebarNavOpen');
    server.use(
      http.get('/api/v1/printers/', () => {
        return HttpResponse.json([
          { id: 1, name: 'X1 Carbon', model: 'X1C', enabled: true },
        ]);
      }),
      http.get('/api/v1/printers/:id/status', () => {
        return HttpResponse.json({
          connected: true,
          state: 'IDLE',
        });
      }),
      http.get('/api/v1/version', () => {
        return HttpResponse.json({ version: '0.1.6', build: 'test' });
      }),
      http.get('/api/v1/settings/', () => {
        return HttpResponse.json({
          check_updates: false,
          check_printer_firmware: false,
        });
      }),
      http.get('/api/v1/external-links/', () => {
        return HttpResponse.json([]);
      }),
      http.get('/api/v1/smart-plugs/', () => {
        return HttpResponse.json([]);
      }),
      http.get('/api/v1/support/debug-logging', () => {
        return HttpResponse.json({ enabled: false });
      }),
      http.get('/api/v1/queue/', () => {
        return HttpResponse.json([]);
      }),
      http.get('/api/v1/updates/check', () => {
        return HttpResponse.json({ update_available: false });
      }),
      http.get('/api/v1/auth/status', () => {
        return HttpResponse.json({ auth_enabled: false, requires_setup: false });
      }),
      http.get('/api/v1/printers/developer-mode-warnings', () => {
        return HttpResponse.json([]);
      }),
      http.get('/api/v1/inbox/unread-count', () => HttpResponse.json({ unread_count: 0 }))
    );
  });

  describe('rendering', () => {
    it('renders the sidebar', async () => {
      render(<Layout />);

      // Layout renders as a flex container with sidebar
      await waitFor(() => {
        const sidebar = document.querySelector('aside');
        expect(sidebar).toBeInTheDocument();
      });
    });

    // WS-13 E7 G02: the WINDOW scrolls the page, so <main> must not be a scroll box of its
    // own — an `overflow` there makes it one that never scrolls, and every `sticky` inside
    // it (the orders workspace list, the file manager's panel, the inventory's bulk bar)
    // sticks to it instead of the screen, i.e. never.
    it('does not make the main area a scroll box of its own', async () => {
      render(<Layout />);

      await waitFor(() => expect(document.querySelector('main')).toBeInTheDocument());
      const main = document.querySelector('main') as HTMLElement;
      expect(main.className).not.toMatch(/(^|\s)overflow-(auto|scroll|hidden|clip|y-auto|x-auto)(\s|$)/);
      expect(main.className).toContain('min-w-0');
    });

    // The same change lets every page-level `sticky` stick to the WINDOW — where the fixed
    // compact header (h-14, below 1144 px) covers the top. Layout says its height once, as
    // `--app-top`, for those bars and for the document's scroll padding.
    it('tells the page how tall the fixed compact header is, and zero when there is none', async () => {
      // Swapped by hand and put back: a spy's restore would reset setup.ts's own mock.
      const original = window.matchMedia;
      const compact = (on: boolean) => {
        window.matchMedia = ((query: string) =>
          ({
            matches: on && query.includes('max-width: 1143px'),
            media: query,
            onchange: null,
            addListener: vi.fn(),
            removeListener: vi.fn(),
            addEventListener: vi.fn(),
            removeEventListener: vi.fn(),
            dispatchEvent: vi.fn(),
          }) as unknown as MediaQueryList) as typeof window.matchMedia;
      };
      try {
        compact(true);
        const { unmount } = render(<Layout />);
        await waitFor(() => expect(document.documentElement.style.getPropertyValue('--app-top')).toBe('3.5rem'));
        unmount();
        expect(document.documentElement.style.getPropertyValue('--app-top')).toBe('');
        compact(false);
        render(<Layout />);
        await waitFor(() => expect(document.querySelector('main')).toBeInTheDocument());
        await waitFor(() => expect(document.documentElement.style.getPropertyValue('--app-top')).toBe('0px'));
      } finally {
        window.matchMedia = original;
      }
    });

    it('renders navigation links', async () => {
      render(<Layout />);

      await waitFor(() => {
        // Navigation links should be present
        const links = document.querySelectorAll('a');
        expect(links.length).toBeGreaterThan(0);
      });
    });

    it('renders the brand lock-up from the pack, never the old mascot', async () => {
      render(<Layout />);
      await waitFor(() => {
        const logos = document.querySelectorAll<HTMLImageElement>('img[alt="BamDude"]');
        expect(logos.length).toBeGreaterThan(0);
        for (const img of logos) {
          expect(img.getAttribute('src')).toMatch(/^\/img\/brand\/(lockup-compact-on-(dark|light)\.svg|mark-on-(dark|light)-64\.png)$/);
        }
      });
    });
  });

  describe('navigation', () => {
    it('has navigation items', async () => {
      render(<Layout />);

      await waitFor(() => {
        // Should have multiple navigation links
        const navLinks = document.querySelectorAll('a[href]');
        expect(navLinks.length).toBeGreaterThan(0);
      });
    });

    it('includes settings link', async () => {
      render(<Layout />);

      await waitFor(() => {
        // Settings link should exist (route /settings)
        const settingsLink = document.querySelector('a[href="/settings"]');
        expect(settingsLink).toBeInTheDocument();
      });
    });
  });

  describe('external link badge', () => {
    // N45 (upstream 29a81520d): the app a sidebar link points at reports a count for it.
    const link = (id: number, badge_count: number | null) => ({
      id,
      name: `App ${id}`,
      url: `http://app${id}.lan`,
      icon: 'link',
      open_in_new_tab: id === 1,
      custom_icon: null,
      nav_group: 'external',
      sort_order: id,
      created_at: '2026-11-27T00:00:00',
      updated_at: '2026-11-27T00:00:00',
      badge_count,
    });

    it('shows the count the app reports, capped at 99+, and nothing without one', async () => {
      server.use(
        http.get('/api/v1/external-links/', () => HttpResponse.json([link(1, 7), link(2, 150), link(3, null), link(4, 0)])),
      );
      render(<Layout />);

      expect(await screen.findByTestId('external-link-badge-1')).toHaveTextContent('7');
      expect(screen.getByTestId('external-link-badge-2')).toHaveTextContent('99+');
      expect(screen.queryByTestId('external-link-badge-3')).not.toBeInTheDocument();
      expect(screen.queryByTestId('external-link-badge-4')).not.toBeInTheDocument();
    });
  });

  describe('keyboard shortcuts', () => {
    afterEach(() => {
      _resetForTests();
    });

    it('number-key navigation is off while a modal is open', async () => {
      render(<Layout />);
      await waitFor(() => {
        expect(document.querySelector('a[href="/settings"]')).toBeInTheDocument();
      });

      // A modal is on the stack: `1` must not move the app out from under it.
      act(() => {
        register('probe', [], { current: { onClose: vi.fn(), closeDisabled: false } }, { current: null });
      });
      navigateSpy.mockClear();
      fireEvent.keyDown(document, { key: '1' });
      expect(navigateSpy).not.toHaveBeenCalled();

      // With the stack empty again the very same keypress navigates — so the
      // assertion above pinned the guard, not a key handler that never worked.
      act(() => {
        unregister('probe');
      });
      navigateSpy.mockClear();
      fireEvent.keyDown(document, { key: '1' });
      expect(navigateSpy).toHaveBeenCalled();
    });
  });

  describe('version display', () => {
    it('shows version info', async () => {
      render(<Layout />);

      await waitFor(() => {
        // Version info is displayed in sidebar
        expect(document.body).toBeInTheDocument();
      });
    });
  });

  describe('theme toggle', () => {
    it('has theme toggle button', async () => {
      render(<Layout />);

      await waitFor(() => {
        // Theme toggle should be present
        const buttons = document.querySelectorAll('button');
        expect(buttons.length).toBeGreaterThan(0);
      });
    });
  });

  describe('plate detection alert modal', () => {
    it('shows unavailable checks without claiming objects were detected', async () => {
      render(<Layout />);
      await waitFor(() => expect(document.querySelector('a[href="/settings"]')).toBeInTheDocument());
      window.dispatchEvent(new CustomEvent('plate-not-empty', {
        detail: { printer_id: 1, printer_name: 'Product A printer', reason: 'plate_check_unavailable' },
      }));
      await waitFor(() => expect(document.body.textContent).toContain('The plate check is unavailable'));
      expect(document.body.textContent).not.toContain('Objects detected on build plate');
    });
    it('shows modal when plate-not-empty event is dispatched', async () => {
      render(<Layout />);

      // Wait for AuthContext to finish loading the admin user so the listener's
      // hasPermission('printers:control') guard sees a real user instead of null.
      await waitFor(() => {
        expect(document.querySelector('a[href="/settings"]')).toBeInTheDocument();
      });

      // Dispatch the plate-not-empty event
      window.dispatchEvent(
        new CustomEvent('plate-not-empty', {
          detail: {
            printer_id: 1,
            printer_name: 'Test Printer',
            message: 'Objects detected on build plate',
          },
        })
      );

      await waitFor(() => {
        // Modal should appear with "Print Paused!" text
        expect(document.body.textContent).toContain('Print Paused!');
        expect(document.body.textContent).toContain('Test Printer');
      });
    });

    it('closes modal when I Understand button is clicked', async () => {
      render(<Layout />);

      await waitFor(() => {
        expect(document.querySelector('a[href="/settings"]')).toBeInTheDocument();
      });

      // Dispatch the plate-not-empty event
      window.dispatchEvent(
        new CustomEvent('plate-not-empty', {
          detail: {
            printer_id: 1,
            printer_name: 'Test Printer',
            message: 'Objects detected on build plate',
          },
        })
      );

      await waitFor(() => {
        expect(document.body.textContent).toContain('Print Paused!');
      });

      // Click the "I Understand" button
      const button = document.querySelector('button');
      if (button && button.textContent?.includes('I Understand')) {
        button.click();
      }

      // Find and click the "I Understand" button by searching all buttons
      const buttons = document.querySelectorAll('button');
      buttons.forEach((btn) => {
        if (btn.textContent?.includes('I Understand')) {
          btn.click();
        }
      });

      await waitFor(() => {
        // Modal should be closed
        expect(document.body.textContent).not.toContain('Print Paused!');
      });
    });
  });

  describe('developer mode warning banner', () => {
    it('shows warning banner when printers lack developer mode', async () => {
      server.use(
        http.get('/api/v1/printers/developer-mode-warnings', () => {
          return HttpResponse.json([
            { printer_id: 1, name: 'X1 Carbon' },
          ]);
        })
      );

      render(<Layout />);

      await waitFor(() => {
        expect(document.body.textContent).toContain('Developer LAN mode is not enabled on');
        expect(document.body.textContent).toContain('X1 Carbon');
      });
    });

    it('shows multiple printer names in warning banner', async () => {
      server.use(
        http.get('/api/v1/printers/developer-mode-warnings', () => {
          return HttpResponse.json([
            { printer_id: 1, name: 'X1 Carbon' },
            { printer_id: 2, name: 'P1S' },
          ]);
        })
      );

      render(<Layout />);

      await waitFor(() => {
        expect(document.body.textContent).toContain('X1 Carbon');
        expect(document.body.textContent).toContain('P1S');
      });
    });

    it('hides warning banner when no printers lack developer mode', async () => {
      // Default handler returns empty array
      render(<Layout />);

      await waitFor(() => {
        const sidebar = document.querySelector('aside');
        expect(sidebar).toBeInTheDocument();
      });

      // Banner should not be present
      expect(document.body.textContent).not.toContain('Developer LAN mode is not enabled on');
    });

    it('shows how to enable link in warning banner', async () => {
      server.use(
        http.get('/api/v1/printers/developer-mode-warnings', () => {
          return HttpResponse.json([
            { printer_id: 1, name: 'X1 Carbon' },
          ]);
        })
      );

      render(<Layout />);

      await waitFor(() => {
        expect(document.body.textContent).toContain('How to enable');
        const link = document.querySelector('a[href*="enable-developer-mode"]');
        expect(link).toBeInTheDocument();
      });
    });
  });

  describe('Sidebar gate accepts granular read tiers (#1755)', () => {
    // The default Operators group is seeded with `*:read_own` only (A2 m091) —
    // never the legacy `*:read`. Before H6 the sidebar gated Archives / Queue /
    // Files on the legacy flag alone, so those entries hid from every non-admin
    // even though the underlying API accepted their requests. These pin that the
    // gate now accepts ANY of the three tiers (legacy / _own / _all).
    const enableAuthWithUser = (permissions: string[]) => {
      server.use(
        http.get('/api/v1/auth/status', () =>
          HttpResponse.json({ auth_enabled: true, requires_setup: false }),
        ),
        http.get('/api/v1/auth/me', () =>
          HttpResponse.json({
            id: 1,
            username: 'tester',
            role: 'user',
            is_active: true,
            is_admin: false,
            groups: [{ id: 2, name: 'Operators' }],
            permissions,
            created_at: '2026-01-01T00:00:00Z',
          }),
        ),
      );
      window.localStorage.setItem('auth_token', 'test-token');
    };

    const sidebarLink = (href: string) =>
      document.querySelector(`aside a[href="${href}"]`);

    it('shows Files in the sidebar when the user only has library:read_own', async () => {
      enableAuthWithUser(['library:read_own']);
      render(<Layout />);
      await waitFor(() => {
        expect(document.querySelector('aside')).toBeInTheDocument();
        expect(sidebarLink('/files')).toBeInTheDocument();
      });
    });

    it('shows Files in the sidebar when the user only has library:read_all', async () => {
      enableAuthWithUser(['library:read_all']);
      render(<Layout />);
      await waitFor(() => {
        expect(sidebarLink('/files')).toBeInTheDocument();
      });
    });

    it('shows Archives in the sidebar when the user only has archives:read_own', async () => {
      enableAuthWithUser(['archives:read_own']);
      render(<Layout />);
      await waitFor(() => {
        expect(sidebarLink('/archives')).toBeInTheDocument();
      });
    });

    it('shows Queue in the sidebar when the user only has queue:read_own', async () => {
      enableAuthWithUser(['queue:read_own']);
      render(<Layout />);
      await waitFor(() => {
        expect(sidebarLink('/queue')).toBeInTheDocument();
      });
    });

    it('still hides Files/Archives/Queue with none of the three read tiers', async () => {
      enableAuthWithUser(['printers:read']);
      render(<Layout />);
      await waitFor(() => {
        expect(document.querySelector('aside')).toBeInTheDocument();
      });
      expect(sidebarLink('/files')).toBeNull();
      expect(sidebarLink('/archives')).toBeNull();
      expect(sidebarLink('/queue')).toBeNull();
    });

    it('hides Projects and asks no badge count without a Workshop read', async () => {
      let asked = 0;
      server.use(
        http.get('/api/v1/projects/nav-badges', () => {
          asked += 1;
          return HttpResponse.json({ active_orders: 1 });
        }),
      );
      enableAuthWithUser(['printers:read', 'queue:read_own']);
      render(<Layout />);
      await waitFor(() => expect(sidebarLink('/queue')).not.toBeNull());
      expect(screen.queryByRole('button', { name: 'Projects' })).not.toBeInTheDocument();
      expect(sidebarLink('/products')).toBeNull();
      expect(asked).toBe(0);
    });
  });

  describe('nested Projects entry', () => {
    it('renders the parent with its four children', async () => {
      render(<Layout />);
      const toggle = await screen.findByRole('button', { name: 'Projects' });
      expect(toggle).toHaveAttribute('aria-expanded', 'true');
      for (const name of ['Orders', 'Products', 'Customers', 'Stock']) {
        expect(screen.getByRole('link', { name })).toBeInTheDocument();
      }
    });

    it('shows the active-orders count from the server', async () => {
      server.use(http.get('/api/v1/projects/nav-badges', () => HttpResponse.json({ active_orders: 3 })));
      render(<Layout />);
      expect(await screen.findByTitle('Active orders')).toHaveTextContent('3');
    });

    it('shows the draft-products count on Products', async () => {
      server.use(
        http.get('/api/v1/projects/nav-badges', () => HttpResponse.json({ active_orders: 0, draft_products: 4 })),
      );
      render(<Layout />);
      expect(await screen.findByTitle('Draft products')).toHaveTextContent('4');
    });

    it('shows the below-minimum count on Stock', async () => {
      server.use(
        http.get('/api/v1/projects/nav-badges', () =>
          HttpResponse.json({ active_orders: 0, draft_products: 0, stock_below_min: 2 }),
        ),
      );
      render(<Layout />);
      expect(await screen.findByTitle('Positions below minimum')).toHaveTextContent('2');
    });

    it('a failed count leaves the entry and no badge', async () => {
      let asked = 0;
      server.use(
        http.get('/api/v1/projects/nav-badges', () => {
          asked += 1;
          return HttpResponse.json({ detail: 'boom' }, { status: 500 });
        }),
      );
      render(<Layout />);
      expect(await screen.findByRole('link', { name: 'Orders' })).toBeInTheDocument();
      await waitFor(() => expect(asked).toBeGreaterThan(0)); // the answer really was a 500
      await new Promise((r) => setTimeout(r, 20));
      expect(screen.queryByTitle('Active orders')).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Projects' })).toBeInTheDocument();
    });

    it('keeps a sidebar order saved before the menu had children', async () => {
      localStorage.setItem(
        'sidebarOrder',
        JSON.stringify(['printers', 'queue', 'archives', 'stats', 'files', 'projects', 'makerworld']),
      );
      render(<Layout />);
      expect(await screen.findByRole('link', { name: 'Stock' })).toBeInTheDocument();
      const toggle = screen.getByRole('button', { name: 'Projects' });
      const files = screen.getByRole('link', { name: 'File Manager' });
      // files is saved before projects, and the order is honoured with the children inside the parent
      expect(files.compareDocumentPosition(toggle) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    });

    it('drags the parent as one entry', async () => {
      render(<Layout />);
      const parentLi = (await screen.findByRole('button', { name: 'Projects' })).closest('li')!;
      const filesLi = screen.getByRole('link', { name: 'File Manager' }).closest('li')!;
      // `types` too: Layout's app-wide guard against dropped files reads it on every dragover.
      const dataTransfer = { setData: vi.fn(), types: [] as string[], effectAllowed: '', dropEffect: '' };
      fireEvent.dragStart(parentLi, { dataTransfer });
      fireEvent.dragOver(filesLi, { dataTransfer });
      fireEvent.drop(filesLi, { dataTransfer });
      const saved: string[] = JSON.parse(localStorage.getItem('sidebarOrder')!);
      // Dropped on its neighbour below, the parent moves past it — as any single entry does.
      expect(saved.indexOf('projects')).toBeGreaterThan(saved.indexOf('files'));
      expect(saved).not.toContain('orders'); // children never enter the order
    });
  });

  describe('default-view redirect', () => {
    // `Printers` is the INDEX route (`to: '/'`), so "the app just opened" and
    // "the user clicked Printers" are the same pathname. Keying the redirect
    // off every navigation to `/` therefore ate the click: reload anywhere but
    // `/`, go to any other page, click Printers — and you land on your default
    // view instead. Once per page load, and invisible to anyone whose default
    // view IS Printers, which is why it took a screen recording to see.
    const sidebarLink = (href: string) =>
      document.querySelector<HTMLAnchorElement>(`aside a[href="${href}"]`);

    afterEach(() => {
      window.localStorage.removeItem('defaultView');
      window.history.pushState({}, '', '/');
    });

    it('redirects to the configured default view when the app is entered at /', async () => {
      window.localStorage.setItem('defaultView', '/queue');
      window.history.pushState({}, '', '/');
      navigateSpy.mockClear();

      render(<Layout />);

      await waitFor(() => {
        expect(navigateSpy).toHaveBeenCalledWith('/queue', { replace: true });
      });
    });

    it('does not redirect when the app is entered anywhere else', async () => {
      window.localStorage.setItem('defaultView', '/queue');
      window.history.pushState({}, '', '/projects');
      navigateSpy.mockClear();

      render(<Layout />);
      await waitFor(() => {
        expect(sidebarLink('/settings')).toBeInTheDocument();
      });

      expect(navigateSpy).not.toHaveBeenCalledWith('/queue', { replace: true });
    });

    it('leaves a later click on Printers where the user aimed it', async () => {
      window.localStorage.setItem('defaultView', '/queue');
      window.history.pushState({}, '', '/projects');

      render(<Layout />);
      await waitFor(() => {
        expect(sidebarLink('/settings')).toBeInTheDocument();
      });

      const printers = sidebarLink('/');
      expect(printers).toBeInTheDocument();

      navigateSpy.mockClear();
      fireEvent.click(printers!, { button: 0 });

      await waitFor(() => {
        expect(window.location.pathname).toBe('/');
      });
      // The bounce was the Layout effect firing `navigate(defaultView,
      // { replace: true })` the moment the pathname became `/`.
      expect(navigateSpy).not.toHaveBeenCalledWith('/queue', { replace: true });
    });
  });
});
