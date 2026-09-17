# Live quality authorization: PR #315 correction

Implementation prepared; native platform validation and protected delivery remain
required. This is a bounded M19 prerequisite and does not close M19, M03 delivery,
or the master hardening objective.

## Observed incident and containment

PR #315 head `9b8ca2dad5c0d60238b68ba9e1114fb8b4a94318` changed `olympus/evals.py`.
That path triggered the pre-existing automatic Answer-quality workflow. The
preflight missed this additional live job beyond the standard 24 checks.
Run `35128691041`, job `104903995324`, used the available Moonshot credential to
query `/models`, selected `kimi-k3`, and entered the benchmark at approximately
2026-09-16T17:31:27Z. A normal cancellation was requested; GitHub subsequently
reported `completed/cancelled` at 17:41:32Z. The job log records cancellation
and orphan Python termination. There are no completed scores or usage totals in
that log. Cancellation does not establish zero inference calls or zero spend.
The operator's provider usage/billing receipt for that interval remains missing.
Old logs, receipts, source, branches and packages must remain preserved.

## Finite acceptance contract

| Surface | Required behavior |
| --- | --- |
| Automatic `quality-gate` | Runs owned authorization/comparison fixtures; no provider secrets, discovery or live benchmark. A green check proves these contracts, not measured answer quality. Standard 24 checks remain required, plus this additional contract check. |
| Manual `live-quality-gate.yml` | Dispatch only; explicit allow flag, exact reviewed main commit, provider, model and reason; protected main; `OLYMPUS_LIVE_QUALITY_ENABLED=1`; `olympus-live-quality` environment. Authorization/source preflight precedes any credential-bearing step. No automatic baseline update. |
| Both CLI entrypoints | Require `--allow-live --expected-commit --provider --model --authorization-reason` before credential access, discovery, evaluation or baseline publication. Validate the clean tracked checkout and exact commit. CI additionally validates dispatch event/input identity, protected main, workflow identity and enablement. |
| Provider resolver | No ambient key priority or automatic model fallback. Optional inventory verification checks only the specified model after authorization; unavailable inventory fails closed. No credential in stdout or `GITHUB_ENV`. |
| Actual benchmark and retry | Explicit settings reach answer and judge calls; one selected model and credential, no pool failover or ambient judge override. A nested scope cannot widen the selected model. Outside this measurement context existing routing behavior is retained. Same-model judging is not independent factual verification. |
| Gate result | Missing authorization/credential/comparable model+endpoint baseline is exit 3, unavailable; benchmark exception is exit 2; regression/missing coverage is exit 1; measured pass or explicit baseline update is exit 0. Preserve pure comparison and confirmation tests. |
| Evidence and delivery | Owned adversarial tests plus native Windows/POSIX full suites; review skips; pinned updated source; all 24 standard and the additional automatic quality check at the new head; protected squash merge, actual parent/tree/diff verification, post-merge checks and fast-forward sync. |

## Operator route, only after separate authorization

No activation, environment change, provider call, baseline refresh or dispatch is
authorized by this correction. Before an actual manual CI run, configure and
verify required reviewers and protected-main restrictions for the named GitHub
environment, approve the provider/account/model and budget, review the exact
commit and baseline, and explicitly enable the repository variable. The source
cannot prove that GitHub environment reviewers were configured; environment
protection and provider budget evidence remain external gates. Never infer
consent from an API key. A skipped manual job is not a measurement.

An authorized local invocation uses the same explicit arguments. CI dispatch
supplies them through environment variables and quoted shell arguments, never
through expression interpolation into shell source. The resolver supports
`--check-authorization` without credential access and `--verify-model` for an
explicitly approved compatible provider's inventory. Running the quality script
with `--update-baseline` is an additional explicit operator action; it must not
be used to erase an unexplained regression. Review and preserve prior baseline
evidence before approving such an update.

Authorization is an operator/runner boundary, not a sandbox against someone who
can replace source, Git metadata, credentials or the process environment. A
single-model benchmark is intentionally narrower than ordinary pool routing.
The timeout bounds job duration; it is not a proven monetary cap. Pricing and
budget accounting remain M08 requirements. Existing general benchmark grade
coercion, baseline publication durability and genuine qualification semantics
remain subject to M19/M10/M15/M18 review; this patch does not certify them.

## Remaining live-path audit (M19)

| Confirmed separate surface | Remaining requirement |
| --- | --- |
| `replay-gate.yml` / `tier1_exit_check.py` | Scheduled/manual provider calls still need equivalent explicit activation and call-chain authorization, retained replay functionality and owned adversarial proof. |
| `search-live.yml` | Scheduled live search/provider smoke needs approved collection/source scope and opt-in; retain owned adapter checks and explicit authorized live route. |
| `auto-upgrade.yml` | Issue-triggered provider/code automation needs explicit autonomy authorization and protection through every consumer; trusted issue author alone is insufficient. |
| General evaluation/CLI/backend consumers | Inventory every caller, budget and external evidence route. The two quality scripts' guard is not a universal backend authorization policy. |

Do not run these separate live routes as part of this correction. Deployment,
publishing, collection, learned routing, autonomy, broker activity and live
verifier fan-out remain restricted. Real calibration checkpoints, intended-host
readiness and comparative measurements remain open; tests cannot manufacture
that evidence.
