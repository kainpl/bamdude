import { Download } from 'lucide-react';
import { Button } from './Button';
import { Modal } from './Modal';
import { api } from '../api/client';

interface QRCodeModalProps {
  archiveId: number;
  archiveName: string;
  onClose: () => void;
}

export function QRCodeModal({ archiveId, archiveName, onClose }: QRCodeModalProps) {
  const qrCodeUrl = api.getArchiveQRCodeUrl(archiveId, 300);

  const handleDownload = () => {
    const link = document.createElement('a');
    link.href = qrCodeUrl;
    link.download = `${archiveName}_qrcode.png`;
    link.click();
  };

  return (
    <Modal onClose={onClose} title="QR Code" size="sm">
      {/* Content */}
      <div className="p-4 flex flex-col items-center">
        <p className="text-sm text-bambu-gray mb-4 text-center truncate max-w-full">
          {archiveName}
        </p>
        <div className="bg-white p-4 rounded-lg mb-4">
          <img
            src={qrCodeUrl}
            alt="QR Code"
            className="w-64 h-64"
          />
        </div>
        <p className="text-xs text-bambu-gray mb-4 text-center">
          Scan to open this archive
        </p>
        <Button onClick={handleDownload} className="w-full">
          <Download className="w-4 h-4" />
          Download QR Code
        </Button>
      </div>
    </Modal>
  );
}
