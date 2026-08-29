import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { PackageCheck, Plus } from 'lucide-react';
import { api, type ProjectPartRow } from '../api/client';
import { Card, CardContent } from './Card';
import { useToast } from '../contexts/ToastContext';

interface ProjectPartsTableProps {
  projectId: number;
  canEdit: boolean;
}

/**
 * Parts ledger for a project's detail page — one row per distinct part
 * (`name_key`) discovered across the project's archives, with an optional
 * target quantity the operator can set per part.
 *
 * Self-contained: fetches its own data (`['project-parts', projectId]`) and
 * hides itself entirely when there is nothing to show, so the page mounts it
 * unconditionally next to the print plan.
 */
export function ProjectPartsTable({ projectId, canEdit }: ProjectPartsTableProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  // Rows whose target started as `null` (no target set) but the operator
  // clicked the "set target" affordance — swaps the dash for an input
  // pre-filled with 0. Once a save lands, target_qty is no longer null and
  // the row shows the input on its own, independent of this set.
  const [editingUnset, setEditingUnset] = useState<Set<string>>(new Set());

  const { data } = useQuery({
    queryKey: ['project-parts', projectId],
    queryFn: () => api.getProjectParts(projectId),
    enabled: projectId > 0,
  });

  const updateTargetMutation = useMutation({
    mutationFn: (vars: { name_key: string; target_qty: number }) =>
      api.updateProjectParts(projectId, [{ name_key: vars.name_key, target_qty: vars.target_qty }]),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['project-parts', projectId] });
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const parts = data?.parts ?? [];
  if (parts.length === 0) return null;

  const startEditingUnset = (nameKey: string) => {
    setEditingUnset((prev) => {
      const next = new Set(prev);
      next.add(nameKey);
      return next;
    });
  };

  const commitTarget = (part: ProjectPartRow, rawValue: string) => {
    const parsed = parseInt(rawValue, 10);
    const nextQty = Number.isFinite(parsed) ? Math.max(0, parsed) : null;
    if (nextQty === null || nextQty === part.target_qty) return;
    updateTargetMutation.mutate({ name_key: part.name_key, target_qty: nextQty });
  };

  return (
    <Card>
      <CardContent className="p-4">
        <h2 className="text-lg font-semibold text-white flex items-center gap-2 mb-3">
          <PackageCheck className="w-5 h-5" />
          {t('projects.partsLedger.title')}
        </h2>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-bambu-dark-tertiary text-bambu-gray text-left">
                <th className="px-3 py-2 font-medium">{t('projects.partsLedger.name')}</th>
                <th className="px-3 py-2 font-medium text-center">{t('projects.partsLedger.target')}</th>
                <th className="px-3 py-2 font-medium text-center">{t('projects.partsLedger.printed')}</th>
                <th className="px-3 py-2 font-medium text-center">{t('projects.partsLedger.inProgress')}</th>
                <th className="px-3 py-2 font-medium text-center">{t('projects.partsLedger.defective')}</th>
                <th className="px-3 py-2 font-medium text-center">{t('projects.partsLedger.usable')}</th>
                <th className="px-3 py-2 font-medium text-center">{t('projects.partsLedger.remaining')}</th>
              </tr>
            </thead>
            <tbody>
              {parts.map((part) => {
                const showUnsetAffordance = part.target_qty === null && !editingUnset.has(part.name_key);
                return (
                  <tr key={part.name_key} className="border-b border-bambu-dark-tertiary/50 hover:bg-bambu-dark-tertiary/30">
                    <td className="px-3 py-2 text-white truncate max-w-[240px]" title={part.name}>
                      {part.name}
                    </td>
                    <td className="px-3 py-2 text-center tabular-nums">
                      {canEdit ? (
                        showUnsetAffordance ? (
                          <button
                            type="button"
                            data-testid={`part-target-${part.name_key}`}
                            onClick={() => startEditingUnset(part.name_key)}
                            title={t('projects.partsLedger.setTarget')}
                            className="inline-flex items-center gap-1 text-bambu-gray hover:text-white transition-colors"
                          >
                            <span>{'—'}</span>
                            <Plus className="w-3 h-3" />
                          </button>
                        ) : (
                          <input
                            key={`${part.name_key}:${part.target_qty ?? 'unset'}`}
                            type="number"
                            min={0}
                            max={999999}
                            data-testid={`part-target-input-${part.name_key}`}
                            defaultValue={part.target_qty ?? 0}
                            disabled={updateTargetMutation.isPending}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter') e.currentTarget.blur();
                            }}
                            onBlur={(e) => commitTarget(part, e.currentTarget.value)}
                            className="w-16 text-center bg-transparent border border-transparent hover:border-bambu-dark-tertiary focus:border-bambu-green focus:outline-none rounded disabled:opacity-50 [appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none"
                          />
                        )
                      ) : part.target_qty === null ? (
                        <span data-testid={`part-target-${part.name_key}`} className="text-bambu-gray">
                          {'—'}
                        </span>
                      ) : (
                        <span className="text-white">{part.target_qty}</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-center text-white tabular-nums">{part.printed}</td>
                    <td className="px-3 py-2 text-center text-bambu-gray tabular-nums">{part.in_progress}</td>
                    <td className="px-3 py-2 text-center text-amber-400 tabular-nums">{part.defective}</td>
                    <td className="px-3 py-2 text-center text-bambu-green tabular-nums">{part.usable}</td>
                    <td className="px-3 py-2 text-center text-white tabular-nums" data-testid={`part-remaining-${part.name_key}`}>
                      {part.remaining === null ? '—' : part.remaining}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}
