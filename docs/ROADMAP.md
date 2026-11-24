# Boats N Hoes — Master Roadmap

**Current phase:** hardware validation / early prototype  
**Source:** [GitHub repository](https://github.com/turnflockoff/Boatsnhoes.meme)  
**Public landing page:** `index.html` + `contribute.html` (Netlify project `boatsnhoes-dik6`; GitHub link pending)
**Merged plan (meme coin + compute):** [`docs/plan/`](plan/README.md)

This roadmap separates implemented repository assets from proposed work. A checked box means there is evidence in the repository or a reproducible test result—not merely an idea.

## Track A — Public project foundation
- [x] Publish an honest project README with current scope and non-claims.
- [x] Add a responsive static landing page.
- [x] Publish this master roadmap.
- [x] Review page on desktop and mobile browsers (Playwright, 390/768/1440 px).
- [x] Add SVG project favicon and social-sharing metadata.
- [x] Add custom social preview artwork (`assets/og-image.png`).
- [ ] Configure production domain and HTTPS.
- [x] Add a simple privacy-respecting contact / contribution path (`/contribute`, Netlify Forms).

## Track B — Hardware validation (highest priority)
- [x] Build a read-only inventory tool that records the items below in one redacted JSON file. *(`python -m app.hw_inventory`; allowlisted commands only, no sudo or installs; warns on wrong driver, CPU-only Vulkan, missing cards and blocked render nodes. 14 tests against format samples; never run on the real rig yet.)*
- [ ] Record Ubuntu release: `lsb_release -a`.
- [ ] Record kernel: `uname -r`.
- [ ] Capture PCI IDs and bound drivers: `lspci -nnk | grep -A3 -E 'VGA|Display'`.
- [ ] Capture Vulkan devices: `vulkaninfo --summary`.
- [ ] Confirm `/dev/dri` permissions.
- [ ] Confirm both RX 470 cards enumerate independently.
- [ ] Run a minimal Vulkan compute smoke test on GPU 0. *(Run recorder built: `python -m app.gpu_run --card card0 --out run.json -- <test command>` saves exit code, output hash, expected-output match, file hashes and which card was actually busy. 56 tests with fake sysfs and small Python programs; no test has been run on a GPU.)*
- [ ] Repeat on GPU 1. *(Evidence check built: `python -m app.phase3_check --inventory inventory.json run-card0.json run-card1.json` marks each Track B item evidenced/partial/missing/problem and only counts the same test passing in two different PCI slots. 25 tests on sample files; nothing from the rig yet.)*
- [ ] Record temperature, power, VRAM use, throughput, errors, and stability. *(Sensor tool built: `python -m app.gpu_sensors` reads temperature, power, VRAM, busy %, clocks and fan from amdgpu sysfs, read-only, with min/max/avg over a watch. 47 tests against fake sysfs trees; never run on the real rig. Throughput and errors come from the test itself.)*
- [ ] Document exact working driver/runtime versions.

## Track C — Single-node inference
- [ ] Select a small, reproducible workload that fits the 4 GB card.
- [ ] Pin model files and cryptographic hashes. *(Registry and `pin` tool built; first model listed but not yet downloaded or pinned.)*
- [ ] Build or obtain a compatible llama.cpp Vulkan runtime (only after hardware validation).
- [ ] Run inference separately on each GPU. *(Worker side ready, untested on hardware: `LLM_VK_DEVICE=N` pins a worker to one card via `GGML_VK_VISIBLE_DEVICES`; prompts go to llama-cli in a private file and it gets no secrets (`llm-run-v1`). 26 tests with a fake llama-cli only.)*
- [ ] Record model, quantization, prompt/input, output, timing, and resource use.
- [ ] Define expected output and task-specific acceptance criteria.

## Track D — Coordinator and workers
- [x] Define job and receipt schemas. *(Pydantic models, versioned `bnh-job/1` / `bnh-receipt/1` since 0.5.10.)*
- [x] Implement worker identity and authenticated registration. *(One-time operator enrollment codes, per-worker bearer keys stored as sha256, owner labels that keep one person's rigs from auditing each other, revocation. Software only; `prototype/tests/test_worker_auth.py`.)*
- [x] Implement coordinator job lifecycle and deadlines (SQLite state machine).
- [x] Implement worker polling / job leasing / retries.
- [x] Add structured logs and health reporting. *(JSON log lines with no secrets or job content, a request id per request, public `/health` with 200/503, operator `/v1/operator/health` with warnings. 18 tests, software only.)*
- [x] Add idempotent receipt submission.
- [x] Test coordinator restart (jobs persist) and silent-worker recovery (lease timeout → requeue).
- [x] Require a token to queue jobs and limit requests per client. *(`X-Submitter-Token` or `X-Operator-Token` on `POST /v1/jobs`; `RATE_LIMIT_PER_MINUTE` with `429`/`Retry-After`; lock-out after `AUTH_FAILURE_LIMIT` wrong credentials; `/health` exempt. In memory, per connecting address. 20 tests, software only.)*
- [x] Fix simulated points per task type so whoever queues a job can't pick them. *(`POINTS_TABLE`, rule `points-table-v1`, off by default; when set, jobs and hidden tests get the table's number and anything else is `422`, operator included; operator health shows the rule and warns while a submitter token has no table. 25 tests, software only.)*
- [x] Let the most trusted rigs check others' work first. *(`AUDITOR_PICK=trusted-first`, rule `auditor-pick-v1`, off by default: audits and tie-breakers go to a random established rig, then a new one, and a rig with a strike only when nobody else is eligible; operator health warns while a caught rig can still audit. 11 tests, software only. Switching it on is an owner decision.)*
- [x] Pin dependencies and review their licenses. *(`deps-check-v1`, 0.5.46: `prototype/requirements.lock` pins all 31 packages with sha256 hashes (7-day cooldown); `licenses.json` records each license and why the package is there; `python -m app.deps_check lock|installed`; CI installs only the hashed files. A tampered hash stops the install. 71 tests. No vulnerability scan yet.)*
- [x] Pin CI's GitHub Actions to commits and keep its token read-only. *(`actions-pin-v1`, 0.5.47: `python -m app.workflow_check` in CI: full commit hashes with version comments, one hash per action, reviewed owners only, read-only `permissions:`, `persist-credentials: false`, no `pull_request_target`/`workflow_run`, no `${{ }}` inside scripts. 61 tests. Hashes come from this repo's own CI log; updates are manual for now.)*
- [x] Check locked packages for known security problems. *(`vuln-check-v1`, 0.5.48: `python -m app.vuln_check` asks PyPI (OSV data) about every exact locked version; same problem under several ids shown once with its fix version; reviewed exceptions expire within 90 days; "couldn't check" fails. Separate `vuln-check` workflow on lock changes, every Monday and by hand. First run caught `pytest` 8.4.2 (CVE-2025-71176), raised to 9.2.0. 45 tests.)*
- [x] Apply request limits to the real client behind a reverse proxy. *(`TRUSTED_PROXIES`, empty by default; `X-Forwarded-For` read right to left and only from listed proxies; trust-everyone refused; missing/garbled headers warned about on operator health. 20 tests, software only.)*

## Track E — Verification and accounting
- [x] Implement schema, input hash, model hash, and job-version checks. *(Signed `schema_version` and `model_sha256` on jobs; receipts must echo matching versions, input hash and model hash or get `422`. Catches mistakes, not liars. 8 tests, software only.)*
- [x] Reject duplicate and expired submissions.
- [x] Define deterministic task checks and tolerances for nondeterministic tasks. *(`exact` recompute for SHA; AI text must match after collapsing whitespace. Tolerance not yet tested against real GPU output.)*
- [x] Add independent re-run / spot-audit flow. *(Hidden re-runs by a different worker at `AUDIT_RATE`, plus hidden known-answer challenge jobs. Tested in software only.)*
- [x] Add a tie-breaker re-run when an audit disagrees. *(Third worker, excluded from both sides; majority of three. Software only.)*
- [x] Record queued, leased, accepted, rejected, expired and failed results with an audit trail, plus audit outcomes (confirmed, disputed, held).
- [x] Keep rewards simulated and off-chain.
- [x] Export an auditable reward report. *(`python -m app.rewards report` / `verify`: versioned scoring `sim-points-v1`, per-worker points, sha256 over canonical JSON. Simulated points only.)*
- [x] Document collusion, fabrication, and verification limitations. *([`VERIFICATION-THREAT-MODEL.md`](VERIFICATION-THREAT-MODEL.md))*
- [x] Assign audits to a randomly chosen eligible worker instead of first-come. *(Audits and tie-breakers go to one random active, eligible worker; re-assigned if it doesn't show up. 9 tests, software only.)*
- [x] Sign job manifests asymmetrically so workers can't forge jobs. *(Coordinator Ed25519 key; workers verify with a pinned public key. 7 tests, software only.)*
- [x] Sign closed-period reward reports so they can be checked offline. *(Same coordinator key with a separate `purpose`; `rewards verify --no-db --public-key`. Proves who issued a report, not that its numbers are right. 12 tests, software only.)*
- [x] Share redacted ledger copies so others can re-run a report. *(`python -m app.ledger_copy export`: closed, settled periods only; keys, codes, prompts, answers and real owner labels removed; `rewards verify --db <copy>` opens it read-only. Reveals which past jobs were tests. 10 tests, software only.)*
- [x] Re-check new and previously caught workers more often. *(`audit-tier-v1`: strike (failed hidden test or outvoted) → every result re-run; fewer than 5 independently checked results → 0.5; otherwise the base `AUDIT_RATE`. Operator-only; changes checks, not points. 7 tests, software only.)*
- [x] Rotate the coordinator signing key without re-pinning every worker by hand. *(`python -m app.key_rotation rotate`: handover signed by both the old and new key; workers follow a valid chain, refuse anything else; queued jobs re-signed only if their old signature is ours. Planned rotations only, not a fix for a stolen key. 10 tests, software only.)*
- [x] Stop a stolen signing key from moving workers to a key of the thief's. *(`handover-key-v1`, opt-in: offline key made with `make-handover-key`, public half announced by the coordinator and pinned by workers; handovers must also carry its signature; `rotate` refuses without it. A thief can still sign jobs until you rotate. 13 tests, software only.)*
- [x] Don't let a tie-breaker settle a dispute on the word of rigs that haven't earned trust yet. *(`TIEBREAK_DECIDES=established-only`, rule `tiebreak-trust-v1`, off by default: a majority settles only when the tie-breaker and the rig it sided with are both `established`; otherwise all three stay held, nobody gets a strike, and the case goes to the review queue. 11 tests, software only. Switching it on is an owner decision.)*
- [x] Count audits a worker was picked for but let go. *(`audit_follow_through` in the operator trust report: not picked up in time, lease ran out, result refused; health warning at `AUDIT_DROP_WARN`. Operator-only; no effect on points or audit rates. 9 tests, software only.)*
- [x] Don't count a busy rig as dropping an audit. *(`follow-through-v2`, 0.5.37: a pick that took other, older jobs during its window is `not_reached`, not a drop; jobs go out oldest first, so it never saw the audit. 3 tests, software only.)*
- [x] List held results that need a person in one place. *(`/v1/operator/review`, `python -m app.review`: stuck disputes, three-way disagreements and failed hidden tests side by side, with which answers agree; read-only, no override. 13 tests, software only.)*
- [x] Aim hidden known-answer tests at one rig or at a trust tier. *(`target_worker` / `target_tier` on `POST /v1/challenges`; only the named rig, or a random active rig in the tier, can take it; workers can't tell. Operator-only; changes checks, not points. 15 tests, software only.)*
- [x] Pause the coordinator during an incident without losing in-flight work. *(`pause-v1`: `POST /v1/operator/pause` or `python -m app.incident pause`; resume only on the server; no new leases, jobs, tests or rigs; in-flight results still accepted; interrupted audit picks get a fresh window. Plan: [`INCIDENT-RESPONSE.md`](INCIDENT-RESPONSE.md). 20 tests, software only.)*
- [x] Check that a later ledger copy didn't rewrite an earlier one. *(`python -m app.ledger_copy compare older newer`: nothing edited, deleted or back-dated before the older copy's end, same report numbers; owner labels and masked inputs compared by shape. 13 tests, software only.)*
- [x] Don't reuse a hidden test that a shared ledger copy revealed. *(`test-reuse-v1`: each export is recorded; a hidden test with the exact input of one from a copied period gets `409`; operator health warns while open tests share an input. 12 tests, software only.)*
- [x] Treat near copies of a revealed hidden test as revealed too. *(`test-reuse-v2`, 0.5.39: inputs compared by a fingerprint that ignores case, spacing, punctuation, invisible characters and, for AI jobs, the seed and other settings; the private pool skips near copies. Rewording isn't caught. 23 tests, software only.)*
- [x] Keep a private stock of hidden tests and hand them out a few at a time, each used once. *(`test-pool-v1`, `python -m app.challenge_pool`: owner-only file outside git; every test checked like the coordinator would; crossed off before sending so a crash can't send it twice; tests a ledger copy revealed are retired. 29 tests, software only; no real model answers in a pool yet.)*
- [x] Don't let the public ledger show a hidden test's outcome the moment it is submitted. *(`public-delay-v1`, 0.5.38: `GET /v1/ledger` counts every result as `pending` and hides its confidence for `PUBLIC_SETTLE_DELAY_SECONDS`, default 1 hour, whatever kind of job it was; operator token sees it live; `0` = old live view with a health warning. 11 tests, software only.)*
- [x] Don't let the public ledger pair a re-check with the job it re-checked. *(`public-no-hashes-v1`, 0.5.40: public entries no longer show the answer's hash, which was identical for a job and an agreeing re-check; operator view keeps it. 6 tests, software only.)*
- [x] Find rigs that agreed with a rig that was later caught. *(`vouch-check-v1`, 0.5.43: `/v1/operator/vouches`, `python -m app.vouches`; leads (caught pairs first), confirmations that rest only on a caught rig, and a health warning for leads that never passed a hidden test. Read-only, operator-only, no prompts or answers. 12 tests, software only.)*
- [x] Aim one hidden test at each rig the vouch check lists. *(`lead-tests-v1`, 0.5.44: `python -m app.challenge_pool issue-leads [--dry-run]`; uses a task type the rig agreed on, skips rigs that already have an aimed test waiting, keeps every pool rule. Operator runs it; nothing automatic. 18 tests, software only.)*
- [x] Don't let the public job counts show that a re-check was just queued. *(`public-counts-v1`, 0.5.41: `/health` and `GET /v1/ledger` show job counts taken once per 15-minute window (`PUBLIC_COUNTS_INTERVAL_SECONDS`), for everyone; operator view stays live. 13 tests, software only.)*
- [x] Post signed reports and ledger copies in a fixed public place. *(`reports-v1`, 0.5.42: owner chose the site and GitHub; the repo's `reports/` folder is served at boatsnhoes.meme/reports/; `python -m app.publish_report add|check` only accepts signed reports that re-check against a redacted copy, 7 days after the period, with history unchanged; never replaces anything; CI re-checks the folder. 11 tests. Nothing published yet.)*
- [x] Let rigs check that a worker download is genuine before running it. *(`release-sign-v1`, 0.5.49: `python -m app.release make|sign|verify`; a release lists every git-tracked file's sha256 and only counts when at least 2 different people from a pinned signer list signed it and the files on disk match. 54 tests, software only; no signers chosen and nothing signed yet.)*

## Track F — Two-GPU proof of concept
- [ ] Run two independent workers on the two RX 470 cards.
- [ ] Demonstrate coordinator dispatch to both workers.
- [ ] Demonstrate accepted and rejected receipts.
- [ ] Test duplicate, malformed, expired, and replayed submissions. *(Rehearsal built: `python -m app.rehearsal` acts as two workers and records how the coordinator answers 28 good and bad cases, including duplicate, malformed, expired and replayed receipts. Passes in-process and against a real local coordinator; CPU SHA-256 jobs only, not yet run alongside the real workers on the two RX 470s.)*
- [ ] Compare throughput and result quality.
- [ ] Measure CPU, RAM, VRAM, thermal, and power behavior.
- [ ] Publish reproducible setup and benchmark results.

## Track G — Optional settlement research (blocked until A–F)
- [x] Build a candidate penalty rule for caught cheaters, switched off. *(`sim-points-v2-forfeit`, 0.5.36: a rig with a strike in a period forfeits that period's unverified points; confirmed points kept. 13 tests. Whether to use it is an owner decision.)*
- [ ] Publish reward allocation, emission, scoring, dispute, and cutoff policy.
- [ ] Review legal, tax, security, and operational implications.
- [ ] Specify capped test token and claim/distributor design.
- [ ] Write automated contract tests.
- [ ] Obtain independent security review.
- [ ] Test on a public testnet.
- [ ] Make a separate explicit decision before any mainnet deployment.

## Deployment checklist
- [x] Deploy root `index.html` using Netlify or GitHub Pages. *(Netlify, deploys from `main`; owner set up 2026-11-21.)*
- [ ] Verify HTTPS, canonical URL, mobile layout, and outbound links. *(Checker built, 0.5.45, `site-check-v1`: `python -m app.site_check static` checks links, files, share cards and CSP fit offline (CI runs it); `live` checks headers, http→https, www→bare domain, hidden folders and the share image on the real site. Not yet run against boatsnhoes.meme: this sandbox can't reach it; the `site-check` GitHub workflow can. Outbound links to other sites aren't fetched.)*
- [x] Add a custom domain only after DNS ownership and target are confirmed. *(boatsnhoes.meme, owner confirmed DNS 2026-11-21.)*
- [x] Check every page for common accessibility problems, offline in CI. *(`a11y-check-v1`, 0.5.50: `python -m app.a11y_check`; subpages pass, homepage findings are notes for the owner. Cross-checked with axe-core in a real browser. A pass isn't a full accessibility audit.)*
- [ ] Add deployment status to README after the site is actually live.

## Guardrails
- Do not describe the project as Ethereum mining or proof-of-useful-work consensus.
- Do not claim GPU compatibility until the actual cards pass tests.
- Do not show fake live network statistics.
- Do not promise profitability, investment returns, payouts, or token value.
- Do not deploy a token or accept funds as part of the initial prototype.
- Treat worker receipts and hashes as records, not proof of GPU execution.

## Immediate next action
On the actual Ubuntu host, run `python -m app.hw_inventory --out inventory.json` from `prototype/` (it runs the inventory commands below read-only and blanks hostname, username and UUIDs). Use those results to choose the smallest viable Vulkan inference smoke test, then run it on each card through `python -m app.gpu_run --card cardN --out gpuN-run1.json -- <command>` so the report shows which card really did the work.
