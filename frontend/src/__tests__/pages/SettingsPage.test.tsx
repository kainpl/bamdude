/**
 * Tests for the SettingsPage component.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { SettingsPage } from '../../pages/SettingsPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const mockSettings = {
  save_thumbnails: true,
  capture_finish_photo: true,
  default_filament_cost: 25.0,
  currency: 'USD',
  ams_humidity_good: 40,
  ams_humidity_fair: 60,
  ams_temp_good: 30,
  ams_temp_fair: 35,
  time_format: 'system',
  date_format: 'system',
  mqtt_enabled: false,
  mqtt_host: '',
  mqtt_port: 1883,
  spoolman_enabled: false,
  spoolman_url: '',
  ha_enabled: false,
  ha_url: '',
  ha_token: '',
  check_updates: false,
  check_printer_firmware: false,
  bed_cooled_threshold: 35,
};

describe('SettingsPage', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/settings/', () => {
        return HttpResponse.json(mockSettings);
      }),
      http.patch('/api/v1/settings/', async ({ request }) => {
        const body = await request.json();
        return HttpResponse.json({ ...mockSettings, ...body });
      }),
      http.get('/api/v1/printers/', () => {
        return HttpResponse.json([]);
      }),
      http.get('/api/v1/smart-plugs/', () => {
        return HttpResponse.json([]);
      }),
      http.get('/api/v1/notifications/', () => {
        return HttpResponse.json([]);
      }),
      http.get('/api/v1/api-keys/', () => {
        return HttpResponse.json([]);
      }),
      http.get('/api/v1/mqtt/status', () => {
        return HttpResponse.json({ enabled: false });
      }),
      http.get('/api/v1/virtual-printer/status', () => {
        return HttpResponse.json({ running: false });
      }),
      http.get('/api/v1/auth/status', () => {
        return HttpResponse.json({ auth_enabled: false, requires_setup: false });
      })
    );
  });

  describe('rendering', () => {
    it('renders the page title', async () => {
      render(<SettingsPage />);

      await waitFor(() => {
        // Use role-based query to avoid conflicts with dropdown options
        expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument();
      });
    });

    it('shows settings tabs', async () => {
      render(<SettingsPage />);

      await waitFor(() => {
        // Use getAllByText since "General" appears both as tab and section heading
        expect(screen.getAllByText('General').length).toBeGreaterThan(0);
        expect(screen.getByText('Smart Plugs')).toBeInTheDocument();
        expect(screen.getAllByText('Notifications').length).toBeGreaterThan(0);
        expect(screen.getAllByText('Filament').length).toBeGreaterThan(0);
        expect(screen.getByText('Network')).toBeInTheDocument();
        expect(screen.getByText('API Keys')).toBeInTheDocument();
      });
    });
  });

  describe('general settings', () => {
    it('shows date format setting', async () => {
      render(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Date Format')).toBeInTheDocument();
      });
    });

    it('shows time format setting', async () => {
      render(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Time Format')).toBeInTheDocument();
      });
    });

    it('shows default printer setting', async () => {
      render(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Default Printer')).toBeInTheDocument();
      });
    });

    it('shows preferred slicer setting', async () => {
      render(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Preferred Slicer')).toBeInTheDocument();
      });
    });

    it('shows slicer dropdown with both options', async () => {
      render(<SettingsPage />);

      await waitFor(() => {
        const slicerSelect = screen.getAllByDisplayValue('Bambu Studio');
        expect(slicerSelect.length).toBeGreaterThan(0);
      });
    });

    it('shows appearance section', async () => {
      render(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Appearance')).toBeInTheDocument();
      });
    });

    it('shows updates section with firmware toggle', async () => {
      render(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Updates')).toBeInTheDocument();
        expect(screen.getByText('Check for updates')).toBeInTheDocument();
        expect(screen.getByText('Check printer firmware')).toBeInTheDocument();
      });
    });
  });

  describe('tabs navigation', () => {
    it('can switch to Network tab', async () => {
      const user = userEvent.setup();
      render(<SettingsPage />);

      // Wait for settings to load first
      await waitFor(() => {
        expect(screen.getByText('Date Format')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Network'));

      await waitFor(() => {
        // Network tab contains MQTT Publishing section
        expect(screen.getByText('MQTT Publishing')).toBeInTheDocument();
      });
    });

    it('can switch to Smart Plugs tab', async () => {
      const user = userEvent.setup();
      render(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('Smart Plugs')).toBeInTheDocument();
      });

      await user.click(screen.getByText('Smart Plugs'));

      await waitFor(() => {
        expect(screen.getByText('Add Smart Plug')).toBeInTheDocument();
      });
    });

    it('can switch to Notifications tab', async () => {
      const user = userEvent.setup();
      render(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getAllByText('Notifications').length).toBeGreaterThan(0);
      });

      // Click the tab button (not the mobile dropdown option)
      const notificationButtons = screen.getAllByText('Notifications');
      const tabButton = notificationButtons.find(el => el.tagName === 'BUTTON') || notificationButtons[0];
      await user.click(tabButton);

      await waitFor(() => {
        expect(screen.getByText('Add Provider')).toBeInTheDocument();
      });
    });

    it('can switch to Filament tab', async () => {
      const user = userEvent.setup();
      render(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getAllByText('Filament').length).toBeGreaterThan(0);
      });

      await user.click(screen.getAllByText('Filament')[0]);

      await waitFor(() => {
        expect(screen.getByText('AMS Display Thresholds')).toBeInTheDocument();
      });
    });
  });

  describe('API Keys tab', () => {
    it('can switch to API Keys tab', async () => {
      const user = userEvent.setup();
      render(<SettingsPage />);

      await waitFor(() => {
        expect(screen.getByText('API Keys')).toBeInTheDocument();
      });

      await user.click(screen.getByText('API Keys'));

      await waitFor(() => {
        // Button text is "Create Key"
        expect(screen.getByText('Create Key')).toBeInTheDocument();
      });
    });
  });

  describe('API Keys tab — delete flow (audit A.26)', () => {
    // Without setQueryData on success the deleted row stayed visible until a
    // manual reload — invalidateQueries didn't reliably trigger a UI swap on
    // every browser. Pin the synchronous-removal contract here.
    it('removes a deleted key from the list without a page reload', async () => {
      const initialKeys = [
        {
          id: 42,
          name: 'CI deploy key',
          key_prefix: 'bb_abcd1234',
          can_queue: true,
          can_control_printer: false,
          can_read_status: true,
          printer_ids: null,
          enabled: true,
          last_used: null,
          created_at: '2026-01-01T00:00:00Z',
          expires_at: null,
        },
      ];

      let deleteCallCount = 0;
      server.use(
        http.get('/api/v1/api-keys/', () => HttpResponse.json(initialKeys)),
        http.delete('/api/v1/api-keys/:id', ({ params }) => {
          deleteCallCount += 1;
          expect(params.id).toBe('42');
          return HttpResponse.json({ message: 'API key deleted' });
        })
      );

      const user = userEvent.setup();
      render(<SettingsPage />);

      // Switch to API Keys tab. Both desktop tab + mobile dropdown render
      // the label, so just grab the button form.
      await waitFor(() => {
        expect(screen.getAllByText('API Keys').length).toBeGreaterThan(0);
      });
      const tabButton = screen.getAllByText('API Keys').find((el) => el.tagName === 'BUTTON');
      expect(tabButton).toBeDefined();
      await user.click(tabButton!);

      // Key is listed
      await waitFor(() => {
        expect(screen.getByText('CI deploy key')).toBeInTheDocument();
      });

      // Click the trash button on the row
      const cards = screen.getByText('CI deploy key').closest('.flex.items-center.justify-between');
      expect(cards).not.toBeNull();
      const trashButton = cards!.querySelectorAll('button');
      await user.click(trashButton[trashButton.length - 1]);

      // Confirm the deletion in the modal
      const confirmButton = await screen.findByRole('button', { name: /delete/i });
      await user.click(confirmButton);

      // The deleted key disappears from the list immediately — no manual
      // reload required. setQueryData drops it before any refetch could fire.
      await waitFor(() => {
        expect(screen.queryByText('CI deploy key')).not.toBeInTheDocument();
      });

      expect(deleteCallCount).toBe(1);
    });
  });

  describe('external camera snapshot URL override (#1177)', () => {
    /**
     * The snapshot URL input only appears for stream camera types where the
     * MJPEG warm-up problem can occur (mjpeg / rtsp / usb). Pure HTTP
     * snapshot sources don't need an override since their stream URL is
     * already a single-frame endpoint.
     */
    const mjpegPrinter = {
      id: 7,
      name: 'go2rtc Cam',
      serial_number: 'TEST123',
      ip_address: '192.168.1.100',
      access_code: 'XXXX',
      model: 'P1S',
      location: null,
      nozzle_count: 1,
      is_active: true,
      auto_archive: true,
      external_camera_url: 'http://192.168.1.61:1984/api/stream.mjpeg?src=printer',
      external_camera_type: 'mjpeg',
      external_camera_enabled: true,
      external_camera_snapshot_url: null,
      camera_rotation: 0,
      plate_detection_enabled: false,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    };

    /**
     * The External Cameras section sits inside the Printing tab in our layout
     * (different from upstream's tab arrangement). Each test must switch to
     * Printing first before asserting on snapshot-URL UI.
     */
    const switchToPrintingTab = async () => {
      const user = userEvent.setup();
      const tabButton = await waitFor(() => {
        const buttons = screen
          .getAllByText('Printing')
          .filter((el) => el.tagName === 'BUTTON');
        expect(buttons.length).toBeGreaterThan(0);
        return buttons[0];
      });
      await user.click(tabButton);
    };

    it('renders the snapshot URL input when camera_type is mjpeg', async () => {
      server.use(
        http.get('/api/v1/printers/', () => HttpResponse.json([mjpegPrinter])),
      );

      render(<SettingsPage />);
      await switchToPrintingTab();

      await waitFor(() => {
        expect(screen.getByPlaceholderText(/api\/frame\.jpeg\?src=printer/)).toBeInTheDocument();
      });
    });

    it('hides the snapshot URL input when camera_type is snapshot (already a single-frame source)', async () => {
      server.use(
        http.get('/api/v1/printers/', () =>
          HttpResponse.json([{ ...mjpegPrinter, external_camera_type: 'snapshot' }]),
        ),
      );

      render(<SettingsPage />);
      await switchToPrintingTab();

      // Wait for the printer name (rendered alongside the camera section) so
      // we know the printing tab finished mounting before asserting absence
      // of the snapshot input.
      await waitFor(() => {
        expect(screen.getByText('go2rtc Cam')).toBeInTheDocument();
      });
      expect(screen.queryByPlaceholderText(/api\/frame\.jpeg\?src=printer/)).not.toBeInTheDocument();
    });

    it(
      'PATCHes the printer with external_camera_snapshot_url when the user types into the input',
      async () => {
        let receivedBody: Record<string, unknown> | null = null;
        server.use(
          http.get('/api/v1/printers/', () => HttpResponse.json([mjpegPrinter])),
          http.patch('/api/v1/printers/7', async ({ request }) => {
            receivedBody = (await request.json()) as Record<string, unknown>;
            return HttpResponse.json({ ...mjpegPrinter, ...receivedBody });
          }),
        );

        render(<SettingsPage />);
        await switchToPrintingTab();

        const input = await waitFor(() =>
          screen.getByPlaceholderText(/api\/frame\.jpeg\?src=printer/),
        );

        // delay:null makes user.type() instant instead of 50ms-per-char —
        // 51 chars × 50ms ≈ 2.5s of pure typing on top of the debounced
        // PATCH wait, which tipped the default 5s test timeout under load.
        const user = userEvent.setup({ delay: null });
        await user.type(input, 'http://192.168.1.61:1984/api/frame.jpeg?src=printer');

        // Save is debounced by 800ms; assert the PATCH eventually fires with
        // the typed snapshot URL.
        await waitFor(
          () => {
            expect(receivedBody).not.toBeNull();
            expect(receivedBody!.external_camera_snapshot_url).toBe(
              'http://192.168.1.61:1984/api/frame.jpeg?src=printer',
            );
          },
          { timeout: 5000 },
        );
      },
      // Solo run is ~2.6s, but in the full suite this test routinely lands
      // last and the cold-start render + 800ms debounce + waitFor compete
      // for the default 5s window. 15s test-level timeout absorbs the
      // suite-load jitter without masking real regressions (the inner
      // waitFor still caps PATCH-wait at 5s).
      15000,
    );
  });
});
