/**
 * Busy is a property of the ROW, not of the list.
 *
 * One `busy` flag covered every attachment: downloading a large spec disabled
 * the download button of all ten rows, and the section read as broken when one
 * slow request was in flight. The delete button had the same shape through
 * `remove.isPending` — one mutation shared by every row cannot say which row
 * asked.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { render } from '../../utils';
import { api } from '../../../api/client';
import type { Order } from '../../../api/client';
import { OrderAttachments } from '../../../components/projects/OrderAttachments';

const order = {
  id: 1,
  attachments: [
    { filename: 'a.pdf', original_name: 'Spec.pdf', size: 1024, uploaded_at: '2026-09-01T00:00:00Z' },
    { filename: 'b.pdf', original_name: 'Quote.pdf', size: 2048, uploaded_at: '2026-09-01T00:00:00Z' },
  ],
} as unknown as Order;

describe('OrderAttachments', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getProjectAttachmentUrl').mockReturnValue('/api/v1/projects/1/attachments/a.pdf');
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('only the row being downloaded goes quiet', async () => {
    let finish: (r: Response) => void = () => {};
    vi.stubGlobal(
      'fetch',
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            finish = resolve;
          }),
      ),
    );

    render(<OrderAttachments order={order} canEdit />);

    fireEvent.click(screen.getByTestId('attachment-download-a.pdf'));

    await waitFor(() => expect(screen.getByTestId('attachment-download-a.pdf')).toBeDisabled());
    expect(screen.getByTestId('attachment-download-b.pdf')).toBeEnabled();

    finish({ ok: false, status: 500 } as Response);
    await waitFor(() => expect(screen.getByTestId('attachment-download-a.pdf')).toBeEnabled());
  });

  it('only the row being deleted goes quiet', async () => {
    let finish: () => void = () => {};
    vi.spyOn(api, 'deleteProjectAttachment').mockReturnValue(
      new Promise<void>((resolve) => {
        finish = resolve;
      }) as never,
    );

    render(<OrderAttachments order={order} canEdit />);

    fireEvent.click(screen.getByTestId('attachment-delete-b.pdf'));

    await waitFor(() => expect(screen.getByTestId('attachment-delete-b.pdf')).toBeDisabled());
    expect(screen.getByTestId('attachment-delete-a.pdf')).toBeEnabled();

    finish();
    await waitFor(() => expect(screen.getByTestId('attachment-delete-b.pdf')).toBeEnabled());
  });
});
