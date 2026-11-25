import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api, type SlicerPipelineCreateRequest } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';

export function useSavePipeline() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  return useMutation({
    mutationFn: (body: SlicerPipelineCreateRequest) => api.createSlicerPipeline(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['slicer-pipelines'] });
      showToast(t('slice.pipelines.toast.saved', 'Pipeline saved'), 'success');
    },
    onError: (error: Error) => {
      showToast(error.message || t('slice.pipelines.toast.saveFailed', 'Save failed'), 'error');
    },
  });
}
