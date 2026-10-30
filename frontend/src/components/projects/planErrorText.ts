import type { TFunction } from 'i18next';
import { ApiError } from '../../api/client';
import { QUEUE_SOURCE_REASONS } from '../../utils/queueSource';

const CONFLICTS = ['plan_conflict', 'revision_conflict', 'working_plan_conflict', 'saved_revision_conflict', 'comparison_epoch_changed', 'workingPlan.conflict'];
/** A failed refresh after an action is a read failure, not another publication. */
export class PlanReadError extends Error {}

export function planErrorText(error: unknown, t: TFunction, fallback: 'publication_failed' | 'read_failed' = 'publication_failed'): string {
  if (error instanceof PlanReadError) return error.message;
  const code = typeof error === 'string' ? error : error instanceof ApiError ? error.code : error instanceof Error ? error.message : '';
  if (code === 'order_resources_changed') return t('orderResources.errors.order_resources_changed');
  if (code === 'submission_pending') return t('workingPlan.pending');
  if (CONFLICTS.includes(code ?? '')) return t('workingPlan.conflict');
  if ((QUEUE_SOURCE_REASONS as readonly string[]).includes(code ?? '')) return t(`queueSpool.reason.${code}`);
  if (['inputs_changed', 'source_review_required', 'plan_authority_changed', 'operation_actor_changed', 'order_closed', 'publication_refused', 'publication_failed'].includes(code ?? '')) return t(`workingPlan.errors.${code}`);
  // Preserve future human refusals, without rendering an opaque JSON body or
  // an unexpected server failure's exception text in the operator dialog.
  if (error instanceof ApiError && !code && error.status < 500 && error.message.trim() && !/^[{[]/.test(error.message.trim())) return error.message;
  return t(`workingPlan.errors.${fallback}`);
}
