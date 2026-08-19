import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Copy, Check } from 'lucide-react';

interface CopyButtonProps {
  value: string;
  /** i18n key for the resting tooltip. */
  titleKey?: string;
  /** i18n key for the tooltip while the tick is showing. */
  copiedTitleKey?: string;
  className?: string;
  iconClassName?: string;
}

/**
 * Copy-to-clipboard button, with the plain-HTTP fallback.
 *
 * ⚠️ The fallback is the whole reason this is shared rather than rewritten per
 * call site: `navigator.clipboard` is gated behind the secure-context
 * requirement, so on a bare-IP LAN install reached over plain HTTP — which is
 * most BamDude installs — the API is simply undefined, and a naive
 * implementation swallows the failure with no tick and nothing copied.
 *
 * Lifted out of PrinterInfoModal when the Docker update instructions needed
 * the same control.
 */
export function CopyButton({
  value,
  titleKey = 'printers.copyToClipboard',
  copiedTitleKey = 'printers.copied',
  className = 'ml-2 p-1 rounded hover:bg-bambu-dark-tertiary text-bambu-gray hover:text-white transition-colors',
  iconClassName = 'w-3.5 h-3.5',
}: CopyButtonProps) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);

  // navigator.clipboard.writeText is gated by the secure-context requirement
  // (HTTPS or localhost). On the typical bare-IP HTTP LAN deployment shape
  // navigator.clipboard is undefined; without the legacy fallback the copy
  // silently fails and the icon never flips to the tick (#1174). Mirror the
  // off-screen-textarea + document.execCommand('copy') path that the camera
  // tokens panel already uses for the same scenario.
  const handleCopy = async () => {
    let succeeded = false;
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(value);
        succeeded = true;
      } catch {
        // Fall through to legacy path below.
      }
    }
    if (!succeeded) {
      const textarea = document.createElement('textarea');
      textarea.value = value;
      textarea.setAttribute('readonly', '');
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      textarea.style.pointerEvents = 'none';
      document.body.appendChild(textarea);
      try {
        textarea.select();
        succeeded = document.execCommand('copy');
      } catch {
        succeeded = false;
      } finally {
        document.body.removeChild(textarea);
      }
    }
    if (!succeeded) return;
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <button
      onClick={handleCopy}
      className={className}
      title={copied ? t(copiedTitleKey) : t(titleKey)}
    >
      {copied ? <Check className={`${iconClassName} text-bambu-green`} /> : <Copy className={iconClassName} />}
    </button>
  );
}
