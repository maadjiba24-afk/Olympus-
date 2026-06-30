# Deploying Olympus to caelarion.com

This puts Olympus on a small always-on server with **automatic HTTPS** (Caddy +
Let's Encrypt). No certificates to manage, no domain config beyond one DNS
record. ~15 minutes start to finish.

## What you need
- The domain **caelarion.com** (done ✓) — and access to its DNS settings at your
  registrar.
- A small Linux server (Ubuntu 24.04, ≥1 GB RAM). A $6/mo DigitalOcean droplet or
  a ~€4/mo Hetzner server is plenty. Note its **public IP address**.
- Your `ANTHROPIC_API_KEY`.

## Step 1 — point the domain at the server
In your registrar's DNS settings for caelarion.com, add two records:

| Type | Name | Value |
|------|------|-------|
| A    | `@`  | your server's IP |
| A    | `www`| your server's IP |

DNS can take a few minutes (sometimes up to an hour) to propagate. You can check
with `dig caelarion.com +short` — it should return your server IP.

## Step 2 — install Docker on the server
SSH in (`ssh root@YOUR_SERVER_IP`), then:

```bash
curl -fsSL https://get.docker.com | sh
```

## Step 3 — get Olympus and configure it
```bash
git clone https://github.com/maadjiba24-afk/Olympus-.git
cd Olympus-/deploy
cp .env.example .env
# generate two strong secrets:
echo "OLYMPUS_ACCESS_TOKEN=$(openssl rand -hex 32)"
echo "OLYMPUS_SECRET_KEY=$(openssl rand -hex 32)"
nano .env     # paste your ANTHROPIC_API_KEY and the two secrets above
```

## Step 4 — launch
```bash
docker compose up -d --build
```

Caddy will automatically fetch the HTTPS certificate for caelarion.com on first
request (give it ~30 seconds). Then open **https://caelarion.com**.

Because `OLYMPUS_REQUIRE_LOGIN=1` is set, you'll see a login screen — click
**Create account** to make the first account. Everyone else does the same; each
person's memory and actions are private to their account.

This also starts the **always-on learning loop** (the `heartbeat` service): even
while you're asleep or away, Olympus scans the world, learns from queued videos,
distills the day's experience into new skills, and runs its weekly self-audit —
sharing the same memory as the chat app. It spends tokens on your key in the
background, **bounded by `OLYMPUS_DAILY_BUDGET`** (default $20/day; cycles skip
once the cap is hit). To turn it off, comment out the `heartbeat` service in
`docker-compose.yml` (or `docker compose stop heartbeat`) — note that setting
the budget to `0` means *unlimited*, not off. Watch it with
`docker compose logs -f heartbeat`.

## Step 5 — verify it's healthy
```bash
curl https://caelarion.com/healthz        # -> {"status":"ok",...}
```
Behind your access token, `https://caelarion.com/api/metrics` shows uptime,
traffic, errors, and today's spend. From the server, `docker compose logs -f
olympus` tails the app, and `docker compose exec olympus python -m olympus
status` prints a health summary.

## Updating later
```bash
cd Olympus-/deploy && git pull && docker compose up -d --build
```

## Optional
- **Self-learning loop:** runs **by default** (the `heartbeat` service above).
  Comment it out in `docker-compose.yml` if you want a chat-only instance that
  learns solely during conversations.
- **Google (item #3) / WhatsApp (item #4):** the redirect/webhook URLs are
  already `https://caelarion.com/...`; fill the matching env vars in `.env` and
  `docker compose up -d` again.

## WhatsApp (item #4)

The WhatsApp gateway is included but **dormant** (a Compose profile), and Caddy
already routes `https://caelarion.com/webhook` to it. To turn it on:

1. Create a Meta app with the WhatsApp product; get the **phone number ID** and
   an **access token**, and choose a **verify token** (any secret string).
2. Add to `.env`:
   ```
   WHATSAPP_ACCESS_TOKEN=...
   WHATSAPP_PHONE_NUMBER_ID=...
   WHATSAPP_VERIFY_TOKEN=your-secret
   WHATSAPP_ALLOWED_NUMBERS=15551234567   # lock to your number while testing
   ```
3. Start the gateway:
   ```
   docker compose --profile whatsapp up -d --build
   ```
4. In the Meta console, set the webhook to `https://caelarion.com/webhook` with
   the same verify token, and subscribe to the **messages** field.

## Notes
- Only Caddy is exposed to the internet (ports 80/443); the app listens on the
  private compose network. The access token is the entry gate, accounts are
  per-user identity, and the memory/accounts/vault all persist in the
  `olympus-memory` Docker volume across restarts and updates.
