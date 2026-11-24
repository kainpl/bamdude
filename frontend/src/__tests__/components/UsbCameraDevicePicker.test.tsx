import { fireEvent, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { UsbCameraDevicePicker } from '../../components/UsbCameraDevicePicker';
import { render } from '../utils';

describe('USB camera devices', () => {
  const listing = {
    cameras: [{ device: '/dev/video2', name: 'Camera' }],
    configured_devices_found: { 3: false },
  };

  it('selects a detected device and can refresh a missing saved device', () => {
    const onSelect = vi.fn();
    const onRefresh = vi.fn();
    render(<UsbCameraDevicePicker
      printerName="P1S" printerId={3} stored value=""
      listing={listing} isFetching={false} isError={false} onSelect={onSelect} onRefresh={onRefresh}
    />);
    expect(screen.getByRole('status')).toHaveTextContent('The configured USB device was not found.');
    expect(screen.getByRole('option', { name: 'Saved device' })).toHaveProperty('selected', true);
    fireEvent.change(screen.getByRole('combobox', { name: /detected usb devices/i }), { target: { value: '/dev/video2' } });
    expect(onSelect).toHaveBeenCalledWith('/dev/video2');
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(onRefresh).toHaveBeenCalledOnce();
  });

  it('shows a manual fallback even when discovery fails and applies only a completed V4L2 path', () => {
    const onSelect = vi.fn();
    render(<UsbCameraDevicePicker
      printerName="P1S" printerId={3} stored value="rtsp://private:password@private.example/live"
      listing={undefined} isFetching={false} isError onSelect={onSelect} onRefresh={vi.fn()}
    />);
    expect(screen.getByRole('status')).toHaveTextContent('Could not list USB devices');
    fireEvent.click(screen.getByRole('button', { name: /enter device path manually/i }));
    const input = screen.getByRole('textbox', { name: /device path/i });
    expect(input).toHaveValue('');
    fireEvent.change(input, { target: { value: '/dev/video' } });
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    fireEvent.change(input, { target: { value: '/dev/video07' } });
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSelect).toHaveBeenCalledWith('/dev/video7');
  });
});
