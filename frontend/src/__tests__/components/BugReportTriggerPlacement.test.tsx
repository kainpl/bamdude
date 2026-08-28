/**
 * Where the bug-report trigger lives, and why it moved.
 *
 * The floating disc is pinned bottom-right, which is where most controls live.
 * On the Profiles page it sat on the scroll-to-top button at the same z-index,
 * so which one you could click came down to DOM order — and being
 * viewport-fixed it also covers in-flow card buttons that scroll under it.
 * Below the sidebar-compact breakpoint the trigger moves into the top bar.
 *
 * ⚠️ Not a hide switch. The bubble is the only entry to the report form, and
 * that form runs the printer diagnostic, the log scan and the debug capture —
 * hiding it would yield reports with nothing attached. So the panel must stay
 * reachable at every width.
 *
 * ⚠️ And the panel must stay at the Layout root, not move into the header with
 * its button: the header is `fixed z-40` and therefore its own stacking
 * context, which would cap the z-50 panel at the header's level and bury it
 * under every ordinary modal.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';

import { render } from '../utils';

import { BugReportBubble } from '../../components/BugReportBubble';
import layoutSource from '../../components/Layout.tsx?raw';
import profilesSource from '../../pages/ProfilesPage.tsx?raw';

vi.mock('../../components/ConnectionDiagnostic', () => ({ DiagnosticChecklist: () => null }));
vi.mock('../../components/SystemHealthPanel', () => ({ SystemHealthPanel: () => null }));
vi.mock('../../components/Collapsible', () => ({ Collapsible: () => null }));

describe('the floating disc', () => {
  beforeEach(() => vi.clearAllMocks());

  it('is drawn by default', () => {
    render(<BugReportBubble />);

    expect(screen.getByTitle(/report a bug/i)).toBeInTheDocument();
  });

  it('can be suppressed without unmounting the panel', () => {
    // The panel's own state and effects have to keep living here even when
    // something else owns the button.
    render(<BugReportBubble showTrigger={false} />);

    expect(screen.queryByTitle(/report a bug/i)).not.toBeInTheDocument();
  });

  it('opens from a controlled flag, so another component can be the trigger', () => {
    render(<BugReportBubble showTrigger={false} open onOpenChange={vi.fn()} />);

    expect(screen.getByText(/report a bug/i)).toBeInTheDocument();
  });
});

describe('Layout wires the two placements', () => {
  it('hides the disc exactly where the compact header appears', () => {
    expect(layoutSource).toContain('showTrigger={!isSidebarCompact}');
  });

  it('gives the compact header its own trigger, so the form is never unreachable', () => {
    const header = layoutSource.slice(layoutSource.indexOf('{isSidebarCompact && ('));
    expect(header).toContain('setBugReportOpen(true)');
  });

  it('keeps the panel at the Layout root', () => {
    // ⚠️ If this ever moves inside the `fixed z-40` header, the panel is
    // capped at z-40 and disappears under every modal in the app.
    const headerStart = layoutSource.indexOf('{isSidebarCompact && (');
    const headerEnd = layoutSource.indexOf('</header>', headerStart);
    const panel = layoutSource.indexOf('<BugReportBubble');
    expect(panel).toBeGreaterThan(headerEnd);
  });
});

describe('the corner it was sharing', () => {
  it('moves the Profiles scroll-to-top button clear of it', () => {
    // Both are z-40 and both are 16–64px from the two edges, so they overlap
    // almost entirely and DOM order decided the winner.
    expect(profilesSource).toContain('fixed bottom-24 right-6');
    expect(profilesSource).not.toContain('fixed bottom-6 right-6');
  });
});
