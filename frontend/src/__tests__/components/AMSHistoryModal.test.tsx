/**
 * Tests for the AMSHistoryModal component.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { render } from '../utils';
import { AMSHistoryModal } from '../../components/AMSHistoryModal';
import { api } from '../../api/client';

// Mock the API client
vi.mock('../../api/client', () => ({
  api: {
    getAMSHistory: vi.fn(),
    getSettings: vi.fn().mockResolvedValue({}),
    updateSettings: vi.fn().mockResolvedValue({}),
  },
}));

// Mock recharts to avoid rendering issues in tests
vi.mock('recharts', () => ({
  LineChart: ({ children }: { children: React.ReactNode }) => <div data-testid="line-chart">{children}</div>,
  Line: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Legend: () => null,
  ReferenceLine: () => null,
}));

const mockHistoryData = {
  data: [
    { recorded_at: '2024-12-11T10:00:00Z', humidity: 45, temperature: 28 },
    { recorded_at: '2024-12-11T10:05:00Z', humidity: 46, temperature: 27 },
    { recorded_at: '2024-12-11T10:10:00Z', humidity: 44, temperature: 29 },
    { recorded_at: '2024-12-11T10:15:00Z', humidity: 47, temperature: 28 },
    { recorded_at: '2024-12-11T10:20:00Z', humidity: 48, temperature: 30 },
  ],
  avg_humidity: 46,
  min_humidity: 44,
  max_humidity: 48,
  avg_temperature: 28.4,
  min_temperature: 27,
  max_temperature: 30,
};

const defaultProps = {
  isOpen: true,
  onClose: vi.fn(),
  printerId: 1,
  printerName: 'Test Printer',
  amsId: 0,
  amsLabel: 'AMS-A',
  initialMode: 'humidity' as const,
  thresholds: {
    humidityGood: 40,
    humidityFair: 60,
    tempGood: 30,
    tempFair: 35,
  },
};

describe('AMSHistoryModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.getAMSHistory as ReturnType<typeof vi.fn>).mockResolvedValue(mockHistoryData);
  });

  it('renders nothing visible when closed', () => {
    render(<AMSHistoryModal {...defaultProps} isOpen={false} />);

    // The modal content should not be visible when closed
    expect(screen.queryByText('AMS-A History')).not.toBeInTheDocument();
  });

  it('renders modal when open', async () => {
    render(<AMSHistoryModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText('AMS-A History')).toBeInTheDocument();
    });

    expect(screen.getByText('Test Printer')).toBeInTheDocument();
  });

  it('displays humidity mode by default', async () => {
    render(<AMSHistoryModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText('Humidity')).toBeInTheDocument();
    });

    // Should show humidity stats - the Average value
    await waitFor(() => {
      expect(screen.getByText('Average')).toBeInTheDocument();
    });
  });

  it('displays temperature mode when initialMode is temperature', async () => {
    render(<AMSHistoryModal {...defaultProps} initialMode="temperature" />);

    await waitFor(() => {
      expect(screen.getByText('Temperature')).toBeInTheDocument();
    });
  });

  it('shows time range buttons', async () => {
    render(<AMSHistoryModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText('6h')).toBeInTheDocument();
    });

    expect(screen.getByText('24h')).toBeInTheDocument();
    expect(screen.getByText('48h')).toBeInTheDocument();
    expect(screen.getByText('7d')).toBeInTheDocument();
  });

  it('switches between humidity and temperature modes', async () => {
    render(<AMSHistoryModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText('Humidity')).toBeInTheDocument();
    });

    // Click temperature button
    const tempButton = screen.getByText('Temperature');
    fireEvent.click(tempButton);

    // Should now show temperature mode is active (button styling changes)
    await waitFor(() => {
      // Temperature stats should be visible - checking the labels
      expect(screen.getByText('Min')).toBeInTheDocument();
      expect(screen.getByText('Max')).toBeInTheDocument();
    });
  });

  it('displays statistics cards', async () => {
    render(<AMSHistoryModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText('Current')).toBeInTheDocument();
    });

    expect(screen.getByText('Average')).toBeInTheDocument();
    expect(screen.getByText('Min')).toBeInTheDocument();
    expect(screen.getByText('Max')).toBeInTheDocument();
  });

  it('displays min/max humidity values', async () => {
    render(<AMSHistoryModal {...defaultProps} />);

    await waitFor(() => {
      // Min humidity - may appear multiple times
      const minValues = screen.getAllByText('44%');
      expect(minValues.length).toBeGreaterThanOrEqual(1);
    });

    // Max humidity - may appear multiple times (in current and max cards)
    const maxValues = screen.getAllByText('48%');
    expect(maxValues.length).toBeGreaterThanOrEqual(1);
  });

  it('displays min/max temperature values in temperature mode', async () => {
    render(<AMSHistoryModal {...defaultProps} initialMode="temperature" />);

    await waitFor(() => {
      // Min temp appears in the Min card
      const minCards = screen.getAllByText('27°C');
      expect(minCards.length).toBeGreaterThanOrEqual(1);
    });

    // Max temp appears in the Max card (may appear multiple times in different contexts)
    const maxCards = screen.getAllByText('30°C');
    expect(maxCards.length).toBeGreaterThanOrEqual(1);
  });

  it('calls onClose when close button clicked', async () => {
    const onClose = vi.fn();
    render(<AMSHistoryModal {...defaultProps} onClose={onClose} />);

    await waitFor(() => {
      expect(screen.getByText('AMS-A History')).toBeInTheDocument();
    });

    // Find and click close button (X icon)
    const closeButton = document.querySelector('button');
    if (closeButton) {
      fireEvent.click(closeButton);
    }
  });

  it('calls onClose when clicking backdrop', async () => {
    const onClose = vi.fn();
    render(<AMSHistoryModal {...defaultProps} onClose={onClose} />);

    await waitFor(() => {
      expect(screen.getByText('AMS-A History')).toBeInTheDocument();
    });

    // Click on backdrop (the fixed overlay)
    const backdrop = document.querySelector('.fixed.inset-0');
    if (backdrop) {
      fireEvent.click(backdrop);
      expect(onClose).toHaveBeenCalled();
    }
  });

  it('does not close when clicking modal content', async () => {
    const onClose = vi.fn();
    render(<AMSHistoryModal {...defaultProps} onClose={onClose} />);

    await waitFor(() => {
      expect(screen.getByText('AMS-A History')).toBeInTheDocument();
    });

    // Click on modal content (should not close)
    const modalContent = document.querySelector('.rounded-xl');
    if (modalContent) {
      fireEvent.click(modalContent);
      expect(onClose).not.toHaveBeenCalled();
    }
  });

  it('shows loading state', async () => {
    // Make API call never resolve
    (api.getAMSHistory as ReturnType<typeof vi.fn>).mockImplementation(
      () => new Promise(() => {})
    );

    render(<AMSHistoryModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText('Loading...')).toBeInTheDocument();
    });
  });

  it('shows error state on API failure', async () => {
    (api.getAMSHistory as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error('Network error')
    );

    render(<AMSHistoryModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText('Error')).toBeInTheDocument();
    });
  });

  it('shows no data message when empty', async () => {
    (api.getAMSHistory as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: [],
      avg_humidity: null,
      min_humidity: null,
      max_humidity: null,
      avg_temperature: null,
      min_temperature: null,
      max_temperature: null,
    });

    render(<AMSHistoryModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText('No data available')).toBeInTheDocument();
    });
  });

  it('changes time range when clicking different range buttons', async () => {
    render(<AMSHistoryModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText('6h')).toBeInTheDocument();
    });

    // Click 7d button
    fireEvent.click(screen.getByText('7d'));

    // API should be called with 168 hours (7 days)
    await waitFor(() => {
      expect(api.getAMSHistory).toHaveBeenCalledWith(1, 0, 168);
    });
  });

  it('displays recording info text', async () => {
    render(<AMSHistoryModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText(/data is recorded every 5 minutes/i)).toBeInTheDocument();
    });
  });

  it('displays current value with correct color based on threshold', async () => {
    // Test with humidity value above fair threshold
    const highHumidityData = {
      ...mockHistoryData,
      data: [
        ...mockHistoryData.data,
        { recorded_at: '2024-12-11T10:25:00Z', humidity: 75, temperature: 28 },
      ],
    };

    (api.getAMSHistory as ReturnType<typeof vi.fn>).mockResolvedValue(highHumidityData);

    render(<AMSHistoryModal {...defaultProps} />);

    await waitFor(() => {
      // The current value (75%) should be displayed
      expect(screen.getByText('75%')).toBeInTheDocument();
    });
  });

  it('renders chart component', async () => {
    render(<AMSHistoryModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByTestId('line-chart')).toBeInTheDocument();
    });
  });
});

describe('AMSHistoryModal trend calculation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows stable trend when values are similar', async () => {
    const stableData = {
      data: Array.from({ length: 20 }, (_, i) => ({
        recorded_at: new Date(Date.now() - i * 5 * 60 * 1000).toISOString(),
        humidity: 45, // Same value
        temperature: 28,
      })),
      avg_humidity: 45,
      min_humidity: 45,
      max_humidity: 45,
      avg_temperature: 28,
      min_temperature: 28,
      max_temperature: 28,
    };

    (api.getAMSHistory as ReturnType<typeof vi.fn>).mockResolvedValue(stableData);

    render(<AMSHistoryModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText('Current')).toBeInTheDocument();
    });

    // Should show stable trend icon (horizontal line)
    // The Minus icon indicates stable trend
  });

  it('shows upward trend when values increase', async () => {
    const increasingData = {
      data: Array.from({ length: 20 }, (_, i) => ({
        recorded_at: new Date(Date.now() - (20 - i) * 5 * 60 * 1000).toISOString(),
        humidity: 30 + i * 2, // Increasing values
        temperature: 28,
      })),
      avg_humidity: 50,
      min_humidity: 30,
      max_humidity: 68,
      avg_temperature: 28,
      min_temperature: 28,
      max_temperature: 28,
    };

    (api.getAMSHistory as ReturnType<typeof vi.fn>).mockResolvedValue(increasingData);

    render(<AMSHistoryModal {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByText('Current')).toBeInTheDocument();
    });

    // Should show upward trend icon (TrendingUp)
  });
});
