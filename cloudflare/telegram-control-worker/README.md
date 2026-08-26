# Telegram Edge Control — Cloudflare Worker

Purpose: make Telegram feel immediate without moving production authority out of the existing GitHub/Python control plane.

## What runs at the edge

- Persistent/home navigation responses.
- Search/library/statistics submenu navigation.
- Live YouTube operational statistics.
- Operations UI `Details ↔ Compact` toggle on the same Telegram message.
- Immediate callback acknowledgements so Telegram buttons never look dead.

## What does NOT run at the edge

- No production dispatch.
- No request approval mutation.
- No saved/used-topic mutation.
- No Quality/Security Gate decision.
- No provider routing/retry logic.
- No YouTube publishing.

Stateful commands are forwarded only to `telegram-editorial-control.yml`, which replays the authenticated Telegram update through the existing Python authorization and request-bound control path.

## Security boundary

The Worker requires both:

1. Telegram webhook secret header (`X-Telegram-Bot-Api-Secret-Token`).
2. Exact `TELEGRAM_ALLOWED_USER_ID` **and** `TELEGRAM_CHAT_ID` match.

Operations detail callbacks also require the callback's actual Telegram `message_id` to equal the message id encoded in the callback data before the Worker edits anything.

## Required Worker secrets

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- `TELEGRAM_ALLOWED_USER_ID`
- `TELEGRAM_CHAT_ID`
- `GITHUB_CONTROL_TOKEN`
- `YOUTUBE_API_KEY`

The GitHub token should have the minimum repository permission required to dispatch the editorial workflow. It is not used by the Worker to call the production workflow directly.

## Activation sequence

This directory is implementation-only until explicitly deployed. Merging code alone does not activate a webhook.

When activation is later authorized:

1. Deploy the Worker using a copy of `wrangler.toml.example`.
2. Add the Worker secrets above.
3. Register Telegram `setWebhook` to `https://<worker>/telegram` with the configured `secret_token`.
4. Verify `/health`.
5. Press `🏠 ابدأ`, `📈 الإحصائيات`, and an Operations `📋 التفاصيل` callback.
6. Verify one stateful search command reaches `Telegram Editorial Control` through `workflow_dispatch`.

When a Telegram webhook is detected, the GitHub scheduled control workflow automatically suppresses `getUpdates` polling so Telegram's webhook and polling modes are never used at the same time.
