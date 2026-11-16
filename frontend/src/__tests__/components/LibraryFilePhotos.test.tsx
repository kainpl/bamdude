/**
 * The photo gallery of a library file: thumbnails under the media token, a
 * viewer, captions, and — for someone who may edit the file — adding (several
 * at once, by the button or by dropping them) and deleting.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { LibraryFilePhotos } from '../../components/LibraryFilePhotos';
import { api, type LibraryFilePhoto } from '../../api/client';

interface Photo {
  id: number;
  library_file_id: number;
  original_name: string | null;
  content_type: string;
  file_size: number;
  width: number;
  height: number;
  caption: string | null;
  uploaded_by_name: string | null;
  created_at: string;
}

const photo = (id: number, extra: Partial<Photo> = {}): Photo => ({
  id,
  library_file_id: 7,
  original_name: `IMG_${id}.jpg`,
  content_type: 'image/jpeg',
  file_size: 2048,
  width: 800,
  height: 600,
  caption: null,
  uploaded_by_name: 'iryna',
  created_at: '2024-01-02T10:00:00Z',
  ...extra,
});

let stored: Photo[] = [];
let posted: string[] = [];
let patched: unknown[] = [];
let deleted: number[] = [];

function serve(refuse: Record<string, { status: number; detail: string }> = {}) {
  // The multipart call is replaced at the client's boundary: jsdom's fetch does
  // not hand a mock server a readable multipart body.
  vi.spyOn(api, 'uploadLibraryFilePhoto').mockImplementation(async (fileId: number, file: File) => {
    expect(fileId).toBe(7);
    posted.push(file.name);
    const refusal = refuse[file.name];
    if (refusal) throw new Error(refusal.detail);
    const added = photo(100 + posted.length, { original_name: file.name });
    stored = [...stored, added];
    return added as LibraryFilePhoto;
  });
  server.use(
    http.get('/api/v1/library/files/7/photos', () => HttpResponse.json(stored)),
    http.patch('/api/v1/library/files/7/photos/:id', async ({ request, params }) => {
      const body = (await request.json()) as { caption: string | null };
      patched.push({ id: Number(params.id), ...body });
      stored = stored.map((p) => (p.id === Number(params.id) ? { ...p, caption: body.caption } : p));
      return HttpResponse.json(stored.find((p) => p.id === Number(params.id)));
    }),
    http.delete('/api/v1/library/files/7/photos/:id', ({ params }) => {
      deleted.push(Number(params.id));
      stored = stored.filter((p) => p.id !== Number(params.id));
      return HttpResponse.json({ success: true });
    }),
  );
}

const file = (name: string, type = 'image/jpeg') => new File(['bytes'], name, { type });

describe('LibraryFilePhotos', () => {
  beforeEach(() => {
    stored = [];
    posted = [];
    patched = [];
    deleted = [];
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('shows each photo as its small copy, under the media-token URL of its file', async () => {
    stored = [photo(1, { caption: 'First print' }), photo(2)];
    serve();
    render(<LibraryFilePhotos fileId={7} canEdit={false} />);

    const first = await screen.findByRole('img', { name: 'First print' });
    expect(first.getAttribute('src')).toMatch(/^\/api\/v1\/library\/files\/7\/photos\/1\/thumbnail(\?token=.*)?$/);
    // No caption: the name the file came with stands in.
    const second = screen.getByRole('img', { name: 'IMG_2.jpg' });
    expect(second.getAttribute('src')).toMatch(/^\/api\/v1\/library\/files\/7\/photos\/2\/thumbnail(\?token=.*)?$/);
    expect(screen.getByText('First print')).toBeInTheDocument();
  });

  it('says so when there are none, and offers nothing to a reader', async () => {
    serve();
    render(<LibraryFilePhotos fileId={7} canEdit={false} />);

    expect(await screen.findByText('No photos yet.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add photos' })).not.toBeInTheDocument();
  });

  it('gives a reader no way to change a photo', async () => {
    stored = [photo(1, { caption: 'First print' })];
    serve();
    render(<LibraryFilePhotos fileId={7} canEdit={false} />);

    await screen.findByRole('img', { name: 'First print' });
    expect(screen.queryByRole('button', { name: 'Delete photo' })).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Caption' })).not.toBeInTheDocument();
  });

  it('opens a photo full size in the image viewer', async () => {
    stored = [photo(1, { caption: 'First print' })];
    serve();
    render(<LibraryFilePhotos fileId={7} canEdit={false} />);

    await userEvent.click(await screen.findByRole('button', { name: 'View photo: First print' }));

    const viewer = await screen.findByRole('dialog', { name: 'First print' });
    const full = within(viewer).getByRole('img', { name: 'First print' });
    expect(full.getAttribute('src')).toMatch(/^\/api\/v1\/library\/files\/7\/photos\/1\/image(\?token=.*)?$/);
    await userEvent.click(within(viewer).getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog', { name: 'First print' })).not.toBeInTheDocument();
  });

  it('uploads every chosen file, one request each, and shows them', async () => {
    serve();
    render(<LibraryFilePhotos fileId={7} canEdit />);
    await screen.findByText('No photos yet.');

    await userEvent.upload(screen.getByLabelText('Add photos'), [file('a.jpg'), file('b.png', 'image/png')]);

    await waitFor(() => expect(posted).toEqual(['a.jpg', 'b.png']));
    expect(await screen.findByRole('img', { name: 'a.jpg' })).toBeInTheDocument();
    expect(await screen.findByRole('img', { name: 'b.png' })).toBeInTheDocument();
  });

  it("names a refused file with the server's reason and still adds the others", async () => {
    serve({ 'notes.jpg': { status: 422, detail: 'The photo must be a PNG, JPEG or WebP picture' } });
    render(<LibraryFilePhotos fileId={7} canEdit />);
    await screen.findByText('No photos yet.');

    await userEvent.upload(screen.getByLabelText('Add photos'), [file('notes.jpg'), file('good.jpg')]);

    await waitFor(() => expect(posted).toEqual(['notes.jpg', 'good.jpg']));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('notes.jpg');
    expect(alert).toHaveTextContent('The photo must be a PNG, JPEG or WebP picture');
    expect(await screen.findByRole('img', { name: 'good.jpg' })).toBeInTheDocument();
  });

  it('takes files dropped onto it', async () => {
    serve();
    render(<LibraryFilePhotos fileId={7} canEdit />);
    await screen.findByText('No photos yet.');

    const zone = screen.getByTestId('library-file-photos');
    const dataTransfer = { files: [file('dropped.jpg')], types: ['Files'] };
    fireEvent.dragOver(zone, { dataTransfer });
    fireEvent.drop(zone, { dataTransfer });

    await waitFor(() => expect(posted).toEqual(['dropped.jpg']));
  });

  it('ignores a drop for a reader', async () => {
    serve();
    render(<LibraryFilePhotos fileId={7} canEdit={false} />);
    await screen.findByText('No photos yet.');

    fireEvent.drop(screen.getByTestId('library-file-photos'), {
      dataTransfer: { files: [file('dropped.jpg')], types: ['Files'] },
    });

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(posted).toEqual([]);
  });

  it('saves a caption', async () => {
    stored = [photo(1)];
    serve();
    render(<LibraryFilePhotos fileId={7} canEdit />);

    const box = await screen.findByRole('textbox', { name: 'Caption' });
    await userEvent.type(box, 'After sanding{Enter}');

    await waitFor(() => expect(patched).toEqual([{ id: 1, caption: 'After sanding' }]));
  });

  it('deletes a photo only after it is confirmed', async () => {
    stored = [photo(1, { caption: 'First print' }), photo(2)];
    serve();
    render(<LibraryFilePhotos fileId={7} canEdit />);

    await screen.findByRole('img', { name: 'First print' });
    await userEvent.click(screen.getAllByRole('button', { name: 'Delete photo' })[0]);
    expect(deleted).toEqual([]);
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(deleted).toEqual([]);

    await userEvent.click(screen.getAllByRole('button', { name: 'Delete photo' })[0]);
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(deleted).toEqual([1]));
    await waitFor(() => expect(screen.queryByRole('img', { name: 'First print' })).not.toBeInTheDocument());
    expect(screen.getByRole('img', { name: 'IMG_2.jpg' })).toBeInTheDocument();
  });
});
