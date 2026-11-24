import { useEffect, useRef, useState } from 'react';
import { Camera, ImagePlus, Loader2, QrCode } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { api, type InventorySpool } from '../api/client';
import { readSpoolLabel } from '../utils/spoolLabel';
import { Button } from './Button';
import { Modal } from './Modal';

const PREVIEW_EDGE = 640;
const PHOTO_RETRY_EDGE = 2400;
const MAX_PHOTO_BYTES = 25 * 1024 * 1024;

function sample(
  canvas: HTMLCanvasElement,
  source: CanvasImageSource,
  width: number,
  height: number,
  maxEdge: number,
) {
  const scale = Math.min(1, maxEdge / Math.max(width, height));
  canvas.width = Math.max(1, Math.round(width * scale));
  canvas.height = Math.max(1, Math.round(height * scale));
  const context = canvas.getContext('2d', { willReadFrequently: true });
  if (!context) throw new Error('Canvas unavailable');
  context.drawImage(source, 0, 0, canvas.width, canvas.height);
  return context.getImageData(0, 0, canvas.width, canvas.height);
}

export interface ScannedSpool {
  spool: InventorySpool;
  key: string;
}

interface SpoolLabelScannerProps {
  target: string;
  onClose: () => void;
  onScan: (selection: ScannedSpool) => void;
}

export function SpoolLabelScanner({ target, onClose, onScan }: SpoolLabelScannerProps) {
  const { t } = useTranslation();
  const video = useRef<HTMLVideoElement>(null);
  const photo = useRef<HTMLInputElement>(null);
  const cameraStop = useRef<(() => void) | null>(null);
  const mounted = useRef(false);
  const [busy, setBusy] = useState(false);
  const [camera, setCamera] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      cameraStop.current?.();
    };
  }, []);

  const stopCamera = () => {
    cameraStop.current?.();
    cameraStop.current = null;
    setCamera(false);
  };

  const resolveLabel = async (payload: string) => {
    const label = readSpoolLabel(payload);
    if (!label) {
      setError(t('inventory.qrScan.invalidLabel'));
      return;
    }
    try {
      const spool = await api.getSpool(label.spoolId, label.key);
      if (mounted.current) onScan({ spool, key: label.key });
    } catch (err) {
      if (mounted.current) setError(err instanceof Error ? err.message : t('inventory.deepLinkFetchFailed'));
    }
  };

  const startCamera = async () => {
    setError('');
    if (!navigator.mediaDevices?.getUserMedia) {
      setError(t('inventory.qrScan.cameraUnavailable'));
      return;
    }
    setCamera(true);
    let stopped = false;
    let stream: MediaStream | undefined;
    let timer: number | undefined;
    cameraStop.current = () => {
      stopped = true;
      window.clearTimeout(timer);
      stream?.getTracks().forEach((track) => track.stop());
      if (video.current) video.current.srcObject = null;
    };
    try {
      const { default: decode } = await import('jsqr');
      if (stopped) return;
      stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } },
      });
      const player = video.current;
      if (stopped || !player) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      player.srcObject = stream;
      await player.play();
      if (stopped) return;

      const canvas = document.createElement('canvas');
      const scanFrame = async () => {
        if (stopped) return;
        try {
          if (player.videoWidth && player.videoHeight) {
            const frame = sample(canvas, player, player.videoWidth, player.videoHeight, PREVIEW_EDGE);
            const code = decode(frame.data, frame.width, frame.height, { inversionAttempts: 'attemptBoth' });
            if (code) {
              stopCamera();
              setBusy(true);
              await resolveLabel(code.data);
              if (mounted.current) setBusy(false);
              return;
            }
          }
          timer = window.setTimeout(() => { void scanFrame(); }, 180);
        } catch {
          stopCamera();
          setError(t('inventory.qrScan.cameraUnavailable'));
        }
      };
      void scanFrame();
    } catch {
      if (stopped) return;
      stopCamera();
      setError(t('inventory.qrScan.cameraUnavailable'));
    }
  };

  const scanPhoto = async (file: File | undefined) => {
    if (!file) return;
    stopCamera();
    setError('');
    if (file.size > MAX_PHOTO_BYTES) {
      setError(t('inventory.qrScan.photoTooLarge'));
      return;
    }
    setBusy(true);
    const url = URL.createObjectURL(file);
    const image = new Image();
    try {
      const { default: decode } = await import('jsqr');
      await new Promise<void>((resolve, reject) => {
        image.onload = () => resolve();
        image.onerror = reject;
        image.src = url;
      });
      if (!mounted.current) return;
      const canvas = document.createElement('canvas');
      const edges = Math.max(image.naturalWidth, image.naturalHeight) > PREVIEW_EDGE
        ? [PREVIEW_EDGE, PHOTO_RETRY_EDGE]
        : [PREVIEW_EDGE];
      for (const edge of edges) {
        const frame = sample(canvas, image, image.naturalWidth, image.naturalHeight, edge);
        const code = decode(frame.data, frame.width, frame.height, { inversionAttempts: 'attemptBoth' });
        if (code) {
          await resolveLabel(code.data);
          return;
        }
      }
      setError(t('inventory.qrScan.noCode'));
    } catch {
      if (mounted.current) setError(t('inventory.qrScan.photoFailed'));
    } finally {
      URL.revokeObjectURL(url);
      if (mounted.current) setBusy(false);
    }
  };

  return (
    <Modal onClose={onClose} title={t('inventory.qrScan.scan')} icon={<QrCode className="w-5 h-5 text-bambu-green" />}>
      <div className="space-y-4 p-4">
        <p className="text-sm text-bambu-gray">{target}</p>
        {camera && (
          <video
            ref={video}
            playsInline
            muted
            className="max-h-72 w-full rounded-lg bg-black object-contain"
            aria-label={t('inventory.qrScan.camera')}
          />
        )}
        {error && <p role="alert" className="text-sm text-red-400">{error}</p>}
        <div className="flex flex-wrap items-center gap-2">
          {camera ? (
            <Button variant="secondary" onClick={stopCamera}>{t('inventory.qrScan.stopCamera')}</Button>
          ) : (
            <Button onClick={() => void startCamera()} disabled={busy}>
              <Camera className="w-4 h-4" />
              {t('inventory.qrScan.camera')}
            </Button>
          )}
          <Button variant="secondary" onClick={() => photo.current?.click()} disabled={busy}>
            <ImagePlus className="w-4 h-4" />
            {t('inventory.qrScan.photo')}
          </Button>
          {busy && <Loader2 aria-label={t('common.loading')} className="w-4 h-4 animate-spin" />}
        </div>
        <input
          ref={photo}
          type="file"
          accept="image/*"
          className="hidden"
          onChange={(event) => {
            const file = event.currentTarget.files?.[0];
            event.currentTarget.value = '';
            void scanPhoto(file);
          }}
        />
      </div>
    </Modal>
  );
}
