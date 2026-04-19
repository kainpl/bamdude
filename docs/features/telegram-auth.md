---
title: Multi-Chat Auth
description: Per-chat authorization with roles, permissions, and registration
---

# Multi-Chat Authorization

BamDude's Telegram bot supports multiple chats with independent authorization, role-based permissions, and registration control.

---

## :material-shield-lock: Overview

Each Telegram chat (private or group) is linked to a BamDude group (role). This provides:

- **Per-chat permissions** -- Different chats can have different access levels
- **Notification routing** -- Each chat receives only the events it's subscribed to
- **Quiet hours** -- Per-chat quiet hour configuration
- **Registration control** -- Control how new chats are authorized

---

## :material-account-group: Chat Management

Manage Telegram chats in the web UI at **Settings > Notifications > Telegram Chats**.

### Chat Properties

| Property | Description |
|----------|-------------|
| **Chat ID** | Telegram chat identifier (auto-detected) |
| **Chat Name** | Display name |
| **Group** | BamDude group (role) assignment |
| **Notification Events** | Which events this chat receives |
| **Quiet Hours** | Time window to suppress notifications |
| **Daily Digest** | Receive a summary instead of individual messages |
| **Active** | Enable/disable the chat |

---

## :material-key: Permission Gating

The bot middleware checks permissions before executing commands:

1. User sends a command or taps an inline button
2. Middleware looks up the chat's group
3. Checks if the group has the required permission
4. Allows or denies the action

### Permission Examples

| Action | Required Permission |
|--------|:------------------:|
| View printers | `printers:read` |
| Control printers | `printers:control` |
| Start prints | `queue:create` |
| View queue | `queue:read` |
| Manage maintenance | `settings:update` |

---

## :material-account-plus: Registration Modes

Control how new Telegram chats are authorized:

| Mode | Behavior |
|------|----------|
| **Open** | Any chat that sends `/start` is automatically registered with a default group |
| **Approval** | New chats are registered but inactive until approved in the web UI |
| **Closed** | Only chats added manually in the web UI can interact with the bot |

Configure the registration mode in **Settings > Notifications > Telegram**.

---

## :material-chat-processing: Auth Middleware

The auth middleware (`telegram_handlers/auth_middleware.py`) runs on every message and callback:

1. Checks if the chat exists in the `TelegramChat` model
2. If new: auto-registers based on registration mode
3. Loads the chat's group and permissions
4. Injects permissions into the handler context
5. Handler checks required permissions before acting

---

## :material-bell-ring: Per-Chat Notifications

Each chat can subscribe to different notification events:

- **Print events** -- complete, failed, progress
- **Queue events** -- added, started, waiting, complete
- **System events** -- printer offline, HMS errors, maintenance due

Configure in the web UI per chat.

---

## :material-lightbulb: Tips

!!! tip "Admin Chat"
    Set up a private chat with the Administrators group for full control, and a group chat with Viewers group for monitoring.

!!! tip "Quiet Hours"
    Set quiet hours per chat to avoid notifications during off-hours. The daily digest will summarize missed events.

!!! tip "Revoking Access"
    To revoke a chat's access, either delete it from the web UI or set it to inactive. The bot will stop responding to that chat.
