import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { supportApi } from '../../api/client';
import { LogArchivesPanel } from '../../components/LogArchivesPanel';

const current = { filename: 'bamdude.log', size_bytes: 2048, mtime: '2026-09-13T10:00:00Z' };
const archive = { ...current, filename: 'bamdude-2026-09-12.log' };

describe('LogArchivesPanel', () => {
  beforeEach(() => {
    vi.spyOn(supportApi, 'listLogArchives').mockResolvedValue({ current, archives: [] });
    vi.spyOn(supportApi, 'downloadLogArchive').mockResolvedValue();
    vi.spyOn(supportApi, 'deleteLogArchive').mockResolvedValue({ message: 'Deleted' });
  });

  it('downloads the current log even when no archives exist, without a delete action', async () => {
    const user = userEvent.setup();
    render(<LogArchivesPanel />);
    expect(await screen.findByText('bamdude.log')).toBeInTheDocument();
    expect(screen.getByText('Current')).toBeInTheDocument();
    expect(screen.queryByTitle('Delete')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Download' }));
    expect(supportApi.downloadLogArchive).toHaveBeenCalledWith('bamdude.log');
    expect(supportApi.deleteLogArchive).not.toHaveBeenCalled();
  });

  it('puts the current log first and preserves archive download and confirmed deletion', async () => {
    vi.mocked(supportApi.listLogArchives).mockResolvedValue({ current, archives: [archive] });
    const user = userEvent.setup();
    render(<LogArchivesPanel />);
    await screen.findByText(archive.filename);
    const rows = screen.getAllByRole('row');
    expect(rows[1]).toHaveTextContent('bamdude.log');
    const archiveRow = within(rows[2]);
    await user.click(archiveRow.getByRole('button', { name: 'Download' }));
    expect(supportApi.downloadLogArchive).toHaveBeenCalledWith(archive.filename);
    await user.click(archiveRow.getByTitle('Delete'));
    expect(supportApi.deleteLogArchive).not.toHaveBeenCalled();
    await user.click(archiveRow.getByRole('button', { name: 'Confirm' }));
    expect(supportApi.deleteLogArchive).toHaveBeenCalledWith(archive.filename);
  });

  it('shows download failures and allows retrying', async () => {
    vi.mocked(supportApi.downloadLogArchive).mockRejectedValueOnce(new Error('Current log not found'));
    const user = userEvent.setup();
    render(<LogArchivesPanel />);
    await screen.findByText('bamdude.log');
    await user.click(screen.getByRole('button', { name: 'Download' }));
    expect(await screen.findByText('Current log not found')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled());
  });

  it('handles installations with no current file or archives', async () => {
    vi.mocked(supportApi.listLogArchives).mockResolvedValue({ current: null, archives: [] });
    render(<LogArchivesPanel />);
    expect(await screen.findByText('No log files available.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Download' })).not.toBeInTheDocument();
  });

  it('still lists archives from servers without current-log metadata', async () => {
    vi.mocked(supportApi.listLogArchives).mockResolvedValue({ archives: [archive] });
    render(<LogArchivesPanel />);
    expect(await screen.findByText(archive.filename)).toBeInTheDocument();
    expect(screen.queryByText('Current')).not.toBeInTheDocument();
  });
});
