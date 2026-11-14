/**
 * What the camera test says when it succeeded. The resolution is optional —
 * a probe may connect without learning it — so the sentence with it and the
 * sentence without it are two strings, never one with a gap to glue into.
 */
export function cameraTestMessage(
  t: (key: string, options?: Record<string, unknown>) => string,
  resolution: string | null | undefined,
): string {
  const value = resolution?.trim();
  return value
    ? t('settings.toast.cameraConnectedAt', { resolution: value })
    : t('settings.toast.cameraConnected');
}
