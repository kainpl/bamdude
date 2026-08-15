/**
 * Tests for the FilamentHoverCard component.
 * Focuses on fill level display and Spoolman source indicator.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '../utils';
import { FilamentHoverCard } from '../../components/FilamentHoverCard';

const baseFilamentData = {
  vendor: 'Bambu Lab' as const,
  profile: 'PLA Basic',
  colorName: 'Red',
  colorHex: 'FF0000',
  kFactor: '0.030',
  fillLevel: 75,
  trayUuid: 'A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4',
};

function renderWithHover(ui: React.ReactElement) {
  const result = render(ui);
  // Trigger hover to show the card
  const trigger = result.container.firstElementChild as HTMLElement;
  fireEvent.mouseEnter(trigger);
  return result;
}

describe('FilamentHoverCard', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  describe('fill level display', () => {
    it('shows fill percentage when fillLevel is set', async () => {
      renderWithHover(
        <FilamentHoverCard data={{ ...baseFilamentData, fillLevel: 75 }}>
          <div>trigger</div>
        </FilamentHoverCard>
      );

      vi.advanceTimersByTime(100);

      await waitFor(() => {
        expect(screen.getByText('75%')).toBeInTheDocument();
      });
    });

    it('shows dash when fillLevel is null', async () => {
      renderWithHover(
        <FilamentHoverCard data={{ ...baseFilamentData, fillLevel: null }}>
          <div>trigger</div>
        </FilamentHoverCard>
      );

      vi.advanceTimersByTime(100);

      await waitFor(() => {
        expect(screen.getByText('-')).toBeInTheDocument();
      });
    });

    it('shows 0% when fillLevel is zero', async () => {
      renderWithHover(
        <FilamentHoverCard data={{ ...baseFilamentData, fillLevel: 0 }}>
          <div>trigger</div>
        </FilamentHoverCard>
      );

      vi.advanceTimersByTime(100);

      await waitFor(() => {
        expect(screen.getByText('0%')).toBeInTheDocument();
      });
    });
  });

  describe('Spoolman source indicator', () => {
    it('shows Spoolman label when fillSource is spoolman', async () => {
      renderWithHover(
        <FilamentHoverCard data={{ ...baseFilamentData, fillLevel: 80, fillSource: 'spoolman' }}>
          <div>trigger</div>
        </FilamentHoverCard>
      );

      vi.advanceTimersByTime(100);

      await waitFor(() => {
        expect(screen.getByText('(Spoolman)')).toBeInTheDocument();
      });
    });

    it('does not show Spoolman label when fillSource is ams', async () => {
      renderWithHover(
        <FilamentHoverCard data={{ ...baseFilamentData, fillLevel: 80, fillSource: 'ams' }}>
          <div>trigger</div>
        </FilamentHoverCard>
      );

      vi.advanceTimersByTime(100);

      await waitFor(() => {
        expect(screen.getByText('80%')).toBeInTheDocument();
        expect(screen.queryByText('(Spoolman)')).not.toBeInTheDocument();
      });
    });

    it('does not show Spoolman label when fillLevel is null', async () => {
      renderWithHover(
        <FilamentHoverCard data={{ ...baseFilamentData, fillLevel: null, fillSource: 'spoolman' }}>
          <div>trigger</div>
        </FilamentHoverCard>
      );

      vi.advanceTimersByTime(100);

      await waitFor(() => {
        expect(screen.getByText('-')).toBeInTheDocument();
        expect(screen.queryByText('(Spoolman)')).not.toBeInTheDocument();
      });
    });

    it('does not show Spoolman label when fillSource is undefined', async () => {
      renderWithHover(
        <FilamentHoverCard data={{ ...baseFilamentData, fillLevel: 50 }}>
          <div>trigger</div>
        </FilamentHoverCard>
      );

      vi.advanceTimersByTime(100);

      await waitFor(() => {
        expect(screen.getByText('50%')).toBeInTheDocument();
        expect(screen.queryByText('(Spoolman)')).not.toBeInTheDocument();
      });
    });
  });

  describe('hover behavior', () => {
    it('does not show card when disabled', () => {
      renderWithHover(
        <FilamentHoverCard data={baseFilamentData} disabled>
          <div>trigger</div>
        </FilamentHoverCard>
      );

      vi.advanceTimersByTime(100);

      // Card should not be visible
      expect(screen.queryByText('PLA Basic')).not.toBeInTheDocument();
    });

    it('shows filament details on hover', async () => {
      renderWithHover(
        <FilamentHoverCard data={baseFilamentData}>
          <div>trigger</div>
        </FilamentHoverCard>
      );

      vi.advanceTimersByTime(100);

      await waitFor(() => {
        expect(screen.getByText('Red')).toBeInTheDocument();
        expect(screen.getByText('PLA Basic')).toBeInTheDocument();
        expect(screen.getByText('0.030')).toBeInTheDocument();
      });
    });
  });

  // The card is portalled to <body> and positioned `fixed`. As an `absolute`
  // child of the trigger it was clipped by `<main>`'s `overflow-auto` — which
  // is what the old viewport-clamp existed to work around — but leaving the
  // trigger's subtree costs something the clamp never did.
  describe('portalled out of the trigger', () => {
    it('renders outside the trigger element', async () => {
      const { container } = renderWithHover(
        <FilamentHoverCard data={baseFilamentData}>
          <div>trigger</div>
        </FilamentHoverCard>
      );

      vi.advanceTimersByTime(100);

      await waitFor(() => expect(screen.getByText('PLA Basic')).toBeInTheDocument());
      expect(container.contains(screen.getByText('PLA Basic'))).toBe(false);
    });

    it('stays open while the pointer is on the card itself', async () => {
      // ⚠️ The whole reason the card carries the trigger's handlers. Inside the
      // trigger's subtree this was free: hovering the card WAS hovering the
      // trigger. Portalled, leaving the trigger starts the 100 ms close timer,
      // and reaching for Configure or Assign spool crosses that gap — so
      // without this the card closes under the cursor on its way to a button.
      const { container } = renderWithHover(
        <FilamentHoverCard data={baseFilamentData}>
          <div>trigger</div>
        </FilamentHoverCard>
      );
      vi.advanceTimersByTime(100);
      await waitFor(() => expect(screen.getByText('PLA Basic')).toBeInTheDocument());

      // ⚠️ The portal ROOT, by test id. Reaching for it with `closest('div[style]')`
      // finds an inner wrapper instead, and `mouseenter` does not bubble — so
      // that version of this test passed with the handlers deleted, which is
      // worse than not having it.
      fireEvent.mouseLeave(container.firstElementChild as HTMLElement);
      fireEvent.mouseEnter(screen.getByTestId('filament-hover-card'));
      vi.advanceTimersByTime(500);

      expect(screen.getByText('PLA Basic')).toBeInTheDocument();
    });

    it('closes once the pointer leaves the card too', async () => {
      renderWithHover(
        <FilamentHoverCard data={baseFilamentData}>
          <div>trigger</div>
        </FilamentHoverCard>
      );
      vi.advanceTimersByTime(100);
      await waitFor(() => expect(screen.getByText('PLA Basic')).toBeInTheDocument());

      const card = screen.getByTestId('filament-hover-card');
      fireEvent.mouseEnter(card);
      fireEvent.mouseLeave(card);
      vi.advanceTimersByTime(500);

      await waitFor(() => expect(screen.queryByText('PLA Basic')).not.toBeInTheDocument());
    });
  });

  // upstream #2631 — the card sits at z-[60] (so it can escape sibling printer
  // cards' stacking contexts), which puts it above the z-50 dialogs its own
  // buttons open. Nothing dismissed it, and a touch device never sends the
  // mouseleave that would, so on a tablet both layers stayed on screen.
  describe('dismiss before opening a dialog', () => {
    it('hides the card when Configure is clicked', async () => {
      const onConfigure = vi.fn();
      renderWithHover(
        <FilamentHoverCard data={baseFilamentData} configureSlot={{ enabled: true, onConfigure }}>
          <div>trigger</div>
        </FilamentHoverCard>
      );

      vi.advanceTimersByTime(100);
      await waitFor(() => expect(screen.getByText('PLA Basic')).toBeInTheDocument());

      fireEvent.click(screen.getByTitle('Configure Slot'));

      expect(onConfigure).toHaveBeenCalledTimes(1);
      await waitFor(() => expect(screen.queryByText('PLA Basic')).not.toBeInTheDocument());
    });

    it('stays dismissed even if a pending show timer was queued', async () => {
      const onConfigure = vi.fn();
      const result = renderWithHover(
        <FilamentHoverCard data={baseFilamentData} configureSlot={{ enabled: true, onConfigure }}>
          <div>trigger</div>
        </FilamentHoverCard>
      );

      vi.advanceTimersByTime(100);
      await waitFor(() => expect(screen.getByText('PLA Basic')).toBeInTheDocument());

      // Re-enter queues an 80ms show timer; the dismiss must clear it, otherwise
      // the card pops back over the dialog it just opened.
      fireEvent.mouseEnter(result.container.firstElementChild as HTMLElement);
      fireEvent.click(screen.getByTitle('Configure Slot'));
      vi.advanceTimersByTime(200);

      await waitFor(() => expect(screen.queryByText('PLA Basic')).not.toBeInTheDocument());
    });
  });
});
