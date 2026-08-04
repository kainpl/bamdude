import { useEffect, useState } from 'react';

// The beforeinstallprompt event is not in the standard TS DOM lib.
export interface BeforeInstallPromptEvent extends Event {
  readonly platforms: string[];
  prompt: () => Promise<void>;
  readonly userChoice: Promise<{ outcome: 'accepted' | 'dismissed'; platform: string }>;
}

/**
 * Whether the browser is currently offering to install BamDude as a PWA.
 *
 * Its own module because the sidebar has to know whether the install button
 * will draw ANYTHING before it lays out its icon rows — and in most sessions it
 * will not (already installed, unsupported browser, iOS Safari, which has no
 * programmatic install at all). Counting a button that renders null would leave
 * a hole in a row.
 *
 * Two consumers means two listeners on one event; `beforeinstallprompt` fires
 * for both, so both end up holding the same prompt. Only the button uses it.
 */
export function useInstallPrompt(): BeforeInstallPromptEvent | null {
  const [promptEvent, setPromptEvent] = useState<BeforeInstallPromptEvent | null>(null);

  useEffect(() => {
    const onBeforeInstallPrompt = (e: Event) => {
      // Suppress Chrome's own mini-infobar (desktop) so the button is the
      // single, predictable install entry point.
      e.preventDefault();
      setPromptEvent(e as BeforeInstallPromptEvent);
    };
    const onInstalled = () => setPromptEvent(null);
    window.addEventListener('beforeinstallprompt', onBeforeInstallPrompt);
    window.addEventListener('appinstalled', onInstalled);
    return () => {
      window.removeEventListener('beforeinstallprompt', onBeforeInstallPrompt);
      window.removeEventListener('appinstalled', onInstalled);
    };
  }, []);

  return promptEvent;
}
