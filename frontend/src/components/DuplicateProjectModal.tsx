import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { X, Copy, Loader2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { api } from '../api/client';
import { Card, CardContent } from './Card';
import { Button } from './Button';
import { useToast } from '../contexts/ToastContext';

interface DuplicateProjectModalProps {
  projectId: number;
  projectName: string;
  onClose: () => void;
  /** Called with the new project's id — lets the caller navigate to it. */
  onDuplicated?: (newId: number) => void;
}

/**
 * Copy a project's setup into a new one.
 *
 * The dialog spells out the split rather than leaving it to be discovered:
 * settings, part list, linked files and print plan come across; print history
 * and the queue stay with the original. That is the whole question a user has
 * before pressing the button, and the answer is not guessable from the word
 * "duplicate".
 */
export function DuplicateProjectModal({ projectId, projectName, onClose, onDuplicated }: DuplicateProjectModalProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const [name, setName] = useState('');
  const [includeChildren, setIncludeChildren] = useState(false);

  // The list payload carries no child information, so the sub-project choice
  // is only offered once we know there is one to make. Asking about children
  // a project does not have is worse than one extra request.
  const { data: full, isLoading } = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => api.getProject(projectId),
  });
  const childCount = full?.children?.length ?? 0;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const duplicate = useMutation({
    mutationFn: () =>
      api.duplicateProject(projectId, {
        name: name.trim() || undefined,
        include_children: includeChildren,
      }),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      queryClient.invalidateQueries({ queryKey: ['project'] });
      showToast(t('projects.toast.duplicated'));
      onDuplicated?.(created.id);
      onClose();
    },
    onError: () => showToast(t('projects.duplicate.failed'), 'error'),
  });

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <Card className="w-full max-w-md">
        <CardContent className="p-0">
          <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
            <div className="flex items-center gap-2">
              <Copy className="w-5 h-5 text-bambu-green" />
              <h2 className="text-xl font-semibold text-white">{t('projects.duplicate.title')}</h2>
            </div>
            <button
              onClick={onClose}
              className="text-bambu-gray hover:text-white transition-colors"
              disabled={duplicate.isPending}
            >
              <X className="w-5 h-5" />
            </button>
          </div>

          <div className="p-4 space-y-4">
            <div>
              <label className="block text-sm text-bambu-gray mb-1" htmlFor="duplicate-project-name">
                {t('projects.duplicate.nameLabel')}
              </label>
              <input
                id="duplicate-project-name"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={`${projectName} (Copy)`}
                disabled={duplicate.isPending}
                className="w-full px-3 py-2 rounded-lg bg-bambu-dark border border-bambu-dark-tertiary text-white placeholder:text-bambu-gray focus:outline-none focus:border-bambu-green"
              />
            </div>

            <div className="text-sm space-y-1">
              <p className="text-bambu-gray">{t('projects.duplicate.copies')}</p>
              <p className="text-bambu-gray">{t('projects.duplicate.excludes')}</p>
            </div>

            {isLoading ? (
              <div className="flex items-center gap-2 text-sm text-bambu-gray">
                <Loader2 className="w-4 h-4 animate-spin" />
              </div>
            ) : (
              childCount > 0 && (
                <label className="flex items-center gap-2 text-sm text-white cursor-pointer">
                  <input
                    type="checkbox"
                    checked={includeChildren}
                    onChange={(e) => setIncludeChildren(e.target.checked)}
                    disabled={duplicate.isPending}
                    className="accent-bambu-green"
                  />
                  {t('projects.duplicate.includeChildren', { count: childCount })}
                </label>
              )
            )}
          </div>

          <div className="flex justify-end gap-2 p-4 border-t border-bambu-dark-tertiary">
            <Button variant="secondary" onClick={onClose} disabled={duplicate.isPending}>
              {t('common.cancel')}
            </Button>
            <Button onClick={() => duplicate.mutate()} disabled={duplicate.isPending}>
              {duplicate.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : t('projects.duplicate.submit')}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
