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

### Upgrading ACROSS the non-root change (one time, only if you deployed before it)

The image now runs as `olympus` (**UID/GID 10001**) instead of root. Docker seeds
a **new** named volume with the ownership of the image's mount point, so a fresh
deployment gets `olympus-memory` owned by `10001:10001` and needs nothing here.

An **existing** volume keeps the ownership it was created with. If this instance
ever ran the root image, `olympus-memory` is root-owned and the new non-root
process cannot write to it — the container starts, reports healthy for a moment,
and then silently persists nothing. Fix it once, before `up`:

```bash
docker compose down
docker run --rm -v olympus-memory:/m alpine chown -R 10001:10001 /m
docker compose up -d --build
```

Verify:

```bash
docker compose exec olympus sh -c 'id; touch /app/memory/.wtest && echo WRITABLE && rm /app/memory/.wtest'
```

**Untested against a real pre-existing volume**, because Olympus has never been
deployed and no such volume exists to test against. The command is written from
the documented behaviour of volume ownership, not from a reproduction — treat it
as the starting point for the first real upgrade, and check `id` and the write
probe above rather than assuming it worked.

### Upgrading ACROSS the gallery scoping change (W2-1b)

The gallery is now **per principal**. Images generated after this change land in
`<workspace>/gallery/<principal>/` and are listable, readable, deletable and
editable only by their owner. Before it, every image sat flat in the shared
workspace and `list/read/delete` unioned across sandbox roots, so any account
could see and delete any other account's images.

**Images generated before the upgrade have no owner**, and what happens to them
depends deliberately on whether you run accounts:

| `OLYMPUS_REQUIRE_LOGIN` | What you see |
|---|---|
| unset (single user) | the old flat images are still shown — **nothing is lost by upgrading** |
| `1` (accounts on) | they are shown to **nobody**, because they belong to nobody |

The single-user case is the one that would bite a real person, so it is the one
that keeps working with no action. On a multi-user instance the images are not
deleted, just not surfaced; claim them into one principal's gallery deliberately:

```bash
docker compose exec olympus python -c   "from olympus import gallery, memory; memory.set_user('u:1'); print(gallery.claim_legacy(), 'claimed')"
```

Use the namespace of the account that should own them (`u:<account_id>`).
Verified by test in both directions: legacy images remain reachable with accounts
off, and are invisible with accounts on until claimed.

**What this does NOT scope.** The gallery *surface* is per principal; the sandbox
file tools still share one workspace by design (`sandbox.workdir`, ADR 0005). A
principal who can run file tools can still reach another's gallery directory
through those tools. Closing that is a workspace-model change, not a gallery one.

## Verified bring-up (W1-4)

Until this, `docker-compose.staging.yml` said in its own header that it had "NOT
[been] VERIFIED BY EXECUTION … never been brought up". This section is the record
of actually bringing the stack up.

**Read the scope line first.** Everything below was run on **Docker Desktop
29.2.1 / WSL2 / Windows, cgroup v2**. That is not a droplet, and this repo has
never been deployed to one. A green local stack is evidence about the compose
files and the app; it is **not** evidence about production.

### Verified locally

| # | What | Result |
|---|---|---|
| 1 | Proxy routing through Caddy — `/healthz`, `/readyz`, web UI | 200 / 200 / 200 (52,653 bytes) |
| 2 | Production boot guard refuses `OLYMPUS_DAILY_BUDGET=0` | refuses, exit 1 |
| 3 | Production boot guard refuses an unwritable memory dir | refuses, exit 1 |
| 4 | Production boot with budget UNSET is bounded, not unlimited | boots, ceiling $10/day |
| 5 | Graceful shutdown of the web service | 0.92 s, exit 0, drain logged |
| 6 | Memory survives `docker compose down` + `up` | persisted |

### NOT verified — do not cite this section as covering these

- **Let's Encrypt / TLS for caelarion.com.** Not exercised at all; see below.
- **Anything on a real server.** No droplet, no public DNS, no firewall, no
  systemd, no reboot, no disk pressure, no concurrent users.
- **The `whatsapp` profile.** Never started (needs real `WHATSAPP_*` credentials).
- **The pre-existing-root-volume upgrade** documented above. Still untested
  against a volume created by the old root image, because none exists.
- **Sustained operation.** The longest run here was minutes.

---

### 1. The proxy path, and why TLS had to be worked around

Everything before W1-4 talked to port 8484 directly. Nothing had ever reached the
app *through Caddy*. With the production `Caddyfile` unchanged, it still cannot:

```
$ curl -o /dev/null -D - http://localhost/healthz
HTTP/1.1 308 Permanent Redirect
Location: https://localhost/healthz

$ curl -k https://localhost/healthz
curl: (35) schannel: ... SEC_E_INTERNAL_ERROR      # no certificate exists
```

Caddy's logs say why:

```
challenge failed ... "identifier":"caelarion.com","challenge_type":"http-01",
  "detail":"46.101.143.92: Fetching http://caelarion.com/.well-known/acme-challenge/…:
            Timeout during connect (likely firewall problem)"
could not get certificate from issuer ... acme-v02.api.letsencrypt.org
could not get certificate from issuer ... acme.zerossl.com  (EAB request: DNS lookup failed)
```

Note what that does and does not prove. `caelarion.com` **does** resolve — to
`46.101.143.92` — so Let's Encrypt dutifully tried to reach *that host*, not this
laptop, and timed out because nothing is listening there. ACME cannot be satisfied
from a machine that does not hold the domain. Caddy then fell back to ZeroSSL,
which failed on its own DNS lookup from inside the container.

So with the production proxy the app is unreachable locally: `:80` redirects to an
HTTPS listener that has no certificate.

**The override.** `Caddyfile.local` + `docker-compose.local.yml` serve the same
routes over plain HTTP with `auto_https off`:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
```

```
GET http://localhost/healthz   -> HTTP 200  (40 bytes, 0.008s)
GET http://localhost/readyz    -> HTTP 200  (150 bytes, 0.009s)
GET http://localhost/          -> HTTP 200  (52653 bytes, 0.008s)

{"status": "ok", "uptime_seconds": 18.6}
{"status": "ready", "env": "dev", "version": "0.27.3", "memory_dir_writable": true, …}
```

**The override exists so the ROUTING half can be tested with TLS removed. It is
not evidence that the production TLS path works, and it never will be.** The
production `Caddyfile` is untouched; the only way to verify Let's Encrypt is to
run it on the host `caelarion.com` actually points at. One useful side result:
`memory_dir_writable: true` in `/readyz` shows the non-root container really can
write the named volume, end to end through the proxy.

### 2. The fail-closed boot guard — a security control nobody had watched fire

Three deliberately-broken production configs, each with `OLYMPUS_ENV=production`
and a signing seed (production refuses a forgeable seed first, which would have
masked the checks under test).

**`OLYMPUS_DAILY_BUDGET=0` — refuses.** `0` means UNLIMITED, not off:

```
[production] production configuration is incomplete — refusing to start:
  - OLYMPUS_DAILY_BUDGET='0' means UNLIMITED, not off. Production requires a positive cap.
exit code: 1
```

**Unwritable memory dir — refuses:**

```
[production] production configuration is incomplete — refusing to start:
  - OLYMPUS_MEMORY_DIR (/app/memory) is not writable by this process: journals,
    ledgers and accounts would all fail to persist. Check volume ownership and permissions.
exit code: 1
```

**Budget UNSET — boots, and that is correct.** A refusal was expected here; it
does not refuse, deliberately, and `config.production_problems()` says so:

> `OLYMPUS_DAILY_BUDGET` need not be set — unset now resolves to the bounded
> `DEFAULT_DAILY_BUDGET`. An explicitly UNLIMITED value is still refused, which is
> the actual hazard.

Verified the fallback is genuinely bounded rather than a silent unlimited:

```
config.DEFAULT_DAILY_BUDGET = 10.0
usage.daily_budget()        = 10.0
production_problems()       = []
```

The guard fires on 2 of the 3 cases, and the third is a documented relaxation of
the staging rules rather than a hole. **No case booted that should have refused.**

### 3. Graceful shutdown — a defect, found and fixed

The web service was fine. The heartbeat was being **killed on every stop**:

```
  olympus      0.92s  exit=0    CLEAN — "… received SIGTERM, finishing in-flight requests"
  heartbeat    1.98s  exit=137  SIGKILLED after grace
  caddy        1.22s  exit=0    CLEAN
```

Root cause, confirmed by isolating it: `heartbeat.run_forever()` catches only
`KeyboardInterrupt`, i.e. SIGINT — and the process is **PID 1** in the container,
where Linux ignores any signal that has no explicitly-installed handler. SIGTERM
was dropped on the floor and Docker SIGKILLed the loop after the grace period. The
web service escapes this only because `web.py::_install_shutdown` installs a
SIGTERM handler explicitly.

```
  SIGTERM: 1.66s exit=137          # ignored, then killed
  SIGINT:  0.77s exit=0            # only because cli.py catches KeyboardInterrupt
```

**What a kill actually costs.** Not corruption: `SIGKILL` ends the process, but
the bytes it already wrote are in the kernel page cache and get flushed anyway,
and the tmp-file + `os.replace` publish is atomic against process death by
construction (`olympus/atomicio.py` — `fsync` there addresses power loss and
kernel panic, which is a different failure). The real costs are that an in-flight
model call is abandoned *after it has already been paid for*, the cadence
timestamp for the running job is never written so the same work re-runs on the
next boot, and a multi-step maintenance sweep stops wherever it happened to be.

**Fixed in the application, not just in compose.** `stop_signal: SIGINT` in the
compose file also made the symptom go away, but only there — systemd, Kubernetes
and plain `docker run` all send SIGTERM and would have reproduced the identical
SIGKILL. `heartbeat._install_shutdown` now installs a SIGTERM/SIGINT handler the
way `web.py::_install_shutdown` always has, with the same best-effort guard
(handlers install only on the main thread; failing to install is not a reason to
refuse to run). The loop waits on an `Event` rather than `time.sleep`, because
since PEP 475 a handler that returns normally makes `time.sleep` *resume* for its
remaining time — so a SIGTERM arriving mid-tick would set the flag and still be
killed waiting out the sleep.

Same measurement as the defect, so the two are directly comparable — and taken
with the **default** stop signal, no override:

```
  BEFORE (no handler):     1.66s  exit=137  SIGKILLED
  AFTER  (SIGTERM handler): 0.56s  exit=0    CLEAN
        … received SIGTERM, finishing this tick and stopping
        Heartbeat stopped.
```

`stop_signal: SIGINT` was then **removed**. Once SIGTERM is handled properly the
override is a workaround for a bug that no longer exists, and leaving it would
hide a regression: delete the handler and compose would still stop cleanly while
every other runtime went back to being killed.

A second finding while measuring: **the effective grace period here was 1 second,
not the documented 10.** A container that ignores SIGTERM died after 1.77 s, and
`.Config.StopTimeout` read `1`. The production compose declared no
`stop_grace_period` at all, so the drain inherited whatever the host chose — and
the clean web drain already took 0.92 s, uncomfortably close to that. Applied:
`stop_grace_period: 60s` on `olympus` and `whatsapp` (matching staging), `30s` on
`heartbeat`.

**`caddy` was deliberately left with no `stop_grace_period`**, so it still
inherits `StopTimeout=1`. It handles SIGTERM itself and exits cleanly in 1.22 s,
so there is nothing to fix; and unlike the other two it holds no in-flight
application state — a proxy that stops is a proxy that stops. Adding a number
there would imply a drain it does not need. The asymmetry is intentional.

After, with no `stop_signal` anywhere:

```
  /deploy-olympus-1    StopSignal= StopTimeout=60   exit=0
  /deploy-heartbeat-1  StopSignal= StopTimeout=30   exit=0
  /deploy-caddy-1      StopSignal= StopTimeout=1    exit=0
  total `docker compose stop`: 1.44s
```

No slowdown — a grace period is a ceiling, not a wait.

### 4. Full-stack teardown and restart

`docker compose down` (**without** `-v`; `-v` destroys the named volumes) then
`up` — a stronger test than the container restart W1-3 did:

```
containers remaining: 0
olympus-memory volume present: 1
… up …
loaded: [{'role': 'user', 'content': 'survives docker compose down'}]
PERSISTED through docker compose down + up
GET http://localhost/readyz -> HTTP 200
```

### 5. Everything that went wrong on the way

The errors are the point; a bring-up that reports none was not written down
honestly.

- **The production Caddyfile makes the app unreachable locally.** TLS failing was
  expected; the failure mode was not — a `308` into a certificate-less HTTPS
  listener rather than a plain error. Worked around, not fixed.
- **The first unwritable-volume test was wrong, and it looked like a critical
  finding.** A volume was created and `chown`-ed to root; the app booted anyway.
  The guard was fine — the setup was not. Docker re-seeds an **empty** named
  volume from the image, ownership included, so the chown was silently undone.
  Making the volume non-empty first reproduced it correctly. This is the same
  mechanism as the pre-existing-volume note above, seen from the other side, and
  it is worth knowing before anyone stages that upgrade.
- **The first budget test looked like a boot-guard failure.** With the budget
  unset the container ran 6m40s instead of refusing. That is correct behaviour,
  but it took reading `production_problems()` to know, and stdout buffering (no
  `PYTHONUNBUFFERED`, output piped through `head`) hid the startup banner that
  would have said so sooner. Later runs set `PYTHONUNBUFFERED=1`.
- **`bc` is absent** in this Git-Bash environment, so the first shutdown timing
  produced an empty elapsed figure. Re-timed with Python.
- **`ps` is absent** in the slim image, so "is it PID 1" had to be established
  indirectly — by observing that SIGTERM was ignored and SIGINT was not.
- An `alpine` pull was needed to manipulate volume ownership from outside the app
  image.

### Reproducing this

```bash
cd deploy
cp .env.example .env                     # then fill it in
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
curl -sS http://localhost/healthz && curl -sS http://localhost/readyz
docker compose -f docker-compose.yml -f docker-compose.local.yml down
```

Drop `-f docker-compose.local.yml` for the real TLS path — which, on anything that
is not the host `caelarion.com` resolves to, fails at ACME exactly as above.

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
  private compose network. The memory/accounts/vault all persist in the
  `olympus-memory` Docker volume across restarts and updates.

### There are TWO secrets, and they must be different

| Secret | Header | Gates | Who holds it |
|---|---|---|---|
| `OLYMPUS_ACCESS_TOKEN` | `X-Olympus-Token` | whether `/api/*` is reachable at all — the **entry gate** | **every user** of the instance |
| `OLYMPUS_OPERATOR_TOKEN` | `X-Olympus-Operator` | `/api/admin` and `/api/admin/act` — the **operator panel** | the operator only |

Accounts (`OLYMPUS_REQUIRE_LOGIN`) are per-user identity on top of the entry
gate; they are not a substitute for either token.

**Why they must differ.** The access token is handed to everyone who uses the
instance — that is what "entry gate" means. Until W2-1a the admin panel was
gated on that same token, so **every account that could log in was a full
operator**, and `/api/admin/act` is not read-only: it carries `config_set`,
`set_autonomy` on other users, approve/reject of pending actions, `schedule_add`
and `mcp_add`. That was privilege escalation by registration. Setting the two
variables to the same value recreates it exactly, so the server now **refuses to
serve the panel** in that configuration rather than appearing configured.

**`OLYMPUS_OPERATOR_TOKEN` is not optional for remote administration.** With it
unset the panel falls back to loopback-only — peer on loopback, server bound to
loopback, no reverse-proxy forwarding header — which on this deployment (behind
Caddy) means it is unreachable. It deliberately does **not** fall back to the
access token; that fallback is the hole. Generate it alongside the others:

```bash
echo "OLYMPUS_OPERATOR_TOKEN=$(openssl rand -hex 32)"
```
