---
title: Notifications
description: Multi-provider push notifications for print events
---

# Notifications

Get notified about print events via WhatsApp, Telegram, Discord, Email, and more.

---

## :material-bell-ring: Supported Providers

| Provider | Setup | Features |
|----------|:-----:|----------|
| **ntfy** | Easy | Free, no account needed |
| **WhatsApp** | Easy | Via CallMeBot |
| **Discord** | Easy | Channel webhooks |
| **Pushover** | Easy | Professional push service |
| **Telegram** | Medium | Via Telegram Bot with actionable buttons |
| **Email** | Medium | SMTP email |
| **Home Assistant** | Easy | Persistent notifications in HA dashboard |
| **Webhook** | Flexible | Custom HTTP POST |

---

## :material-plus-circle: Adding a Provider

1. Go to **Settings** > **Notifications**
2. Click **Add Provider**
3. Select provider type and enter configuration
4. Click **Send Test** to verify
5. Configure event triggers
6. Click **Add**

---

## :material-tune: Event Triggers

| Event | Description |
|-------|-------------|
| **Print Complete** | When a job finishes successfully |
| **Print Failed** | When a print fails |
| **Print Progress** | At configurable progress milestones |
| **Printer Offline** | When connection is lost |
| **HMS Error** | When health issues occur |
| **Maintenance Due** | When maintenance is overdue |
| **Queue Events** | Job added, started, waiting, failed, complete |

---

## :material-send: Actionable Telegram Notifications

When using Telegram as a notification provider, BamDude sends actionable notifications with inline buttons:

| Event | Actions |
|-------|---------|
| **Print Complete** | Clear plate button |
| **Maintenance Due** | Mark done button |
| **Print Progress** | Pause / Stop buttons |

See [Telegram Bot Setup](telegram-bot.md) for full configuration.

!!! tip "Per-chat event routing"
    Telegram notifications are not routed to a single hard-coded chat -- they are fanned out to every authorized chat whose `telegram_chats.notification_events` setting includes the firing event. So one chat can subscribe to "Print Complete" + "HMS Error" only, while another chat takes everything. Configure each chat's subscriptions under **Settings > Notifications > Telegram Chats**.

!!! tip "Localized templates per user"
    Notification bodies are rendered from `notification_templates_{en,uk}.json`. The template language is picked per-recipient -- Telegram uses the chat's owning user's `settings.language`, email uses the recipient user's language, etc. Adding a new template key means updating *both* `en` and `uk` JSON files (BamDude ships en + uk only).

---

## :material-clock: Quiet Hours & Digest

- **Quiet hours** -- Suppress notifications during configured time windows
- **Daily digest** -- Receive a summary instead of individual notifications

---

## :material-lightbulb: Tips

!!! tip "Start with ntfy"
    ntfy is the easiest provider to set up -- no account needed, just pick a topic name and subscribe on your phone.

!!! tip "Multiple Providers"
    You can configure multiple providers to receive notifications through different channels simultaneously.

> Originally based on [Bambuddy](https://github.com/maziggy/bambuddy) documentation.
