import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { X, Copy, Check, Signal, Cable } from 'lucide-react';
import { Card, CardContent } from './Card';
import { useQuery } from '@tanstack/react-query';
import { formatDateTime, type TimeFormat, type DateFormat } from '../utils/date';
import { api, macrosApi } from '../api/client';
import { getPrinterImage, getWifiStrength } from '../utils/printer';
import type { Printer, PrinterStatus } from '../api/client';

interface PrinterInfoModalProps {
  printer: Printer;
  status?: PrinterStatus;
  totalPrintHours?: number;
  onClose: () => void;
}

function CopyButton({ value }: { value: string }) {
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
      className="ml-2 p-1 rounded hover:bg-bambu-dark-tertiary text-bambu-gray hover:text-white transition-colors"
      title={copied ? t('printers.copied') : t('printers.copyToClipboard')}
    >
      {copied ? <Check className="w-3.5 h-3.5 text-bambu-green" /> : <Copy className="w-3.5 h-3.5" />}
    </button>
  );
}

export function PrinterInfoModal({ printer, status, totalPrintHours, onClose }: PrinterInfoModalProps) {
  const { t } = useTranslation();
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  // Swap profile catalog — only used to resolve the human label of the
  // active ``swap_profile`` id for printers that support a plate swapper
  // (currently A1 / A1 mini in BamDude). Cached for 5 min; small payload.
  // Match the cache key + queryFn that ``PrintersPage.tsx`` uses so this
  // modal doesn't fire its own duplicate request — and so the catalog
  // resolves immediately when the user opens info from the printer card
  // (the parent has already hydrated this cache). Earlier I used
  // ``api.getSwapProfiles`` which isn't a real key on the main ``api``
  // object — that lookup returned undefined, useQuery's queryFn then
  // threw, ``swapProfiles`` stayed undefined, and the row fell back to
  // showing the raw id (``a1mini_kit``) instead of the human label
  // (``Kit Edition``).
  const { data: swapProfiles } = useQuery({
    queryKey: ['macros', 'swap-profiles'],
    queryFn: macrosApi.getSwapProfiles,
    staleTime: 5 * 60 * 1000,
  });
  const timeFormat: TimeFormat = (settings as Record<string, string> | undefined)?.time_format as TimeFormat || 'system';
  const dateFormat: DateFormat = (settings as Record<string, string> | undefined)?.date_format as DateFormat || 'system';

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [onClose]);

  const rows: { label: string; value: React.ReactNode }[] = [];

  // Model
  rows.push({
    label: t('printers.model'),
    value: printer.model ?? '-',
  });

  // Connection Status
  rows.push({
    label: t('common.status'),
    value: (
      <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium ${
        status?.connected
          ? 'bg-bambu-green/20 text-bambu-green'
          : 'bg-red-500/20 text-red-400'
      }`}>
        <span className={`w-1.5 h-1.5 rounded-full ${status?.connected ? 'bg-bambu-green' : 'bg-red-400'}`} />
        {status?.connected ? t('printers.status.available') : t('printers.status.offline')}
      </span>
    ),
  });

  // State
  if (status?.state) {
    const stateMap: Record<string, string> = {
      IDLE: 'printers.status.idle',
      RUNNING: 'printers.status.printing',
      PAUSE: 'printers.status.paused',
      FINISH: 'printers.status.finished',
      FAILED: 'printers.status.error',
    };
    rows.push({
      label: t('printers.state'),
      value: t(stateMap[status.state] ?? 'printers.status.unknown'),
    });
  }

  // IP Address
  rows.push({
    label: t('printers.ipAddress'),
    value: (
      <span className="flex items-center">
        <span className="font-mono">{printer.ip_address}</span>
        <CopyButton value={printer.ip_address} />
      </span>
    ),
  });

  // Serial Number
  rows.push({
    label: t('printers.serialNumber'),
    value: (
      <span className="flex items-center">
        <span className="font-mono truncate">{printer.serial_number}</span>
        <CopyButton value={printer.serial_number} />
      </span>
    ),
  });

  // Network connection
  if (status?.wired_network) {
    rows.push({
      label: t('printers.networkLabel', 'Network'),
      value: (
        <span className="flex items-center gap-2">
          <Cable className="w-4 h-4 text-bambu-green" />
          <span className="text-bambu-green">{t('printers.connection.ethernet', 'Ethernet')}</span>
        </span>
      ),
    });
  } else if (status?.wifi_signal != null) {
    const wifi = getWifiStrength(status.wifi_signal);
    rows.push({
      label: t('printers.wifiSignalLabel'),
      value: (
        <span className="flex items-center gap-2">
          <Signal className={`w-4 h-4 ${wifi.color}`} />
          <span className={wifi.color}>{t(wifi.labelKey)}</span>
          <span className="text-bambu-gray text-xs">({status.wifi_signal} dBm)</span>
        </span>
      ),
    });
  }

  // Firmware
  rows.push({
    label: t('printers.firmware'),
    value: status?.firmware_version ?? '-',
  });

  // Developer Mode
  if (status?.developer_mode != null) {
    rows.push({
      label: t('printers.developerMode'),
      value: (
        <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
          status.developer_mode
            ? 'bg-bambu-green/20 text-bambu-green'
            : 'bg-bambu-dark-tertiary text-bambu-gray'
        }`}>
          {status.developer_mode ? t('printers.enabled') : t('printers.disabled')}
        </span>
      ),
    });
  }

  // Nozzle Count
  rows.push({
    label: t('printers.nozzleCount'),
    value: printer.nozzle_count,
  });

  // SD Card
  if (status?.sdcard != null) {
    rows.push({
      label: t('printers.sdCard'),
      value: (
        <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
          status.sdcard
            ? 'bg-bambu-green/20 text-bambu-green'
            : 'bg-bambu-dark-tertiary text-bambu-gray'
        }`}>
          {status.sdcard ? t('printers.inserted') : t('printers.notInserted')}
        </span>
      ),
    });
  }


  // Delete-from-SD-after-print toggle. Reuses the same long-form label as
  // the edit dialog (``printers.modal.cleanupAfterPrintLabel``) so users
  // see the same wording in both places — the old short ``cleanupAfterPrint``
  // label drifted into "Очищення після друку" which read ambiguously
  // (could mean plate-clearing) and didn't match the edit dialog.
  rows.push({
    label: t('printers.modal.cleanupAfterPrintLabel'),
    value: (
      <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
        printer.cleanup_after_print !== false
          ? 'bg-bambu-green/20 text-bambu-green'
          : 'bg-bambu-dark-tertiary text-bambu-gray'
      }`}>
        {printer.cleanup_after_print !== false ? t('printers.enabled') : t('printers.disabled')}
      </span>
    ),
  });

  // Plate-clear confirmation toggle — same setting & label as the edit
  // dialog so the two views stay in sync.
  rows.push({
    label: t('printers.modal.requirePlateClear'),
    value: (
      <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
        printer.require_plate_clear
          ? 'bg-bambu-green/20 text-bambu-green'
          : 'bg-bambu-dark-tertiary text-bambu-gray'
      }`}>
        {printer.require_plate_clear ? t('printers.enabled') : t('printers.disabled')}
      </span>
    ),
  });

  // Swap-mode (automated plate swapper) — only relevant for printer models
  // that have at least one swap profile registered (currently A1 / A1 Mini).
  // We render the bool indicator AND the active profile label; both are
  // hidden for models without any swap profile so the info card stays
  // compact for X1/P1/H2 series owners.
  //
  // Match is case-insensitive — the catalog uses canonical ``A1 Mini`` but
  // ``printer.model`` may have been seeded with ``A1 mini`` / ``A1MINI`` /
  // similar drift from older detect paths, and a strict Array.includes was
  // hiding the rows on real installs that have swap mode wired up.
  //
  // Fallback: if the catalog query hasn't loaded yet (or 401/403'd) but the
  // printer itself reports ``swap_mode_enabled === true`` or has a non-null
  // ``swap_profile``, we still surface the rows — the bool comes from the
  // printer record directly, the profile label degrades to the raw id.
  const modelLower = (printer.model ?? '').toLowerCase();
  const modelSwapProfiles =
    swapProfiles?.filter((p) => p.models.some((m) => m.toLowerCase() === modelLower)) ?? [];
  const showSwapRows =
    modelSwapProfiles.length > 0 || printer.swap_mode_enabled || !!printer.swap_profile;
  if (showSwapRows) {
    rows.push({
      label: t('printers.modal.swapMode'),
      value: (
        <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
          printer.swap_mode_enabled
            ? 'bg-bambu-green/20 text-bambu-green'
            : 'bg-bambu-dark-tertiary text-bambu-gray'
        }`}>
          {printer.swap_mode_enabled ? t('printers.enabled') : t('printers.disabled')}
        </span>
      ),
    });
    if (printer.swap_mode_enabled) {
      const activeProfile =
        modelSwapProfiles.find((p) => p.id === printer.swap_profile) ??
        swapProfiles?.find((p) => p.id === printer.swap_profile);
      // Show the short ``label`` (e.g. "Kit Edition") same as the edit
      // dropdown — the catalog id ("a1mini_kit") leaks only when the
      // catalog query hasn't loaded yet OR no match is found, in which
      // case the row falls back to that raw id so the field never goes
      // empty. Description is intentionally NOT used: the row label
      // already reads "Профіль swap-режиму", so adding the longer
      // description on top makes the row visually heavy.
      rows.push({
        label: t('printers.modal.swapProfile'),
        value: activeProfile?.label || printer.swap_profile || '-',
      });
    }
  }

  // MQTT Connection Timeout
  rows.push({
    label: t('printers.mqttConnectionTimeout'),
    value: printer.mqtt_connection_timeout
      ? `${printer.mqtt_connection_timeout}s`
      : t('printers.disabled'),
  });

  // Total Print Hours
  if (totalPrintHours != null && totalPrintHours > 0) {
    rows.push({
      label: t('printers.totalPrintHours'),
      value: `${Math.round(totalPrintHours)}h`,
    });
  }

  // Location
  if (printer.location) {
    rows.push({
      label: t('printers.sort.location'),
      value: printer.location,
    });
  }

  // Added date
  rows.push({
    label: t('printers.addedOn'),
    value: formatDateTime(printer.created_at, timeFormat, dateFormat),
  });

  return (
    <div
      className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4"
      role="dialog"
      aria-modal="true"
      onClick={onClose}
    >
      <Card className="w-full max-w-md" onClick={(e: React.MouseEvent) => e.stopPropagation()}>
        <CardContent>
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-white">
              {printer.name}
            </h2>
            <button onClick={onClose} className="p-1 hover:bg-bambu-dark rounded flex-shrink-0">
              <X className="w-5 h-5 text-bambu-gray" />
            </button>
          </div>

          {/* Printer Image */}
          <div className="flex justify-center mb-4">
            <img
              src={getPrinterImage(printer.model)}
              alt={printer.model ?? printer.name}
              className="h-24 object-contain"
            />
          </div>

          <div className="space-y-0">
            {rows.map((row, i) => (
              <div key={i} className="flex items-center justify-between gap-4 py-2.5 border-b border-bambu-dark-tertiary last:border-0">
                <span className="text-sm text-bambu-gray whitespace-nowrap">{row.label}</span>
                <span className="text-sm text-white text-right">{row.value}</span>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
