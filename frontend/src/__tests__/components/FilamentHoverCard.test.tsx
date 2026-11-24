/**
 * Tests for the FilamentHoverCard component.
 * Focuses on fill level display and Spoolman source indicator.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '../utils';
import { FilamentHoverCard, EmptySlotHoverCard } from '../../components/FilamentHoverCard';
import { setColorCatalog, __resetColorCatalogForTests } from '../../utils/colors';

const baseFilamentData = {
  vendor: 'Bambu Lab' as const,
  profile: 'PLA Basic',
  colorName: 'Red',
  colorHex: 'FF0000',
  kFactor: '0.030',
  fillLevel: 75,
  trayUuid: 'A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4',
};

function pointer(target: Element, type: string, pointerType: 'mouse' | 'touch' | 'pen') {
  const event = new MouseEvent(type, { bubbles: true, relatedTarget: document.body });
  fireEvent(target, Object.assign(event, { pointerType }));
}

function mouseEnter(target: Element) { pointer(target, 'pointerover', 'mouse'); }
function mouseLeave(target: Element) { pointer(target, 'pointerout', 'mouse'); }

function tap(target: Element) {
  pointer(target, 'pointerdown', 'touch');
  fireEvent.click(target);
}

function renderWithHover(ui: React.ReactElement) {
  const result = render(ui);
  // Trigger hover to show the card
  const trigger = result.container.firstElementChild as HTMLElement;
  mouseEnter(trigger);
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
      mouseLeave(container.firstElementChild as HTMLElement);
      mouseEnter(screen.getByTestId('filament-hover-card'));
      vi.advanceTimersByTime(500);

      expect(screen.getByText('PLA Basic')).toBeInTheDocument();
    });

    // Like the bed-jog menu on the printer card, the card rides the page's own
    // scroll: placed once in DOCUMENT coordinates (+ scrollY), `absolute` in
    // <body>, so the browser moves it with its slot. A `fixed` card chased the
    // slot from a scroll listener and visibly floated over the page.
    it.each([
      ['filament', 'filament-hover-card'],
      ['empty slot', 'empty-slot-hover-card'],
    ])('%s card: placed in document coordinates, not chasing the scroll', async (_kind, testId) => {
      const ui =
        testId === 'filament-hover-card' ? (
          <FilamentHoverCard data={baseFilamentData}>
            <div>trigger</div>
          </FilamentHoverCard>
        ) : (
          <EmptySlotHoverCard configureSlot={{ enabled: true, onConfigure: vi.fn() }} onAssignSpool={vi.fn()}>
            <div>trigger</div>
          </EmptySlotHoverCard>
        );
      const result = render(ui);
      const trigger = result.container.firstElementChild as HTMLElement;
      const rect = (top: number) => ({ top, bottom: top + 20, left: 400, right: 420, width: 20, height: 20, x: 400, y: top, toJSON: () => ({}) });
      trigger.getBoundingClientRect = () => rect(300) as DOMRect;
      Object.defineProperty(window, 'scrollY', { configurable: true, value: 500 });
      try {
        mouseEnter(trigger);
        vi.advanceTimersByTime(100);
        const card = await screen.findByTestId(testId);
        await waitFor(() => expect(card.style.visibility).toBe('visible'));
        // jsdom lays out nothing, so the card measures 0×0: it goes above the
        // trigger (300 − 0 − 8 gap) and centres on it (410); + 500 scrolled.
        expect(card).toHaveClass('absolute');
        expect(card).not.toHaveClass('fixed');
        expect(card.style.top).toBe('792px');
        expect(card.style.left).toBe('410px');

        trigger.getBoundingClientRect = () => rect(100) as DOMRect;
        fireEvent.scroll(window);
        expect(card.style.top).toBe('792px');
      } finally {
        Object.defineProperty(window, 'scrollY', { configurable: true, value: 0 });
      }
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
      mouseEnter(card);
      mouseLeave(card);
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
      mouseEnter(result.container.firstElementChild as HTMLElement);
      fireEvent.click(screen.getByTitle('Configure Slot'));
      vi.advanceTimersByTime(200);

      await waitFor(() => expect(screen.queryByText('PLA Basic')).not.toBeInTheDocument());
    });
  });

  // ⚠️ Paired with the EmptySlotHoverCard assertion below. Together they pin
  // the TWO render paths to the same Assign-then-Configure order — separately,
  // either could move and the menu would reshuffle itself depending on whether
  // the slot happened to hold filament, which is exactly what it did.
  it('lists Assign Spool above Configure', async () => {
    renderWithHover(
      <FilamentHoverCard
        data={baseFilamentData}
        inventory={{ assignedSpool: null, onAssignSpool: vi.fn() }}
        configureSlot={{ enabled: true, onConfigure: vi.fn() }}
      >
        <div>trigger</div>
      </FilamentHoverCard>
    );
    vi.advanceTimersByTime(100);
    await waitFor(() => expect(screen.getByText(/assign spool/i)).toBeInTheDocument());

    const assign = screen.getByText(/assign spool/i);
    const configure = screen.getByText(/^configure$/i);
    expect(assign.compareDocumentPosition(configure)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });

  // Spoolman mode with Spoolman unreachable: neither inventory can take an
  // assignment, and a control that vanishes reads as a permissions problem.
  it('shows Assign disabled with the reason when no inventory can take it', async () => {
    renderWithHover(
      <FilamentHoverCard
        data={baseFilamentData}
        inventory={{ assignedSpool: null, unavailableReason: 'Spoolman is unreachable' }}
      >
        <div>trigger</div>
      </FilamentHoverCard>
    );
    vi.advanceTimersByTime(100);
    await waitFor(() => expect(screen.getByText(/assign spool/i)).toBeInTheDocument());

    expect(screen.getByText(/assign spool/i).closest('button')).toBeDisabled();
    expect(screen.getByText('Spoolman is unreachable')).toBeInTheDocument();
    expect(screen.queryByText(/unassign/i)).not.toBeInTheDocument();
  });

  it('a blank slot that holds a spool names it and offers Replace and Unassign', async () => {
    const onAssignSpool = vi.fn();
    const onUnassign = vi.fn();
    const result = render(
      <EmptySlotHoverCard
        emptyKind="reset"
        configureSlot={{ enabled: true, onConfigure: vi.fn() }}
        onAssignSpool={onAssignSpool}
        assigned={{ label: 'Acme PETG Grey', colorHex: '858585FF', onUnassign }}
      >
        <div>trigger</div>
      </EmptySlotHoverCard>
    );
    mouseEnter(result.container.firstElementChild as HTMLElement);
    vi.advanceTimersByTime(100);
    await waitFor(() => expect(screen.getByText('Acme PETG Grey')).toBeInTheDocument());

    expect(screen.getByText(/the printer has not taken it yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/^assign spool$/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByText(/replace spool/i));
    expect(onAssignSpool).toHaveBeenCalledTimes(1);
  });

  it('Unassign on a blank slot calls its handler', async () => {
    const onUnassign = vi.fn();
    const result = render(
      <EmptySlotHoverCard assigned={{ label: 'Acme PETG Grey', onUnassign }}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );
    mouseEnter(result.container.firstElementChild as HTMLElement);
    vi.advanceTimersByTime(100);
    await waitFor(() => expect(screen.getByText('Acme PETG Grey')).toBeInTheDocument());

    fireEvent.click(screen.getByText(/unassign/i));
    expect(onUnassign).toHaveBeenCalledTimes(1);
  });

  it('lists Assign Spool above Configure on an empty slot too', async () => {
    const result = render(
      <EmptySlotHoverCard
        configureSlot={{ enabled: true, onConfigure: vi.fn() }}
        onAssignSpool={vi.fn()}
      >
        <div>trigger</div>
      </EmptySlotHoverCard>
    );
    mouseEnter(result.container.firstElementChild as HTMLElement);
    vi.advanceTimersByTime(100);
    await waitFor(() => expect(screen.getByText(/assign spool/i)).toBeInTheDocument());

    const assign = screen.getByText(/assign spool/i);
    const configure = screen.getByText(/^configure$/i);
    expect(assign.compareDocumentPosition(configure)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });

  // An operator putting a new spool into an occupied slot had to Unassign
  // first and only then got an Assign button — a click and a wait per slot,
  // on every printer of the farm. The servers have always replaced on an
  // occupied slot (spec 2026-09-13 §1), so the only missing piece was this
  // button. It reuses `onAssignSpool`: the assign dialog, opened over an
  // assigned slot, IS the replace dialog.
  describe('replace the assigned spool', () => {
    const assignedSpool = {
      id: 7,
      material: 'PLA',
      brand: 'Polymaker',
      color_name: 'Red',
      displayName: 'Polymaker PLA Red',
    };

    it('offers Replace before Unassign on an assigned slot', async () => {
      const onAssignSpool = vi.fn();
      const onUnassignSpool = vi.fn();
      renderWithHover(
        <FilamentHoverCard
          data={baseFilamentData}
          inventory={{ assignedSpool, onAssignSpool, onUnassignSpool }}
        >
          <div>trigger</div>
        </FilamentHoverCard>
      );
      vi.advanceTimersByTime(100);
      await waitFor(() => expect(screen.getByText(/replace spool/i)).toBeInTheDocument());

      const replace = screen.getByText(/replace spool/i);
      const unassign = screen.getByText(/^unassign$/i);
      expect(replace.compareDocumentPosition(unassign)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    });

    it('clicking Replace opens the assign dialog and unassigns nothing', async () => {
      const onAssignSpool = vi.fn();
      const onUnassignSpool = vi.fn();
      renderWithHover(
        <FilamentHoverCard
          data={baseFilamentData}
          inventory={{ assignedSpool, onAssignSpool, onUnassignSpool }}
        >
          <div>trigger</div>
        </FilamentHoverCard>
      );
      vi.advanceTimersByTime(100);
      await waitFor(() => expect(screen.getByText(/replace spool/i)).toBeInTheDocument());

      fireEvent.click(screen.getByText(/replace spool/i));

      expect(onAssignSpool).toHaveBeenCalledTimes(1);
      expect(onUnassignSpool).not.toHaveBeenCalled();
      // Same dismiss-before-the-dialog rule as every other button here: the
      // card sits above the z-50 dialog it opens.
      await waitFor(() => expect(screen.queryByText(/replace spool/i)).not.toBeInTheDocument());
    });

    it('offers no Replace where Assign would not be offered either', async () => {
      // The internal branch's `filamentData.vendor !== 'Bambu Lab'` gate on the
      // page leaves `onAssignSpool` undefined; Replace must follow it exactly.
      const onUnassignSpool = vi.fn();
      renderWithHover(
        <FilamentHoverCard data={baseFilamentData} inventory={{ assignedSpool, onUnassignSpool }}>
          <div>trigger</div>
        </FilamentHoverCard>
      );
      vi.advanceTimersByTime(100);
      await waitFor(() => expect(screen.getByText(/^unassign$/i)).toBeInTheDocument());

      expect(screen.queryByText(/replace spool/i)).not.toBeInTheDocument();
    });

    it('an empty slot still offers Assign and no Replace', async () => {
      const onAssignSpool = vi.fn();
      renderWithHover(
        <FilamentHoverCard data={baseFilamentData} inventory={{ assignedSpool: null, onAssignSpool }}>
          <div>trigger</div>
        </FilamentHoverCard>
      );
      vi.advanceTimersByTime(100);
      await waitFor(() => expect(screen.getByText(/assign spool/i)).toBeInTheDocument());

      expect(screen.queryByText(/replace spool/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/^unassign$/i)).not.toBeInTheDocument();
    });
  });

});

describe('FilamentHoverCard colour name (#2875)', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    __resetColorCatalogForTests();
  });

  const whiteMatte = {
    ...baseFilamentData,
    profile: 'Bambu PLA Matte',
    colorHex: 'FFFFFFFF',
    // What PrintersPage now resolves with the slot's own tray_sub_brands.
    colorName: 'Ivory White',
  };

  async function showCard(ui: React.ReactElement) {
    renderWithHover(ui);
    vi.advanceTimersByTime(100);
  }

  it('shows the resolved catalogue name for the slot', async () => {
    await showCard(
      <FilamentHoverCard data={whiteMatte}>
        <div>trigger</div>
      </FilamentHoverCard>
    );

    await waitFor(() => expect(screen.getByText('Ivory White')).toBeInTheDocument());
    expect(screen.queryByText('Jade White')).not.toBeInTheDocument();
  });

  it('prefers the assigned spool name, which is what the user put in the slot', async () => {
    await showCard(
      <FilamentHoverCard
        data={{ ...whiteMatte, colorName: 'Jade White' }}
        inventory={{
          isAssigned: true,
          assignedSpool: { id: 17, material: 'PLA', brand: 'Bambu Lab', color_name: 'Matte Ivory White', displayName: 'Bambu Lab PLA #17' },
        }}
      >
        <div>trigger</div>
      </FilamentHoverCard>
    );

    await waitFor(() => expect(screen.getByText('Matte Ivory White')).toBeInTheDocument());
    expect(screen.queryByText('Jade White')).not.toBeInTheDocument();
  });

  it('ignores a Bambu internal colour code on the assigned spool', async () => {
    // "A06-D0" is not a name, and is not unique across material families
    // (#857) -- the catalogue answer stands.
    await showCard(
      <FilamentHoverCard
        data={whiteMatte}
        inventory={{
          isAssigned: true,
          assignedSpool: { id: 17, material: 'PLA', brand: 'Bambu Lab', color_name: 'A06-D0', displayName: 'Bambu Lab PLA #17' },
        }}
      >
        <div>trigger</div>
      </FilamentHoverCard>
    );

    await waitFor(() => expect(screen.getByText('Ivory White')).toBeInTheDocument());
    expect(screen.queryByText('A06-D0')).not.toBeInTheDocument();
  });

  it.each([
    ['an empty colour name', ''],
    ['a whitespace-only colour name', '   '],
  ])('keeps the catalogue answer for a spool with %s', async (_label, colorName) => {
    await showCard(
      <FilamentHoverCard
        data={whiteMatte}
        inventory={{
          isAssigned: true,
          assignedSpool: { id: 17, material: 'PLA', brand: 'Bambu Lab', color_name: colorName, displayName: 'Bambu Lab PLA #17' },
        }}
      >
        <div>trigger</div>
      </FilamentHoverCard>
    );

    await waitFor(() => expect(screen.getByText('Ivory White')).toBeInTheDocument());
  });

  it('keeps the catalogue answer when a spool is assigned with no colour recorded', async () => {
    setColorCatalog({ ffffff: 'Jade White' }, { 'pla matte|ffffff': 'Ivory White' });

    await showCard(
      <FilamentHoverCard
        data={whiteMatte}
        inventory={{
          isAssigned: true,
          assignedSpool: { id: 17, material: 'PLA', brand: 'Bambu Lab', color_name: null, displayName: 'Bambu Lab PLA #17' },
        }}
      >
        <div>trigger</div>
      </FilamentHoverCard>
    );

    await waitFor(() => expect(screen.getByText('Ivory White')).toBeInTheDocument());
  });
});

/**
 * The header swatch (#2967).
 *
 * A tray record carries one `tray_color` hex and nothing else, so telemetry can
 * never describe a gradient or a surface effect. The reporter's Ziro "Colorful
 * Mist" -- yellow, cyan and pink, effect Tri Color -- painted the header as one
 * flat band of the single hex the slot happened to be configured with. The
 * spool knows better, and the header now paints the spool's own swatch through
 * the same builder the Inventory swatches use.
 */
describe('FilamentHoverCard — assigned spool swatch (#2967)', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  const triColorSpool = {
    id: 191,
    material: 'PLA',
    subtype: 'Matte',
    brand: 'Ziro',
    color_name: 'Colorful Mist',
    rgba: 'FFB6C1FF',
    extra_colors: 'ffff00,00ffff,ffb6c1',
    effect_type: 'tri-color',
    displayName: 'Ziro PLA Matte #191',
  };

  // The swatch header is the card's first child: a fixed-height block carrying
  // the colour and the name. Queried off `document` rather than the render
  // container because the card is portaled into document.body.
  function header(): HTMLElement {
    const el = document.querySelector('.h-12') as HTMLElement | null;
    expect(el, 'header block not found').not.toBeNull();
    return el as HTMLElement;
  }

  it('paints the gradient for a multi-colour spool', async () => {
    renderWithHover(
      <FilamentHoverCard data={baseFilamentData} inventory={{ assignedSpool: triColorSpool }}>
        <div>trigger</div>
      </FilamentHoverCard>
    );
    vi.advanceTimersByTime(100);
    await waitFor(() => expect(screen.getByText('Colorful Mist')).toBeInTheDocument());
    expect(header().style.backgroundImage).toContain('gradient');
  });

  it('carries every stop the spool declared', async () => {
    renderWithHover(
      <FilamentHoverCard data={baseFilamentData} inventory={{ assignedSpool: triColorSpool }}>
        <div>trigger</div>
      </FilamentHoverCard>
    );
    vi.advanceTimersByTime(100);
    await waitFor(() => expect(screen.getByText('Colorful Mist')).toBeInTheDocument());
    const image = header().style.backgroundImage.toLowerCase();
    expect(image).toContain('#ffff00');
    expect(image).toContain('#00ffff');
    expect(image).toContain('#ffb6c1');
  });

  it('leaves a plain single-colour spool on the flat slot colour', async () => {
    // The common case must not go anywhere near the gradient machinery.
    renderWithHover(
      <FilamentHoverCard
        data={baseFilamentData}
        inventory={{
          assignedSpool: { ...triColorSpool, extra_colors: null, effect_type: null },
        }}
      >
        <div>trigger</div>
      </FilamentHoverCard>
    );
    vi.advanceTimersByTime(100);
    await waitFor(() => expect(screen.getByText('Colorful Mist')).toBeInTheDocument());
    expect(header().style.backgroundImage).toBe('');
  });

  it('paints the swatch for an effect with no extra colours', async () => {
    // A silk roll is one colour with a surface the hex cannot express.
    renderWithHover(
      <FilamentHoverCard
        data={baseFilamentData}
        inventory={{ assignedSpool: { ...triColorSpool, extra_colors: null, effect_type: 'silk' } }}
      >
        <div>trigger</div>
      </FilamentHoverCard>
    );
    vi.advanceTimersByTime(100);
    await waitFor(() => expect(screen.getByText('Colorful Mist')).toBeInTheDocument());
    expect(header().style.backgroundImage).not.toBe('');
  });

  it('puts the name on a scrim when the background has several bands', async () => {
    // One hex cannot decide legibility across yellow, cyan and pink, and the
    // label sits dead centre where the background is most likely to change.
    renderWithHover(
      <FilamentHoverCard data={baseFilamentData} inventory={{ assignedSpool: triColorSpool }}>
        <div>trigger</div>
      </FilamentHoverCard>
    );
    vi.advanceTimersByTime(100);
    await waitFor(() => expect(screen.getByText('Colorful Mist')).toBeInTheDocument());
    expect(screen.getByText('Colorful Mist').className).toContain('bg-black/60');
  });

  it('does not scrim a single-colour spool', async () => {
    renderWithHover(
      <FilamentHoverCard
        data={baseFilamentData}
        inventory={{
          assignedSpool: { ...triColorSpool, extra_colors: null, effect_type: null },
        }}
      >
        <div>trigger</div>
      </FilamentHoverCard>
    );
    vi.advanceTimersByTime(100);
    await waitFor(() => expect(screen.getByText('Colorful Mist')).toBeInTheDocument());
    expect(screen.getByText('Colorful Mist').className).not.toContain('bg-black/60');
  });

  it('keeps a slot with no assigned spool exactly as it was', async () => {
    renderWithHover(
      <FilamentHoverCard data={baseFilamentData} inventory={{ assignedSpool: null }}>
        <div>trigger</div>
      </FilamentHoverCard>
    );
    vi.advanceTimersByTime(100);
    await waitFor(() => expect(screen.getByText('Red')).toBeInTheDocument());
    expect(header().style.backgroundImage).toBe('');
  });

  it('tolerates a spool that predates the swatch fields', async () => {
    // Older callers send neither rgba nor extra_colors; the optional fields
    // must not turn that into a blank or a crash.
    renderWithHover(
      <FilamentHoverCard
        data={baseFilamentData}
        inventory={{
          assignedSpool: { id: 1, material: 'PLA', subtype: null, brand: 'Ziro', color_name: 'Mist', displayName: 'Ziro PLA #1' },
        }}
      >
        <div>trigger</div>
      </FilamentHoverCard>
    );
    vi.advanceTimersByTime(100);
    await waitFor(() => expect(screen.getByText('Mist')).toBeInTheDocument());
    expect(header().style.backgroundImage).toBe('');
  });
});


describe('slot activation without a hover', () => {
  it.each(['filled', 'empty'])('opens the %s slot on the first touch of its text', (kind) => {
    render(kind === 'filled'
      ? <FilamentHoverCard data={baseFilamentData}><span>Slot text</span></FilamentHoverCard>
      : <EmptySlotHoverCard><span>Slot text</span></EmptySlotHoverCard>);

    const target = screen.getByText('Slot text');
    tap(target);

    expect(screen.getByTestId(kind === 'filled' ? 'filament-hover-card' : 'empty-slot-hover-card')).toBeVisible();
  });

  it('opens a slot from the keyboard', () => {
    render(<FilamentHoverCard data={baseFilamentData}><span>Slot text</span></FilamentHoverCard>);
    const trigger = screen.getByText('Slot text').parentElement!;
    fireEvent.keyDown(trigger, { key: 'Enter' });
    expect(screen.getByTestId('filament-hover-card')).toBeVisible();
  });
});


describe('slot card dismissal and actions', () => {
  it('ignores emulated mouse hover, toggles by tap and closes outside', async () => {
    render(<FilamentHoverCard data={baseFilamentData}><span>Touch slot</span></FilamentHoverCard>);
    const text = screen.getByText('Touch slot');
    const tile = screen.getByRole('button', { name: 'Touch slot' });
    pointer(tile, 'pointerover', 'touch');
    fireEvent.mouseEnter(tile);
    tap(text);
    expect(tile).toHaveAttribute('aria-expanded', 'true');
    const card = screen.getByRole('dialog', { name: 'Slot details' });
    expect(tile).toHaveAttribute('aria-controls', card.id);

    tap(screen.getByText('PLA Basic'));
    expect(card).toBeVisible();
    tap(text);
    expect(tile).toHaveAttribute('aria-expanded', 'false');
    expect(card).not.toBeInTheDocument();

    tap(text);
    pointer(document.body, 'pointerdown', 'touch');
    expect(screen.queryByRole('dialog', { name: 'Slot details' })).not.toBeInTheDocument();
  });

  it.each(['filled', 'empty'])('focuses a usable %s-slot action and returns on Escape', async (kind) => {
    const configure = vi.fn();
    const config = { enabled: true, onConfigure: configure };
    render(kind === 'filled'
      ? <FilamentHoverCard data={baseFilamentData} configureSlot={config}><span>Slot label</span></FilamentHoverCard>
      : <EmptySlotHoverCard configureSlot={config}><span>Slot label</span></EmptySlotHoverCard>);
    const tile = screen.getByRole('button', { name: 'Slot label' });
    tile.focus();
    fireEvent.keyDown(tile, { key: ' ' });
    const dialog = screen.getByRole('dialog', { name: 'Slot details' });
    await waitFor(() => expect(dialog).toContainElement(document.activeElement as HTMLElement));

    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    expect(dialog).not.toBeInTheDocument();
    expect(tile).toHaveFocus();

    tap(tile);
    tap(screen.getByRole('button', { name: 'Configure' }));
    expect(configure).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('dialog', { name: 'Slot details' })).not.toBeInTheDocument();
  });

  it('places a tall empty-slot card below a trigger near the top and limits its height', () => {
    render(<EmptySlotHoverCard assigned={{ label: 'Long inventory name' }}><span>Compact slot</span></EmptySlotHoverCard>);
    const tile = screen.getByRole('button', { name: 'Compact slot' });
    tile.getBoundingClientRect = () => ({ top: 75, bottom: 99, left: 18, right: 42, width: 24, height: 24 }) as DOMRect;
    const height = vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(1_000);
    try {
      tap(tile);
      const card = screen.getByRole('dialog', { name: 'Slot details' });
      expect(card.style.top).toBe('107px');
      expect((card.firstElementChild as HTMLElement).style.maxHeight).toBe(String(window.innerHeight - 115) + 'px');
    } finally {
      height.mockRestore();
    }
  });
});
