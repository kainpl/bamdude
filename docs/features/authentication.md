---
title: Authentication
description: Optional user authentication with role-based access control
---

# Authentication

BamDude includes an optional authentication system with user accounts and group-based permissions (80+ granular permissions).

---

## :material-lock: Overview

When enabled, authentication provides:

- **User Accounts** -- Multiple users with unique credentials
- **Group-Based Permissions** -- 80+ granular permissions organized by feature
- **Customizable Groups** -- Create custom groups or use defaults
- **JWT Authentication** -- Secure token-based auth
- **User Activity Tracking** -- See who uploaded, queued, and printed

---

## :material-account-group: Default Groups

| Group | Description | Permissions |
|-------|-------------|-------------|
| **Administrators** | Full access | All permissions |
| **Operators** | Control printers and manage content | Printer control, queue, archives |
| **Viewers** | Read-only access | View printers, archives, queue |

---

## :material-key: Permission Categories

Permissions follow a `resource:action` pattern:

- **Printers** -- read, create, update, delete, control, files, clear_plate
- **Archives** -- read, create, update_own/all, delete_own/all, reprint_own/all
- **Queue** -- read, create, update_own/all, delete_own/all, reorder
- **Library** -- read, upload, update_own/all, delete_own/all
- **Settings** -- read, update, backup, restore
- **Users/Groups** -- read, create, update, delete

---

## :material-toggle-switch: Enabling Auth

1. Go to **Settings** > **Authentication**
2. Enable **Require Authentication**
3. Create an admin user
4. All subsequent requests require login

!!! info "Opt-In"
    Auth is completely optional. When disabled, all endpoints are accessible without login. Endpoints use `RequirePermissionIfAuthEnabled` which is a no-op when auth is off.

---

## :material-lightbulb: Tips

!!! tip "Start Without Auth"
    Set up your printers and test BamDude before enabling authentication. You can enable it at any time.

!!! tip "Ownership Permissions"
    Use `*_own` permissions for users who should only modify their own uploads and queue items.

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
