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

/** The checkbox that sits in the same row as a given label. */
const toggleFor = (label: string): HTMLInputElement => {
  const row = screen.getByText(label).closest('.flex.items-center.justify-between');
  expect(row, `no toggle row around "${label}"`).not.toBeNull();
  const input = row!.querySelector('input[type="checkbox"]');
  expect(input, `no checkbox in the "${label}" row`).not.toBeNull();
  return input as HTMLInputElement;
};

describe('SettingsPage', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/settings/', () => {
        return HttpResponse.json(mockSettings);
      }),
      http.patch('/api/v1/settings/', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
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

  describe('slicing settings', () => {
    it('keeps slicer configuration out of General and opens it from Slicing', async () => {
      const user = userEvent.setup();
      render(<SettingsPage />);

      await screen.findByText('Date Format');
      expect(screen.queryByText('Preferred Slicer')).not.toBeInTheDocument();

      await user.click(screen.getByRole('button', { name: 'Slicing' }));

      expect(await screen.findByText('Preferred Slicer')).toBeInTheDocument();
      expect(screen.getByText('Open in Slicer')).toBeInTheDocument();
      expect(screen.getByText('Enable server-side slicing')).toBeInTheDocument();
      expect(screen.getAllByDisplayValue('Bambu Studio')).toHaveLength(1);

      // Tab selection is intentionally reflected in the URL. Restore the
      // default so this test does not leak ?tab=slicing into the next one.
      await user.click(screen.getByRole('button', { name: 'General' }));
      await screen.findByText('Date Format');
    });

    it('shows saved slice settings on Slicing, not Printing', async () => {
      server.use(
        http.get('/api/v1/settings/', () => HttpResponse.json({ ...mockSettings, use_slicer_api: true })),
        http.get('/api/v1/slicer-pipelines/', () => HttpResponse.json({ pipelines: [] })),
        http.get('/api/v1/slicer/presets', () => HttpResponse.json({ printers: [], processes: [], filaments: [] })),
      );
      const user = userEvent.setup();
      render(<SettingsPage />);

      await user.click(await screen.findByRole('button', { name: 'Slicing' }));
      expect(await screen.findByText('Slice settings')).toBeInTheDocument();

      await user.click(screen.getByRole('button', { name: 'Printing' }));
      expect(screen.queryByText('Slice settings')).not.toBeInTheDocument();

      await user.click(screen.getByRole('button', { name: 'General' }));
      await screen.findByText('Date Format');
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

  describe('light for the camera', () => {
    const printerRow = (overrides: Record<string, unknown> = {}) => ({
      id: 9,
      name: 'Mini by the window',
      serial_number: 'MINI0001',
      ip_address: '192.168.1.109',
      access_code: 'XXXX',
      model: 'A1 mini',
      location: null,
      nozzle_count: 1,
      is_active: true,
      auto_archive: true,
      external_camera_url: null,
      external_camera_type: null,
      external_camera_enabled: false,
      external_camera_snapshot_url: null,
      camera_rotation: 0,
      camera_light_auto: 'inherit',
      plate_detection_enabled: false,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
      ...overrides,
    });
    const status = (overrides: Record<string, unknown>) =>
      http.get('/api/v1/printers/:id/status', ({ params }) =>
        HttpResponse.json({ id: Number(params.id), name: 'Mini by the window', connected: true, state: 'IDLE', ...overrides }),
      );
    // The tab a previous test left open survives into this one, so every
    // test here opens the tab it needs instead of trusting the default.
    const openTab = async (label: string) => {
      const user = userEvent.setup();
      const tab = await waitFor(() => {
        const buttons = screen.getAllByText(label).filter((el) => el.tagName === 'BUTTON');
        expect(buttons.length).toBeGreaterThan(0);
        return buttons[0];
      });
      await user.click(tab);
    };

    it('the Obico toggle appears only once the light toggle is on, and both are saved', async () => {
      const saved: Record<string, unknown>[] = [];
      server.use(
        http.get('/api/v1/settings/', () => HttpResponse.json({ ...mockSettings, obico_enabled: true, camera_light_auto: false, camera_light_auto_obico: false })),
        http.put('/api/v1/settings/', async ({ request }) => {
          const body = (await request.json()) as Record<string, unknown>;
          saved.push(body);
          return HttpResponse.json({ ...mockSettings, ...body });
        }),
      );
      render(<SettingsPage />);
      await openTab('Printing');
      const light = await waitFor(() => toggleFor('Light for the camera'), { timeout: 5000 });
      expect(light.checked).toBe(false);
      expect(screen.queryByText('Also for Obico failure detection')).not.toBeInTheDocument();

      const user = userEvent.setup();
      await user.click(light);
      await waitFor(() => toggleFor('Also for Obico failure detection'), { timeout: 5000 });
      await user.click(toggleFor('Also for Obico failure detection'));
      await waitFor(() => {
        const last = saved.at(-1);
        expect(last?.camera_light_auto).toBe(true);
        expect(last?.camera_light_auto_obico).toBe(true);
      }, { timeout: 5000 });
    }, 15000);

    it('a printer that reported a light gets its own selector, and the choice is sent as camera_light_auto', async () => {
      const patches: Record<string, unknown>[] = [];
      server.use(
        http.get('/api/v1/settings/', () => HttpResponse.json({ ...mockSettings, camera_light_auto: true })),
        http.get('/api/v1/printers/', () => HttpResponse.json([printerRow()])),
        status({ has_chamber_light: true }),
        http.patch('/api/v1/printers/:id', async ({ request }) => {
          const body = (await request.json()) as Record<string, unknown>;
          patches.push(body);
          return HttpResponse.json(printerRow(body));
        }),
      );
      render(<SettingsPage />);
      await openTab('Printing');
      const select = (await screen.findByLabelText('Light for the camera', {}, { timeout: 5000 })) as HTMLSelectElement;
      expect(select.value).toBe('inherit');
      await userEvent.setup().selectOptions(select, 'off');
      await waitFor(() => expect(patches).toEqual([{ camera_light_auto: 'off' }]), { timeout: 5000 });
    }, 15000);

    it('a connected printer with no chamber light has no selector', async () => {
      server.use(
        http.get('/api/v1/settings/', () => HttpResponse.json({ ...mockSettings, camera_light_auto: true })),
        http.get('/api/v1/printers/', () => HttpResponse.json([printerRow()])),
        status({ has_chamber_light: false }),
      );
      render(<SettingsPage />);
      await openTab('Printing');
      await screen.findByText('Mini by the window', {}, { timeout: 5000 });
      await waitFor(() => expect(screen.queryByLabelText('Light for the camera')).not.toBeInTheDocument(), { timeout: 5000 });
    }, 15000);

    it('with the farm toggle off there is no per-printer selector at all', async () => {
      server.use(
        http.get('/api/v1/settings/', () => HttpResponse.json({ ...mockSettings, camera_light_auto: false })),
        http.get('/api/v1/printers/', () => HttpResponse.json([printerRow()])),
        status({ has_chamber_light: true }),
      );
      render(<SettingsPage />);
      await openTab('Printing');
      await screen.findByText('Mini by the window', {}, { timeout: 5000 });
      expect(screen.queryByLabelText('Light for the camera')).not.toBeInTheDocument();
    }, 15000);
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


  describe('macro editor — layer trigger and action parameter', () => {
    beforeEach(() => {
      server.use(
        http.get('/api/v1/macros/', () => HttpResponse.json([])),
        http.get('/api/v1/macros/meta', () =>
          HttpResponse.json({
            events: { swap_mode_start: 'Swap Mode - Start', layer_reached: 'Layer Reached' },
            swap_events: ['swap_mode_start'],
            printer_models: {},
            swap_profiles: [],
            mqtt_actions: [
              {
                id: 'print_speed',
                label: 'Print speed',
                i18n_key: 'printSpeed',
                param: {
                  kind: 'choice',
                  i18n_key: 'speedLevel',
                  default: '2',
                  choices: [
                    { value: '1', label: 'Silent', i18n_key: 'silent' },
                    { value: '2', label: 'Standard', i18n_key: 'standard' },
                  ],
                  min_value: null,
                  max_value: null,
                  unit: null,
                },
              },
            ],
          })
        )
      );
    });

    const openMacroModal = async (user: ReturnType<typeof userEvent.setup>) => {
      render(<SettingsPage />);
      await user.click(await screen.findByText('Printing'));
      await user.click(await screen.findByText('Add Macro'));
      await screen.findByText('Event');
    };

    it('asks for a layer only on the layer_reached event', async () => {
      const user = userEvent.setup();
      await openMacroModal(user);

      expect(screen.queryByText('Fires once, when the print reaches this layer.')).not.toBeInTheDocument();

      // The label isn't tied to the select, so reach it through an option only
      // it can own.
      const eventSelect = (await screen.findByRole('option', { name: 'Layer reached' })).closest('select')!;
      await user.selectOptions(eventSelect, 'layer_reached');

      expect(await screen.findByText('Fires once, when the print reaches this layer.')).toBeInTheDocument();
    });

    it('renders the parameter control the server describes', async () => {
      const user = userEvent.setup();
      await openMacroModal(user);

      await user.click(screen.getByText('MQTT action', { selector: 'button' }));

      expect(await screen.findByText('Speed')).toBeInTheDocument();
      expect(screen.getByText('Silent')).toBeInTheDocument();
      expect(screen.getByText('Standard')).toBeInTheDocument();
    });
  });

  describe('Filament checks — prefer_lowest_filament', () => {
    /**
     * The rule was honoured by the print dialog, the auto-queue and the
     * virtual printer long before anything on screen could turn it on. What
     * this pins is the wiring, not the rule: the toggle has to reach the PUT
     * body, which means both hand-written lists in SettingsPage (the
     * hasChanges comparison and the save payload) carry the key. The drift
     * guard next door proves the two lists agree with each other; only an
     * actual save proves they agree with the server.
     */
    const LABEL = 'Drain the emptiest spool first';

    /** Click the Filament tab and wait for the Filament checks card. */
    const switchToFilamentTab = async (user: ReturnType<typeof userEvent.setup>) => {
      await waitFor(() => {
        expect(screen.getAllByText('Filament').length).toBeGreaterThan(0);
      });
      await user.click(screen.getAllByText('Filament')[0]);
      await screen.findByText(LABEL);
    };

    it('renders the toggle on when the server has never set it', async () => {
      const user = userEvent.setup();
      render(<SettingsPage />);
      await switchToFilamentTab(user);

      // mockSettings omits the key entirely — the `?? true` fallback is what
      // keeps an old server's response from rendering an indeterminate box.
      expect(toggleFor(LABEL)).toBeChecked();
    });

    it('sends prefer_lowest_filament: false once switched off', async () => {
      let receivedBody: Record<string, unknown> | null = null;
      server.use(
        // The page saves with PUT; the shared beforeEach only mocks PATCH, so
        // without this handler the save would fall through to the catch-all.
        http.put('/api/v1/settings/', async ({ request }) => {
          receivedBody = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json({ ...mockSettings, ...receivedBody });
        })
      );

      const user = userEvent.setup();
      render(<SettingsPage />);
      await switchToFilamentTab(user);

      const toggle = toggleFor(LABEL);
      await user.click(toggle);

      // Assert the local flip first: if the click were swallowed (missing
      // settings:update, say) the PUT wait below would time out with nothing
      // to say about why.
      await waitFor(() => expect(toggleFor(LABEL)).not.toBeChecked());

      // The save is debounced by 500ms.
      await waitFor(
        () => {
          expect(receivedBody).not.toBeNull();
          expect(receivedBody!.prefer_lowest_filament).toBe(false);
        },
        { timeout: 5000 }
      );
    });
  });

  describe('Auto-queue routing — auto_queue_rebalance_models', () => {
    const LABEL = 'Rebalance across printer models';

    const switchToPrintingTab = async (user: ReturnType<typeof userEvent.setup>) => {
      render(<SettingsPage />);
      await user.click(await screen.findByText('Printing'));
      await screen.findByText(LABEL);
    };

    it('renders off when the server has never set it', async () => {
      const user = userEvent.setup();
      await switchToPrintingTab(user);
      expect(toggleFor(LABEL)).not.toBeChecked();
    });

    it('sends auto_queue_rebalance_models: true once switched on', async () => {
      let receivedBody: Record<string, unknown> | null = null;
      server.use(
        http.put('/api/v1/settings/', async ({ request }) => {
          receivedBody = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json({ ...mockSettings, ...receivedBody });
        }),
      );
      const user = userEvent.setup();
      await switchToPrintingTab(user);

      await user.click(toggleFor(LABEL));
      expect(toggleFor(LABEL)).toBeChecked();
      await waitFor(() => expect(receivedBody).not.toBeNull());
      expect(receivedBody).toMatchObject({ auto_queue_rebalance_models: true });
    });
  });

});
