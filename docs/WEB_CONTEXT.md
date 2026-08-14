# Web Context — the self-evolving web-data moat

Olympus's native answer to a hosted "web data API" (the surveyed scraper-class), built as a
zero-dependency, security-gated, and — crucially — **self-evolving** subsystem.
The design contract is ADR 0010 (absorption) and ADR 0009 (self-evolving moat).
This page is the operator's map of what it does and how it gets stronger over
time.

## The capability surface

| Module | What it is |
|---|---|
| `olympus/webctx.py` | The library: `scrape` (many formats), `map_urls`, `crawl`, `batch_scrape`, `extract` (verified), `generate_llmstxt`, `diff` (git + json modes), `parse_document` (PDF/DOCX), `to_markdown`/`parse_page`. |
| `olympus/webmonitor.py` | Opt-in scheduled change-monitoring (text + json modes), off the replay hot path. |
| `olympus/domainlore.py` | The compounding per-domain knowledge corpus — including learned **action-profiles** and the operator-gated **fleet-share** staging. |
| `olympus/webreflect.py` | The discovery routine — turns the corpus into feature proposals. |
| `olympus/webproposals.py` | Turns proposal-kind discoveries into **auto-drafted build proposals** staged for a human. |

**Tools:** `web_scrape` (formats: markdown, html, rawHtml, links, images,
metadata, branding, **jsonld**, **feeds**, summary, json, attributes; plus
`actions`, `mobile`, `location`), `web_map`, `web_crawl`/`crawl_site`,
`web_batch_scrape`, `web_extract`, `generate_llmstxt`, `parse_document`,
`web_diff`, `web_monitor_add`/`web_monitor_list`.
**CLI:** `olympus scrape | crawl | map | extract | llmstxt | monitor |
webknowledge`.

Every fetch — page, sitemap, robots, crawl hop, document, diff, monitor —
routes through the IP-pinned, egress-gated `tools._http_get`/`_http_get_bytes`.
Every model-bound byte is wrapped `security.wrap_untrusted`. In-scrape `actions`
run only through the governed browser harness; **ungoverned JS is refused**.

## How it gets stronger over time

Three loops, all inside Olympus's existing `evolve`/`moat`/heartbeat spine:

1. **Compounding knowledge (`domainlore`).** Every scrape/map folds durable
   facts about a *domain* into a corpus: where its sitemap lives, its robots
   posture, whether interaction or a mobile UA wins, page size, brand identity,
   whether it exposes JSON-LD or a feed, and how reliably it fetches. That
   knowledge is fed back as **purely-additive hints** to the next visit (a known
   sitemap is tried first; a learned mobile bias is applied automatically). The
   more Olympus scrapes, the better it scrapes — a data network effect a copier
   starts at zero. Hints never relax a safety gate; every fetch is re-gated
   regardless of lore.

2. **Self-tuning (`evolve`).** The corpus records an ok/fail outcome for every
   visit into the self-tuner. The heartbeat reviewer then moves the
   `webctx.fetch_timeout` knob from real results — lengthening it when fetches
   keep failing (bounded, so a slow-drip origin still can't hang a worker). No
   fixed guess; the timeout is earned.

3. **Discovery (`webreflect`).** An opt-in heartbeat routine reads the corpus and
   surfaces **discoveries** — capabilities the observed web is asking Olympus to
   use or grow: domains exposing JSON-LD (use the free `jsonld` format), feeds
   (watch instead of re-scrape), consistent interaction needs (a default action
   profile), or poor fetch success (try mobile/proxy). Each standing pattern is
   surfaced once, saved as a lesson, and notified — **evidence-backed proposals,
   not autonomous code-generation.** New capabilities are proposed to a human (or
   a later deliberate build), never self-written.

The `jsonld`/`feeds` formats are themselves a product of loop 3 in miniature:
the analysis showed the web publishes structured data Olympus wasn't reading, so
it now reads it deterministically, LLM-free.

### Three ways the loops now close harder

- **Learned action-profiles (loop 1 → behavior).** When an *actioned* scrape
  beats a domain's byte baseline, the corpus doesn't just count that interaction
  helped — it remembers the exact **safe action profile** (the scroll/expand/
  click steps) that won. With `OLYMPUS_WEB_AUTO_ACTIONS=1` a plain `scrape` of
  that domain replays the earned profile automatically through the governed
  harness — no manual `actions` argument. Opt-in, replay-inert, and every step
  is re-validated against the safe-verb allowlist on use; an explicit `actions=`
  (including `[]` to force none) always wins.
- **Fleet-shared raw lore (loop 1 → federation).** Beyond sharing *discoveries*
  as lessons, a `trusted` peer's **raw per-domain facts** — sitemap, feed, robots
  posture, mobile/action biases and the safe profile — now cross the federation
  wire (signed, scrubbed of secrets/PII). They land as **candidates in a staging
  area** and are folded into the live corpus only by the operator's explicit
  `webknowledge --merge`. The merge is *purely additive*: it fills gaps and
  OR-s in booleans but never overwrites local truth (visit/byte counts) and never
  relaxes a gate. A whole fleet's crawl experience compounds into each node,
  behind one human gate.
- **Auto-drafted build proposals (loop 3 → engineering queue).** A proposal-kind
  discovery (a cohort needing interaction, a cohort fetching poorly) is now
  drafted into a full **build proposal** — motivation, cited evidence, a concrete
  change in Olympus's idioms, its safety posture, and acceptance criteria —
  queued for the operator to accept / decline / mark built. Still *not* a code
  generator: it produces a reviewable engineering artifact, never self-modifying
  behavior.

## Safety & determinism

- **Opt-in autonomy.** The corpus (`OLYMPUS_WEB_LORE`, default on — pure
  learning) and reflection (`OLYMPUS_WEB_REFLECT`, default off — an autonomous
  behavior) follow the ADR-0008 contract. Both are **inert under
  `OLYMPUS_REPLAY`** and live off the council replay hot path (inside tool
  execution, whose results the harness freezes).
- **Bounded & resilient.** The corpus caps its domain count; a corrupt store is
  *quarantined*, never silently wiped; stale domains are pruned by the heartbeat
  maintenance sweep.
- **Purely additive.** Learned hints only add candidates or bias a choice an
  explicit caller argument always overrides. They can never open a fetch the
  gate would refuse.

## Fleet-wide compounding

Two seams now carry web knowledge across a fleet, both signed, scrubbed, and
operator-gated:

1. **Discoveries as lessons.** A `trusted` peer's lessons are signed, scrubbed of
   secrets/PII, and staged for the operator's gate — never auto-committed.
2. **Raw domain lore.** `olympus webknowledge --share <peer>` pushes this node's
   scrubbed per-domain facts to a `trusted` peer over `/federation/domainlore`;
   the receiver stages them and its operator runs `--merge` to fold them in
   additively. Sharing raw lore requires the *highest* trust level — a `task`
   peer can call the council but cannot receive the corpus.

Web knowledge thus compounds across a fleet without widening the trust surface:
every contributed fact is a hint, every fetch is still re-gated, and a human
approves the merge.

## Operator view

```
olympus webknowledge               # corpus report, top domains, discoveries, proposals
olympus webknowledge --proposals   # full auto-drafted build proposals
olympus webknowledge --accept ID   # record a decision (--accept / --decline / --built)
olympus webknowledge --share PEER  # push scrubbed domain lore to a trusted peer
olympus webknowledge --staged      # peer-shared lore awaiting merge
olympus webknowledge --merge       # fold staged peer lore into the corpus (additive)
olympus moat                       # the self-evolution board (web_context row + tuned knob)
olympus evolve                     # the full self-tuner health/params
```
