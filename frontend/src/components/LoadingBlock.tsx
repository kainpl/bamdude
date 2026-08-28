/**
 * "This is working, it is just not finished."
 *
 * A bare line of grey text saying "Loading" is indistinguishable from a page
 * that has given up — nothing on it moves, so the only way to tell is to wait
 * and see whether it changes. On a farm with a long archive that wait is
 * measured in seconds, and the reports it produces are about a broken page.
 *
 * ⚠️ This is for a WAIT, never for an empty result. "No prints yet" and
 * "loading prints" look alike and mean opposite things: one is answered by
 * waiting, the other by doing something. The empty states in this codebase draw
 * their own icon and sentence deliberately, and must not be replaced with this.
 */

import { Loader2 } from 'lucide-react';

interface LoadingBlockProps {
  /** What is being waited for, in the user's language. Already translated. */
  label: string;
  /**
   * Padding and colour, when the surrounding context is not the usual page
   * body — the stream overlay is on black, a panel inside a card wants less
   * vertical room than a whole page.
   */
  className?: string;
}

export function LoadingBlock({ label, className = 'py-16 text-bambu-gray' }: LoadingBlockProps) {
  return (
    <div className={`flex items-center justify-center gap-3 ${className}`} role="status" aria-live="polite">
      <Loader2 className="w-6 h-6 text-bambu-green animate-spin" aria-hidden="true" />
      <span className="text-sm">{label}</span>
    </div>
  );
}
