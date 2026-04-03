// eslint-disable-next-line @typescript-eslint/no-explicit-any
type TFn = (...args: any[]) => any;

/**
 * Get localized permission category name.
 * Categories are English strings from backend (e.g. "Printers", "Smart Plugs").
 */
export function getPermissionCategoryName(dbName: string, t: TFn): string {
  const key = CATEGORY_KEYS[dbName];
  return key ? t(`permissions.categories.${key}`, dbName) : dbName;
}

/**
 * Get localized permission label.
 * Labels come from backend as "{Action} {Resource}" (e.g. "Read Printers", "Control Smart Plugs").
 * We split and translate action + resource separately for natural word order per language.
 */
export function getPermissionLabel(dbLabel: string, t: TFn): string {
  // Try direct mapping first (for irregular labels)
  const directKey = LABEL_OVERRIDES[dbLabel];
  if (directKey) return t(`permissions.labels.${directKey}`, dbLabel);

  // Split "Action Resource" pattern
  const firstSpace = dbLabel.indexOf(' ');
  if (firstSpace === -1) return dbLabel;

  const action = dbLabel.substring(0, firstSpace);
  const resource = dbLabel.substring(firstSpace + 1);

  const actionKey = ACTION_KEYS[action];
  const resourceKey = CATEGORY_KEYS[resource];

  if (actionKey && resourceKey) {
    return t('permissions.labelTemplate', {
      action: t(`permissions.actions.${actionKey}`, action),
      resource: t(`permissions.categories.${resourceKey}`, resource),
      defaultValue: dbLabel,
    });
  }

  return dbLabel;
}

const CATEGORY_KEYS: Record<string, string> = {
  'Printers': 'printers',
  'Archives': 'archives',
  'Queue': 'queue',
  'Library': 'library',
  'Projects': 'projects',
  'Filaments': 'filaments',
  'Inventory': 'inventory',
  'Smart Plugs': 'smartPlugs',
  'Camera': 'camera',
  'Maintenance': 'maintenance',
  'K-Profiles': 'kProfiles',
  'Notifications': 'notifications',
  'External Links': 'externalLinks',
  'Discovery': 'discovery',
  'Firmware': 'firmware',
  'Stats & History': 'statsHistory',
  'System': 'system',
  'Settings': 'settings',
  'Backup': 'backup',
  'Cloud': 'cloud',
  'API Keys': 'apiKeys',
  'User Management': 'userManagement',
  'Notification Templates': 'notificationTemplates',
};

const ACTION_KEYS: Record<string, string> = {
  'Read': 'read',
  'Create': 'create',
  'Update': 'update',
  'Delete': 'delete',
  'Control': 'control',
  'Upload': 'upload',
  'Reprint': 'reprint',
  'Reorder': 'reorder',
  'View': 'view',
  'Scan': 'scan',
  'Backup': 'backup',
  'Restore': 'restore',
  'Auth': 'auth',
};

// Labels that don't follow the simple "Action Resource" pattern
const LABEL_OVERRIDES: Record<string, string> = {
  'Files Printers': 'printerFiles',
  'Ams Rfid Printers': 'amsRfid',
  'Clear Plate Printers': 'clearPlate',
  'Update Own Archives': 'updateOwnArchives',
  'Update All Archives': 'updateAllArchives',
  'Delete Own Archives': 'deleteOwnArchives',
  'Delete All Archives': 'deleteAllArchives',
  'Reprint Own Archives': 'reprintOwnArchives',
  'Reprint All Archives': 'reprintAllArchives',
  'Update Own Queue': 'updateOwnQueue',
  'Update All Queue': 'updateAllQueue',
  'Delete Own Queue': 'deleteOwnQueue',
  'Delete All Queue': 'deleteAllQueue',
  'Update Own Library': 'updateOwnLibrary',
  'Update All Library': 'updateAllLibrary',
  'Delete Own Library': 'deleteOwnLibrary',
  'Delete All Library': 'deleteAllLibrary',
  'View Assignments Inventory': 'viewAssignments',
  'User Email Notifications': 'userEmailNotifications',
};
