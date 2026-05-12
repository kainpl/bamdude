import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '../api/client';
import type { FilamentCalibrationOut, PACalibHistoryEntryOut } from '../api/client';

export function useCalibrationHistory(
  printerId: number,
  printerModel: string,
  enabled: boolean,
) {
  const qc = useQueryClient();

  const bamdudeQuery = useQuery<FilamentCalibrationOut[]>({
    queryKey: ['filament-calibrations', printerModel],
    queryFn: () => api.listFilamentCalibrations({ printer_model: printerModel }),
    enabled: enabled && Boolean(printerModel),
    staleTime: 10_000,
  });

  const printerSideQuery = useQuery<PACalibHistoryEntryOut[]>({
    queryKey: ['calibration', 'printer-history', printerId],
    queryFn: () => api.getPrinterCalibrationHistory(printerId),
    enabled,
    staleTime: 30_000,
  });

  const setActiveMutation = useMutation({
    mutationFn: (caliId: number) => api.setActiveCalibration(caliId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['filament-calibrations', printerModel] }),
  });

  const deleteMutation = useMutation({
    mutationFn: (caliId: number) => api.deleteCalibration(caliId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['filament-calibrations', printerModel] }),
  });

  const refreshMutation = useMutation({
    mutationFn: (nozzleDia: number) => api.refreshPrinterCalibrationHistory(printerId, nozzleDia),
    onSuccess: () => {
      setTimeout(() => {
        qc.invalidateQueries({ queryKey: ['calibration', 'printer-history', printerId] });
      }, 2000);
    },
  });

  return {
    bamdude: bamdudeQuery.data ?? [],
    printerSide: printerSideQuery.data ?? [],
    isLoading: bamdudeQuery.isLoading || printerSideQuery.isLoading,
    setActive: setActiveMutation.mutateAsync,
    delete: deleteMutation.mutateAsync,
    refreshFromPrinter: refreshMutation.mutateAsync,
    isRefreshing: refreshMutation.isPending,
  };
}
