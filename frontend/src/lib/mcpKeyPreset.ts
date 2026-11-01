/** Deliberate diagnostic access: no write, device or cloud-token grants. */
export const mcpKeyPreset = {
  can_queue: false,
  can_control_printer: false,
  can_read_status: true,
  can_access_cloud: false,
  can_access_mcp: true,
  can_read_mcp_logs: false,
  can_manage_library: false,
  can_manage_inventory: false,
  can_manage_maintenance: false,
  can_manage_archives: false,
  can_manage_projects: false,
  can_print_labels: false,
};
