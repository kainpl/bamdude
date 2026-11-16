/**
 * A library file's details: its facts, read-only, and the one thing here the
 * operator edits — a link of their own (a Printables page, a datasheet).
 *
 * The link is NOT the file's origin: `source_url` is written by the MakerWorld
 * import and the slicer and is shown as it is. The notes thread stays on the
 * card; this dialog only says how many there are.
 *
 * The server decides what a valid link is and who may write it; its refusal is
 * shown as it comes. An address is rendered as an `href` only when it is
 * http(s) — anything else stays text.
 */
import { useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ExternalLink, Info } from 'lucide-react';
import { api, type LibraryFile, type LibraryFileListItem } from '../api/client';
import { Modal } from './Modal';
import { Button } from './Button';
import { formatDateTime, type DateFormat, type TimeFormat } from '../utils/date';
import { formatFileSize } from '../utils/file';
import { isHttpLink } from '../utils/httpLink';

/** As the server caps it. */
const LINK_MAX_LENGTH = 2000;
const SHORT_HASH = 12;

interface Props {
  /** The file as the list has it — shown at once, while the full record loads. */
  file: LibraryFileListItem;
  /** May the viewer edit this file (its own, or any with the right to)? */
  canEdit: boolean;
  onClose: () => void;
  timeFormat?: TimeFormat;
  dateFormat?: DateFormat;
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-sm text-bambu-gray">{label}</dt>
      <dd className="min-w-0 break-words text-sm text-white">{children}</dd>
    </>
  );
}

/** An address as a link when it may be one, as text otherwise. */
function Address({ value }: { value: string }) {
  if (!isHttpLink(value)) return <span className="break-all">{value}</span>;
  return (
    <a href={value} target="_blank" rel="noopener noreferrer" className="break-all text-bambu-green hover:underline">
      {value}
    </a>
  );
}

export function LibraryFileDetailsModal({ file, canEdit, onClose, timeFormat, dateFormat }: Props) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: ['library-file', file.id],
    queryFn: () => api.getLibraryFile(file.id),
  });
  const savedLink = data?.external_url ?? null;
  const [draft, setDraft] = useState('');
  const [touched, setTouched] = useState(false);
  // The box follows the saved link until the operator types in it.
  useEffect(() => {
    if (!touched) setDraft(savedLink ?? '');
  }, [savedLink, touched]);

  const save = useMutation({
    mutationFn: (value: string | null) => api.setLibraryFileExternalUrl(file.id, value),
    onSuccess: (updated: LibraryFile) => {
      queryClient.setQueryData(['library-file', file.id], updated);
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
      setTouched(false);
      setDraft(updated.external_url ?? '');
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate(draft.trim() || null);
  };
  const unchanged = draft.trim() === (savedLink ?? '');
  const hash = data?.file_hash ?? null;
  const dash = '—';

  return (
    <Modal
      onClose={onClose}
      title={t('fileDetails.title')}
      icon={<Info className="h-5 w-5 text-bambu-green" />}
      size="lg"
      footer={
        <div className="flex justify-end">
          <Button variant="secondary" onClick={onClose}>
            {t('common.close')}
          </Button>
        </div>
      }
    >
      <div className="space-y-5 p-4">
        <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-2">
          <Fact label={t('fileDetails.name')}>{data?.filename ?? file.filename}</Fact>
          <Fact label={t('fileDetails.type')}>{file.file_type.toUpperCase()}</Fact>
          <Fact label={t('fileDetails.size')}>{formatFileSize(data?.file_size ?? file.file_size)}</Fact>
          <Fact label={t('fileDetails.folder')}>
            {data ? (data.folder_id == null ? t('fileDetails.rootFolder') : (data.folder_name ?? dash)) : dash}
          </Fact>
          {(data?.created_by_username ?? file.created_by_username) && (
            <Fact label={t('fileDetails.addedBy')}>{data?.created_by_username ?? file.created_by_username}</Fact>
          )}
          <Fact label={t('fileDetails.added')}>{formatDateTime(file.created_at, timeFormat, dateFormat) || dash}</Fact>
          <Fact label={t('fileDetails.updated')}>
            {data ? formatDateTime(data.updated_at, timeFormat, dateFormat) || dash : dash}
          </Fact>
          <Fact label={t('fileDetails.hash')}>
            {hash ? (
              <span title={hash} className="font-mono">
                {hash.slice(0, SHORT_HASH)}
              </span>
            ) : (
              dash
            )}
          </Fact>
          {data?.source_url && (
            <Fact label={t('fileDetails.source')}>
              <Address value={data.source_url} />
            </Fact>
          )}
          <Fact label={t('fileDetails.notes')}>{file.notes_count}</Fact>
          {!canEdit && (
            <Fact label={t('fileDetails.link')}>{savedLink ? <Address value={savedLink} /> : dash}</Fact>
          )}
        </dl>

        {canEdit && (
          <form onSubmit={submit} className="space-y-2 border-t border-bambu-dark-tertiary pt-4">
            <label htmlFor="file-details-link" className="block text-sm font-medium text-white">
              {t('fileDetails.link')}
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <input
                id="file-details-link"
                type="text"
                inputMode="url"
                autoComplete="off"
                spellCheck={false}
                maxLength={LINK_MAX_LENGTH}
                value={draft}
                onChange={(e) => {
                  setTouched(true);
                  setDraft(e.target.value);
                }}
                placeholder="https://"
                className="min-w-0 flex-1 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-3 py-2 text-sm text-white placeholder-bambu-gray focus:border-bambu-green focus:outline-none"
              />
              <Button type="submit" disabled={save.isPending || unchanged || !data}>
                {t('common.save')}
              </Button>
            </div>
            <p className="text-xs text-bambu-gray">{t('fileDetails.linkHint')}</p>
            {save.isError && (
              <p role="alert" className="text-sm text-red-700 dark:text-red-400">
                {save.error instanceof Error ? save.error.message : t('fileDetails.saveFailed')}
              </p>
            )}
            {savedLink && (
              <div className="flex flex-wrap items-center gap-3">
                {isHttpLink(savedLink) && (
                  <a
                    href={savedLink}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 text-sm text-bambu-green hover:underline"
                  >
                    <ExternalLink className="h-4 w-4" />
                    {t('fileDetails.openLink')}
                  </a>
                )}
                <button
                  type="button"
                  onClick={() => save.mutate(null)}
                  disabled={save.isPending}
                  className="text-sm text-bambu-gray hover:text-white disabled:opacity-50"
                >
                  {t('fileDetails.removeLink')}
                </button>
              </div>
            )}
          </form>
        )}
      </div>
    </Modal>
  );
}
