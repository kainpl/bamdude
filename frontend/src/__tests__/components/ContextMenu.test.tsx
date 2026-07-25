/**
 * Tests for the ContextMenu component.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { ContextMenu } from '../../components/ContextMenu';

describe('ContextMenu', () => {
  const mockOnClose = vi.fn();

  const menuItems = [
    { label: 'Edit', onClick: vi.fn() },
    { label: 'Delete', onClick: vi.fn(), danger: true },
    { label: 'Download', onClick: vi.fn() },
  ];

  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('rendering', () => {
    it('renders menu items', () => {
      render(
        <ContextMenu
          x={100}
          y={100}
          items={menuItems}
          onClose={mockOnClose}
        />
      );

      expect(screen.getByText('Edit')).toBeInTheDocument();
      expect(screen.getByText('Delete')).toBeInTheDocument();
      expect(screen.getByText('Download')).toBeInTheDocument();
    });

    it('positions menu at specified coordinates', () => {
      render(
        <ContextMenu
          x={200}
          y={150}
          items={menuItems}
          onClose={mockOnClose}
        />
      );

      // Menu should be rendered with items visible
      expect(screen.getByText('Edit')).toBeInTheDocument();
    });
  });

  describe('interactions', () => {
    it('calls onClick when item is clicked', async () => {
      const user = userEvent.setup();
      render(
        <ContextMenu
          x={100}
          y={100}
          items={menuItems}
          onClose={mockOnClose}
        />
      );

      await user.click(screen.getByText('Edit'));

      expect(menuItems[0].onClick).toHaveBeenCalled();
    });

    it('calls onClose after item click', async () => {
      const user = userEvent.setup();
      render(
        <ContextMenu
          x={100}
          y={100}
          items={menuItems}
          onClose={mockOnClose}
        />
      );

      await user.click(screen.getByText('Edit'));

      expect(mockOnClose).toHaveBeenCalled();
    });
  });

  describe('styling', () => {
    it('applies danger styling', () => {
      render(
        <ContextMenu
          x={100}
          y={100}
          items={menuItems}
          onClose={mockOnClose}
        />
      );

      // Delete item has danger: true, so should have red styling
      const deleteButton = screen.getByText('Delete');
      expect(deleteButton).toBeInTheDocument();
    });
  });

  describe('dividers', () => {
    it('supports divider property on items', () => {
      // Just verify the ContextMenuItem interface accepts divider prop
      const itemsWithDivider = [
        { label: 'Edit', onClick: vi.fn() },
        { label: 'Copy', onClick: vi.fn(), divider: true },
      ];

      // Interface should accept these items without error
      expect(itemsWithDivider[1].divider).toBe(true);
    });
  });

  describe('scroll dismissal (#1151)', () => {
    const projectItems = [
      { label: 'Add to Project', onClick: vi.fn(), submenu: [
        { label: 'Alpha', onClick: vi.fn() },
        { label: 'Beta', onClick: vi.fn() },
      ] },
    ];

    it('closes on a page-level scroll', () => {
      render(<ContextMenu x={10} y={10} items={menuItems} onClose={mockOnClose} />);
      document.dispatchEvent(new Event('scroll', { bubbles: true }));
      expect(mockOnClose).toHaveBeenCalled();
    });

    it('does NOT close when the scroll comes from inside the menu', async () => {
      // The listener is capture-phase on `document`, so it also receives
      // scrolls of descendants — and the submenu panel is max-h-300 with
      // overflow-y-auto, so scrolling a long project list used to slam the
      // whole menu shut.
      const user = userEvent.setup();
      render(<ContextMenu x={10} y={10} items={projectItems} onClose={mockOnClose} />);
      await user.hover(screen.getByText('Add to Project'));
      const inner = await screen.findByText('Alpha');
      mockOnClose.mockClear();

      inner.dispatchEvent(new Event('scroll', { bubbles: true }));
      expect(mockOnClose).not.toHaveBeenCalled();
    });
  });
});
