/**
 * One control, under the rows. Two things about it are load-bearing:
 *
 * 1. It survives a single page. That is the whole reason page size and page
 *    number were merged — with the bar gone, "24 of 24" has no control left
 *    that could ask for more.
 * 2. The arrows agree with the page they show. An off-by-one here sends the
 *    operator to a page they did not ask for, which on the last page means an
 *    empty screen.
 */

import { describe, it, expect, vi } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PaginationBar } from '../../components/PaginationBar';

const setup = (props: Partial<React.ComponentProps<typeof PaginationBar>> = {}) => {
  const onPageChange = vi.fn();
  const onPerPageChange = vi.fn();
  render(
    <PaginationBar
      page={2}
      totalPages={4}
      perPage={24}
      total={96}
      items="archives"
      onPageChange={onPageChange}
      onPerPageChange={onPerPageChange}
      {...props}
    />,
  );
  return { onPageChange, onPerPageChange };
};

describe('PaginationBar', () => {
  it('reports the range, the total and the page', () => {
    setup();

    expect(screen.getByText('Showing 25-48 of 96 archives')).toBeInTheDocument();
    expect(screen.getByText('Page 2 of 4')).toBeInTheDocument();
  });

  it('keeps the page-size control on a single page, without the arrows', () => {
    // The reason the two controls were merged. Hiding the bar whole would take
    // away the only way to ask for more rows.
    setup({ page: 1, totalPages: 1, perPage: 24, total: 8 });

    expect(screen.getByRole('combobox', { name: /show/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /next page/i })).not.toBeInTheDocument();
  });

  it('steps one page at a time and jumps to the ends', async () => {
    const { onPageChange } = setup();

    await userEvent.click(screen.getByRole('button', { name: /next page/i }));
    expect(onPageChange).toHaveBeenLastCalledWith(3);

    await userEvent.click(screen.getByRole('button', { name: /previous page/i }));
    expect(onPageChange).toHaveBeenLastCalledWith(1);

    await userEvent.click(screen.getByRole('button', { name: /last page/i }));
    expect(onPageChange).toHaveBeenLastCalledWith(4);

    await userEvent.click(screen.getByRole('button', { name: /first page/i }));
    expect(onPageChange).toHaveBeenLastCalledWith(1);
  });

  it('will not step past either end', async () => {
    setup({ page: 4, totalPages: 4 });

    expect(screen.getByRole('button', { name: /next page/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /last page/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /previous page/i })).toBeEnabled();
  });

  it('drops the page controls and counts everything on "All"', () => {
    setup({ perPage: -1, page: 1, totalPages: 1, total: 96 });

    expect(screen.getByText('96 archives')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /next page/i })).not.toBeInTheDocument();
  });

  it('renders nothing at all when there are no rows', () => {
    // The empty state owns that screen; a bar reading "0-0 of 0" under it is
    // noise on top of an explanation.
    setup({ page: 1, totalPages: 0, total: 0 });

    expect(document.querySelector('[data-pagination]')).toBeNull();
  });

  it('keeps both questions in one container', async () => {
    // The defect being fixed: page size lived in the filter panel and the
    // arrows near the title, so changing one meant looking elsewhere to see
    // what it did. "Both rendered somewhere" would have passed then too.
    setup();

    const bar = document.querySelector('[data-pagination]') as HTMLElement;
    expect(within(bar).getByRole('combobox', { name: /show/i })).toBeInTheDocument();
    expect(within(bar).getByRole('button', { name: /next page/i })).toBeInTheDocument();
  });
});
