# Web Context — the self-evolving web-data moat

Olympus's native answer to a hosted "web data API" (Firecrawl-class), built as a
zero-dependency, security-gated, and — crucially — **self-evolving** subsystem.
The design contract is ADR 0010 (absorption) and ADR 0009 (self-evolving moat).
This page is the operator's map of what it does and how it gets stronger over
time.

## The capability surface

| Module | What it is |
|---|---|
| `olympus/webctx.py` | The library: `scrape` (many formats), `map_urls`, `crawl`, `batch_scrape`, `extract` (verified), `generate_llmstxt`, `diff` (git + json modes), `parse_document` (PDF/DOCX), `to_markdown`/`parse_page`. |
| `olympus/webmonitor.py` | Opt-in scheduled change-monitoring (text + json modes), off the replay hot path. |
| `olympus/domainlore.py` | The compounding per-domain knowledge corpus. |
| `olympus/webreflect.py` | The discovery routine — turns the corpus into feature proposals. |

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

Discoveries are saved as lessons, so they ride the existing **federation** seam:
a `trusted` peer's lessons are signed, scrubbed of secrets/PII, and *staged for
the operator's gate* — never auto-committed. Web knowledge thus compounds across
a fleet without widening the trust surface. (Sharing the raw per-domain lore
itself — sitemaps, biases — is a deliberate future step behind the same gate.)

## Operator view

```
olympus webknowledge     # corpus report, most-visited domains, live discoveries
olympus moat             # the self-evolution board (web_context row + tuned knob)
olympus evolve           # the full self-tuner health/params
```
