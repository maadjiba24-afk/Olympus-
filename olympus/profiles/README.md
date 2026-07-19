# Built-in operator site profiles

Files here are **curated, declarative site profiles** shipped with Olympus and
loaded by `browser.builtin_profiles()`. They let the Hermes operator log into
and act on common sites without the user having to author CSS selectors.

- One `*.json` file per site (or a JSON array of profiles).
- Loaded read-only and tagged `source: "builtin"`.
- A user's own recorded profile for the same domain **always overrides** the
  built-in (`browser._merged_profiles`), so seeds are a starting point, never a
  lock-in.
- Malformed files are skipped, never fatal.

## Format

```json
{
  "domain": "example.com",
  "login_url": "https://example.com/signin",
  "username_selector": "#email",
  "password_selector": "#password",
  "submit_selector": "button[type=submit]",
  "success_selector": "#account-menu",
  "author": "olympus",
  "templates": {
    "do-the-thing": {
      "risk": "notable",
      "steps": [
        {"op": "click",  "selector": "#start"},
        {"op": "fill",   "selector": "#qty", "value": "$quantity"},
        {"op": "click",  "selector": "#confirm"},
        {"op": "assert", "selector": "#done"}
      ],
      "success_selector": "#done"
    }
  }
}
```

- **Selectors only.** The operator never interprets page prose as instructions —
  a template is a fixed list of `click` / `fill` / `wait` / `assert` ops.
- **`$name`** in a `fill` value pulls from the template's runtime params.
- **`risk`** is one of `notable` (auto-runs only at autonomy L4), `irreversible`
  (always needs approval), or `financial_legal` (approval + tightest daily cap).
- **`success_selector`** must appear after the run or the operate is marked
  FAILED — the operator fails closed, it never assumes success.

## Contributing a profile

Selectors change often and can't be unit-verified here, so a new profile should
be tested live against the real site (operator enabled, domain authorized)
before it's trusted. Prefer `risk: "irreversible"` for anything that spends
money, sends a message, or changes account state — that guarantees a human
approves each run. Keep templates minimal and idempotent where possible.
