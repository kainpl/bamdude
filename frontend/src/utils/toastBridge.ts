export type BridgedToastType = 'success' | 'error' | 'warning' | 'info';

type ToastFn = (message: string, type?: BridgedToastType) => void;

let handler: ToastFn | null = null;

/**
 * How code OUTSIDE the React tree raises a toast.
 *
 * There is exactly one such caller today and it is the reason this file
 * exists: the app's `QueryClient` is built at module scope, so its
 * `QueryCache.onError` cannot reach into `ToastContext` the way a component
 * does. Everything that can call `useToast()` still must — this is not a
 * second way to raise a toast from a component, it is the only way to raise
 * one from somewhere a hook cannot run.
 *
 * ⚠️ **A message with no provider mounted is dropped, on purpose.** The
 * alternative is a queue that replays somebody's stale failure over the login
 * screen after a reload.
 */
export function setToastHandler(fn: ToastFn | null): void {
  handler = fn;
}

export function notifyOutsideReact(message: string, type: BridgedToastType = 'info'): void {
  handler?.(message, type);
}
