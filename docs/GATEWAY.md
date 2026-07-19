# The gateway daemon — Olympus, reachable everywhere

Olympus can be reached through the chat apps you already use. Historically each
channel ran as its own process (`olympus telegram`, `olympus discord`, …). The
**gateway daemon** runs every *configured* channel together in one long-lived
process, so a single deployment answers on all of them:

```bash
olympus gateway            # run every configured channel
olympus gateway --list     # show which channels are configured, then exit
olympus gateway --only telegram,slack   # run just a subset
olympus gateway --status   # from another shell: is the daemon healthy?
```

`--status` reads the daemon's status file (written by the running daemon) from
**any** process — so a monitor or `cron` can check per-channel health, restart
counts, and last errors without attaching to the daemon. It reports "not
running" when the heartbeat is stale or the process is gone.

Each channel runs in its own **supervised** thread: if a channel exits or
crashes (a dropped long-poll, a transient upstream error, a bad token), the
daemon **auto-restarts it with exponential backoff** instead of leaving it
silently offline — and one channel's failure never touches the others. After
`OLYMPUS_GATEWAY_MAX_RESTARTS` consecutive failures (default 20; `0` disables
restart, `-1` is unbounded) the supervisor gives up on that one channel and
keeps the rest running. Every start / restart / give-up is logged. Stop the
daemon with Ctrl-C.

## Channels and what enables each

A channel is "configured" (and auto-started) when its env vars are present:

| Channel | Enable with | Kind | Port |
|---|---|---|---|
| Telegram | `TELEGRAM_BOT_TOKEN` | long-poll | — |
| Signal | `SIGNAL_CLI_REST_URL` | long-poll | — |
| Email | `GMAIL_ACCESS_TOKEN` / `GMAIL_REFRESH_TOKEN` | poll | — |
| Discord | `DISCORD_PUBLIC_KEY` (+ bot token) | HTTP | 8486 |
| Slack | `SLACK_SIGNING_SECRET` (+ `SLACK_BOT_TOKEN`) | HTTP | 8487 |
| WhatsApp | `WHATSAPP_VERIFY_TOKEN` (+ `WHATSAPP_*`) | HTTP | 8485 |
| Webhook | `OLYMPUS_WEBHOOK_SECRET` | HTTP | 8488 |

The HTTP channels each bind a distinct port; put them behind your reverse proxy
/ tunnel. The polling channels need no inbound port.

## Security note — channels are untrusted by default

Anyone who can message a channel is untrusted. Gate who may talk to Olympus with
pairing:

```bash
olympus pair telegram        # mint a one-time code; the user sends /pair <code>
```

Unpaired senders get a limited surface. Proactive pushes (reminders, agent
notifications) go out via `gateway.notify_all` to the configured notify
channels.

## Running it as a service

Point your process manager (systemd, Docker, a supervisor) at `olympus gateway`.
It stays in the foreground and exits cleanly on SIGINT/Ctrl-C, so a standard
`Restart=always` unit keeps Olympus reachable across reboots.
