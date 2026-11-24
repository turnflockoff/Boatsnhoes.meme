# BOATS N HOES: Project Task List (Claude + dev work)
**Version:** 0.5.57 | **Last updated:** 2026-11-24 | Personal to-dos live in USER_TODO.md.

## Phase 0: Foundation
- [x] Buy boatsnhoes.meme
- [x] Super prompts, README, task lists
- [x] Project folder/repo for code and assets (GitHub: turnflockoff/Boatsnhoes.meme)
- [ ] Decide chain path
- [ ] Final ticker check on explorers/trackers

## Phase 1: Legal and Compliance
- [ ] Choose gambling jurisdiction and license route
- [ ] Hire crypto/gambling lawyer
- [ ] KYC/AML and age verification plan
- [ ] Securities-risk review and disclaimers
- [ ] AgentWiki content liability and moderation policy

## Phase 2: Tokenomics
- [x] Draft model page (sketch pad)
- [ ] Lock: 60s blocks, 3.5-year halving, ~100M cap, reward 27
- [ ] Fair launch vs premine final
- [ ] Tail emission decision

## Phase 3: Chain and Mining
- [x] GPU worker backend, Phase 1: durable SQLite job queue, leases and retries, signed manifests verified by workers, audit log (13 tests passing)
- [x] GPU worker Phase 2: pluggable task executors, `llm:generate` task, pinned model registry with licenses, opt-in download (32 tests)
- [ ] Pin the first model (download on the GPU box, then `model_registry pin`)
- [x] Safer llama-cli runs (`llm-run-v1`): prompt in a private file (not the command line), allow-list environment so no tokens or codes reach llama-cli, `LLM_VK_DEVICE` one card per worker, `LLM_GPU_LAYERS`/`LLM_THREADS`, fixed context 2048, closed stdin, 64 KiB output cap, process-group stop on timeout; worker skips a job it can't run instead of crashing (546 tests, fake llama-cli only)
- [x] GPU worker Phase 4 (first slice): hidden re-run audits, hidden known-answer challenges, tolerance rule, worker trust report (44 tests)
- [x] GPU worker Phase 4 (rest): tie-breaker re-run for disputes, written fraud/collusion threat model (`docs/VERIFICATION-THREAT-MODEL.md`, 51 tests)
- [x] Random (not first-come) auditor assignment (threat model #4): one random active, eligible worker per audit/tie-breaker, re-assigned if silent (82 tests)
- [x] GPU worker Phase 5: simulated reward reports (off-chain points, versioned scoring `sim-points-v1`, reproducible sha256, `verify` tool; 61 tests)
- [x] Candidate penalty rule built, off by default: `sim-points-v2-forfeit` (a rig with a strike in a period loses that period's unverified points; confirmed points kept). `REWARD_SCORING` or `report --scoring`; v1 reports unchanged (559 tests)
- [ ] Decide a penalty rule for caught cheaters (scoring v2; owner decision: switch on `sim-points-v2-forfeit`, keep v1, or ask for a different rule)
- [x] Authenticated worker registration / per-worker keys: one-time operator codes, per-worker keys stored as hashes, same-owner workers never audit each other, revocation (73 tests)
- [x] Asymmetric job signatures (threat model #9): coordinator signs with an Ed25519 private key, workers verify with a pinned public key, no shared secret (89 tests)
- [x] Versioned job/receipt schemas: signed job version and pinned model hash, receipts must match version, input hash and model hash (97 tests)
- [x] Signed reward reports (threat model #10, first step): coordinator signs closed-period reports with its Ed25519 key, anyone can check offline with the public key (109 tests)
- [x] Structured logs and health reporting: one JSON line per event, no secrets or prompts in logs, request ids, public `/health` (503 when the database fails), operator `/v1/operator/health` with plain-language warnings (127 tests)
- [x] Shareable ledger copies: redacted copy of a closed, settled period (no keys, codes, prompts, answers or real owner names) that anyone can re-run a signed report and the consistency checks against (137 tests)
- [x] Compare ledger copies across periods (threat model #10 follow-up, `bnh-ledger-compare/1`): `python -m app.ledger_copy compare older.sqlite3 newer.sqlite3` checks nothing in the older period was edited, deleted or back-dated and its report numbers are identical; read-only (467 tests)
- [x] Retire hidden tests a ledger copy revealed (threat model #11, #14, `test-reuse-v1`): every export is recorded in the live ledger; a new hidden test with the exact input of one from a period a copy covers is refused (`409`); operator health warns while open hidden tests share an input (479 tests)
- [x] Near copies of revealed hidden tests refused too (threat model #11, `test-reuse-v2`): input fingerprint ignores case, spacing, punctuation, invisible characters and, for `llm:generate`, seed/max_tokens/timeout/model; same rule in operator health and the private pool (596 tests)
- [x] Private stock of hidden tests (threat model #1, #8, #11, `test-pool-v1`): `python -m app.challenge_pool init|add|make-demo|status|issue`; owner-only file outside git, each test checked like the coordinator would, used once (crossed off before sending), revealed ones retired, aimed at a rig or tier; prints no inputs or answers (509 tests)
- [ ] Fill the hidden-test pool with real `llm:generate` answers from the pinned model (needs the rig, Phase 3)
- [x] Post signed reports and ledger copies in a fixed public place (owner chose 2026-11-23: a page on the site and on GitHub). `reports/` folder served at boatsnhoes.meme/reports/; `python -m app.publish_report add|check` (`reports-v1`): only signed reports that re-check against a redacted copy, 7-day wait, no overlaps, history unchanged, never replaced; CI re-checks the folder (626 tests). Nothing published yet
- [ ] Publish the first period once rigs run real jobs (owner step; see USER_TODO.md)
- [x] Read-only rig inventory tool for Phase 3: `python -m app.hw_inventory` (OS, kernel, PCI IDs, bound driver, Vulkan devices/Mesa version, /dev/dri access, redacted JSON; 151 tests, format samples only)
- [x] Trust-based audit rate (threat model #1, `audit-tier-v1`): new rigs and rigs caught once (failed hidden test or outvoted) are re-checked more often; operator-only, changes checks not points (158 tests)
- [x] Planned coordinator key rotation (threat model #9 follow-up): `python -m app.key_rotation rotate`, handover signed by old and new key, pinned workers follow a valid chain, queued jobs re-signed on claim, crash-safe (168 tests)
- [x] Audit follow-through (threat model #4 follow-up): per-rig count of assigned audits/tie-breakers it never picked up, let time out, or had refused; operator health warns at `AUDIT_DROP_WARN`; operator-only, changes nothing about points (177 tests)
- [x] Busy rigs aren't counted as dropping audits (`follow-through-v2`): a pick that took other, older jobs during its window counts as `not_reached`, not a drop, so the health warning only flags rigs that went silent or let a taken audit go (562 tests)
- [x] Public ledger delay (threat model #14, `public-delay-v1`): `GET /v1/ledger` counts every result as `pending` and withholds its confidence for `PUBLIC_SETTLE_DELAY_SECONDS` (default 3600) after it is accepted, so a rig can't spot a hidden test (or a re-check) by watching the public totals right after it submits; operator token sees it live (573 tests)
- [x] No result hashes in public views (threat model #14 follow-up, `public-no-hashes-v1`): `GET /v1/ledger` entries drop `result_hash`, because a re-check that agrees has the same hash as its original, which paired them for anyone and let an auditor know its job was a re-check; operator view keeps them (602 tests)
- [x] Batched public job counts (threat model #14, last open hint, `public-counts-v1`): `/health` and `GET /v1/ledger` show job counts taken once per clock-aligned `PUBLIC_COUNTS_INTERVAL_SECONDS` window (default 900), for everyone, because live `queued` rose the instant a rig's result was picked for a re-check; operator token sees them live; `0` = live with a health warning; the rehearsal's safety check now reads live counts (615 tests)
- [ ] Decide what repeated audit drops should change, if anything (owner decision; today it's a warning only)
- [x] Operator review queue (threat model #7): read-only list of held cases (stuck disputes, three-way disagreements, failed hidden tests) with answers side by side, `python -m app.review`, health warning (190 tests)
- [x] Vouch check (threat model #3, `vouch-check-v1`): once a rig is caught, list the rigs that agreed with its answers and the confirmations that rest only on a caught rig; `GET /v1/operator/vouches`, `python -m app.vouches`; health warns about unchecked leads; read-only (638 tests)
- [x] Follow up vouch-check leads (threat model #3, `lead-tests-v1`): report shows aimed tests already waiting per lead and `leads_to_test`; `python -m app.challenge_pool issue-leads [--dry-run]` aims one pool test at each, of a task type it agreed on, never a second one while one is waiting (656 tests)
- [ ] Decide what a reviewer's decision should do to held points (part of scoring v2; owner decision)
- [x] Close open job creation and limit requests (threat model #16, #17): `POST /v1/jobs` needs the operator or a job-only submitter token; per-address request limit with `429`/`Retry-After`; lock-out after repeated wrong keys, tokens or codes; addresses kept out of logs (210 tests)
- [x] Trusted-proxy setting (threat model #17 follow-up): `TRUSTED_PROXIES` so limits and lock-outs apply to the real client behind your own reverse proxy; header ignored from anyone else; trust-everyone refused (230 tests)
- [x] Aimed hidden tests (threat model #1, #8): `target_worker` or `target_tier` on `POST /v1/challenges`, so new or caught rigs are sure to get known-answer tests; operator-only, changes checks not points (245 tests)
- [x] Fixed points per task type (threat model #16 follow-up, `points-table-v1`): optional `POINTS_TABLE`; when set, every new job and hidden test gets the table's points and any other number is refused, operator included; health warning while a submitter token has no table (270 tests)
- [x] Trust-aware auditor pick (threat model #4, #6, `auditor-pick-v1`): optional `AUDITOR_PICK=trusted-first` sends audits and tie-breakers to established rigs first, then new, and a rig with a strike only when nobody else is eligible; off by default; health warning while a caught rig can still audit (281 tests)
- [ ] Decide whether to switch on `AUDITOR_PICK=trusted-first` (owner decision: it shifts who earns audit points)
- [x] Trust check before a tie-breaker settles a dispute (threat model #6, `tiebreak-trust-v1`): optional `TIEBREAK_DECIDES=established-only` lets a majority settle only when the tie-breaker and the rig it sided with are both established; otherwise all three results stay held for the review queue, no strike; off by default (520 tests)
- [ ] Decide whether to switch on `TIEBREAK_DECIDES=established-only` (owner decision: early on, while rigs are new, every dispute waits for you)
- [x] Offline handover key (threat model #9 follow-up, `handover-key-v1`): `python -m app.key_rotation make-handover-key`; coordinator announces its public half (signed by the current key), workers pin it and then only follow key handovers it also signed, so a stolen signing key file can't move them; `rotate` refuses without it once set (294 tests)
- [ ] Make the offline handover key and keep it on a USB stick (owner step, before rigs go online)
- [ ] Pick the numbers for `POINTS_TABLE` (owner decision; e.g. 1 for the demo task, 10 for an AI job)
- [x] Read-only GPU sensor tool for Phase 3: `python -m app.gpu_sensors` (temperature, power, VRAM, busy %, clocks, fan from amdgpu sysfs; one snapshot or a watch with min/max/avg; warns on heat, nearly full VRAM, wrong driver, idle card; 341 tests, fake sysfs only)
- [x] One-run recorder for Phase 3: `python -m app.gpu_run --card cardN --out run.json -- <command>` (exit code, output sha256 and tail, expected-output check, program/model file hashes, sensor readings before/during/after for every card, flags when the test ran on another card or the CPU; refuses sudo, secret-looking env, overwrites; 397 tests, fake sysfs only)
- [x] Two-worker rehearsal for Track F: `python -m app.rehearsal --local` (or `--url ... --allow-writes` against a test coordinator) checks dispatch to two workers and how duplicate, malformed, expired, replayed, wrong-worker and wrong-answer submissions are answered; 28 checks, report with no keys; CPU SHA-256 jobs only, says nothing about the GPUs (409 tests)
- [x] Phase 3 evidence check: `python -m app.phase3_check --inventory inventory.json run-card0.json run-card1.json` reads the rig's reports and marks each Track B item evidenced/partial/missing/problem; a smoke test counts only with a clean exit, a matched answer and the right card busy, and "repeat on GPU 1" needs the same test in a second PCI slot (434 tests)
- [x] Emergency pause for incidents (`pause-v1`): `POST /v1/operator/pause` or `python -m app.incident pause`; resume only from the server CLI; no new leases, jobs, hidden tests or enrollments while paused, in-flight results still accepted, interrupted audit picks get a fresh window; survives restarts (455 tests)
- [ ] GPU worker Phase 3: RX 470 hardware validation (needs the real rig; start by running the inventory tool there)
- [ ] Memory-hard, ASIC-resistant algorithm (pseudocode, reference miner)
- [ ] Per-block difficulty adjustment
- [ ] Testnet, then public testnet
- [ ] Checkpoint/finality design against 51% attacks
- [ ] Hashrate monitoring dashboard
- [ ] Code audit and bug bounty *(first pass done in Phase 4: hostile-input fuzz of the coordinator API, 0.5.52; second pass: the worker against a hostile coordinator, 0.5.53; third pass: the checking tools against a hostile ledger copy, 0.5.54; fourth pass: JSON files from strangers, 0.5.55; fifth pass: rig reports for the Phase 3 check, 0.5.56; sixth pass: planted code in a release folder, 0.5.57; an outside audit and a bounty still to come)*

## Phase 4: Security (see README SECURITY)
- [x] Stay Safe page built
- [x] `_headers` security file built
- [x] `_headers` added to repo; CSP verified locally against all pages
- [x] Build a checker for the live headers, HTTPS and www redirects, hidden folders and share image *(`site-check-v1`, 0.5.45: `python -m app.site_check live`, or one click in GitHub: Actions > site-check > Run workflow. 37 tests against a local server and a fake site; not yet run against boatsnhoes.meme, which this sandbox can't reach)*
- [ ] Verify `_headers` on the live Netlify URL (run the site-check workflow once after merging; owner click)
- [x] Pin every prototype dependency to exact, sha256-checked files and review each license *(`deps-check-v1`, 0.5.46: `requirements.lock` with a 7-day cooldown, `licenses.json`, `python -m app.deps_check lock|installed`; CI installs with `--require-hashes` and fails on a new package or version until its license is re-read; 764 tests)*
- [x] Scan locked packages for known vulnerabilities *(`vuln-check-v1`, 0.5.48: `python -m app.vuln_check` asks PyPI's OSV data about every exact locked version; `vuln-exceptions.json` for reviewed, expiring exceptions; "couldn't check" fails; separate `vuln-check` workflow on lock changes, weekly (Mondays) and by hand. First run caught pytest 8.4.2 / CVE-2025-71176, raised to 9.2.0. 45 tests, 870 total)*
- [ ] Optional extra: Dependabot alerts (repo setting, owner click; see USER_TODO.md)
- [x] Pin the CI workflow's GitHub Actions to commit hashes instead of version tags *(`actions-pin-v1`, 0.5.47: both workflows pinned, hashes taken from this repo's own CI log; `persist-credentials: false`; upload-artifact dropped; `python -m app.workflow_check` in CI also requires read-only tokens, no risky triggers and no `${{ }}` in scripts; 61 tests)*
- [ ] Keep the pinned actions up to date (optional `.github/dependabot.yml` for GitHub Actions; opens pull requests, so owner decision)
- [x] Signed miner releases with checksums (multi-signer) *(`release-sign-v1`, 0.5.49: `python -m app.release keygen|make|sign|verify`; release = every git-tracked file's sha256 plus a `SHA256SUMS`; counts only with at least 2 different signers from a pinned list, every signature valid, files on disk matching and no extra Python files; `--not-older-than` against rollbacks. 54 tests (924 total); 20 deliberate breakages all caught. Software only; no real signer keys or signed release yet)*
- [ ] Pick the release signers (at least 2 people, each with their own key) and publish the signer list (owner decision; see USER_TODO.md)
- [x] Code audit, first pass: hostile input against every API route *(`input-limits-v1`, 0.5.52: a fuzz sends ~1,000 broken and hostile requests to every route; found and fixed no body size limit (now `MAX_BODY_BYTES`, 256 KiB, `413` before reading), deeply nested JSON crashing (`400` past 32 levels), `422` errors echoing what was sent (and crashing on `1e999`), huge numbers crashing SQLite, negative worker timestamps; worker and capability names now plain ASCII. 38 tests, 1,063 total)*
- [x] Code audit, second pass: the worker against a hostile coordinator or network *(`worker-input-v1`, 0.5.53: reply size limit (8 MiB, refused before reading when declared), no compressed replies, 60 s whole-reply deadline, broken/deep/`NaN` JSON refused, coordinator text printed without terminal control codes, worker key and job id shape checks, no worker key over plain http to another machine without `--allow-plain-http`, growing wait after refused jobs; any malformed job is refused, never a crash. 103 tests, 1,166 total)*
- [x] Code audit, third pass: the checking tools against a hostile ledger copy *(`copy-open-v1`, 0.5.54: published copies are files from strangers. New `app/copy_guard.py`: exact plain-table schema (a test showed a "time-bomb" view verifying cleanly today and showing other numbers after a date), no views/triggers/virtual or extra tables, `integrity_check`, every row read once, 512 MiB file and 1 MiB value caps, read-only + immutable open (a hot journal beside a copy no longer gets replayed into it), `trusted_schema=OFF`, no ATTACH, 300 s per connection; used by `copy_info`, `rewards verify --db`, `compare`, `publish_report`; export vets its own output; one-line refusals instead of tracebacks. 28 tests, 1,194 total)*
- [x] Code audit, fourth pass: JSON files from strangers *(`json-file-v1`, 0.5.55: new `app/json_file.py` for signed reports, release manifests/signatures/signer lists and `reports/index.json`: regular files only (a named pipe hung the tools), size cap before and while reading, strict UTF-8, depth 32 (100,000 brackets crashed them), no `NaN`, no key written twice. Found and fixed: `rewards verify --no-db` printed a report's unsigned fingerprint *label*, so a report signed by anyone could show the coordinator's fingerprint (label must now match the key; computed fingerprint printed); `publish_report check` never compared the numbers `/reports/` shows with the signed report (an edited point total passed CI) and let index paths point anywhere; tracebacks on lists, `null` and wrong shapes; `^...$` regexes accepting a trailing newline (now `\Z`, guarded by a test). 59 tests, 1,253 total)*
- [x] Code audit, fifth pass: rig reports for the Phase 3 check *(`rig-report-v1`, 0.5.56: `phase3_check` reads inventories and run reports that will come from volunteers' rigs. Now read through `app/json_file.py` (4 MiB, depth, no `NaN`, no duplicate keys, regular files only: a named pipe hung it) and then every field it reads is type-checked. Found and fixed: the text "false" counted as *true* for "exited cleanly" and "card was busy", so a crashed run could pass; a 400-digit temperature, a `null` reading count and a card without a slot crashed it; names were printed with terminal codes intact. Also found: `1e400` slipped past the `NaN`/`Infinity` refusal as infinity in `json_file` and the worker; refused now. A test fails if the checker starts reading a field nobody type-checks. 675 tests, 1,928 total)*
- [x] Code audit, sixth pass: planted code in a downloaded release folder *(`release-folder-v1`, 0.5.57: `release verify --root` (the Stay Safe page's step 2) skipped `.pyc` files and `__pycache__` and didn't look behind links. Shown with real runs: a sourceless `json.pyc`, a `__pycache__/worker.cpython-313.pyc` with the source's time and size in its header, and a linked `json` folder all passed `verify` and then ran their code when the worker started. Now every ending Python can import (`.py`, `.pyc`, `.so`, `.pth`...) outside the release is a problem, as are links, unreadable folders and more than 100,000 entries; a cache passes only if it's byte-for-byte what this Python builds from the checked source (never unpacked); caches from another Python are one line per folder; fix is always "delete `__pycache__`". Listed files opened without following links or blocking on pipes; names printed without terminal codes; versions with hidden characters refused. 53 tests (1,983 total), 47 fail on the old code; 17 of 17 deliberate breakages caught; checked on the repo's own `prototype/` folder with throwaway keys)*
- [ ] Many seed nodes in different regions
- [ ] Node peer limits and rate limits
- [ ] Public status page
- [ ] Mining fairness review: pool distribution, botnet/insider checks
- [ ] Casino: cold storage, withdrawal limits/delays, confirmations by deposit size, provably fair audit
- [x] Incident response plan, draft: `docs/INCIDENT-RESPONSE.md` (first 15 minutes, pause, playbooks for leaked keys/tokens, signing key, points bug, account takeover, impersonation)
- [ ] Fill in the plan's owner blanks: backup person and where public notices go (owner decision)

## Phase 5: Wallet, Casino, AgentWiki
- [ ] Wallet and explorer
- [ ] Deposit/withdrawal flow
- [ ] Provably fair games and casino MVP
- [ ] Responsible-gambling tools and self-exclusion
- [ ] AgentWiki design: storage proofs, spam fees, moderation

## Phase 6: Brand and Web
- [x] Home v1, Mine, Tokenomics, Stay Safe (drafts)
- [x] Home v2: FAQ, share card (community links pending handles)
- [x] Custom graphics: logo mark, favicon, boat art, OG share image
- [x] Contribute a GPU page (browser check, Linux check, tester form)
- [x] Desktop/mobile testing (390, 768, 1440 px)
- [x] Subpages restyled to match the orange/black homepage
- [x] Public logbook page (`/reports/`): lists published periods from `reports/index.json`, how to check them, FAQ; linked from /contribute (homepage unchanged)
- [x] Stay Safe page rebuilt in the orange/black subpage style *(`/stay-safe`, 0.5.51: honest status (no coin, token, presale, airdrop, casino or miner yet), six "we will never" rules, the only official places (site and GitHub; social accounts "not announced yet"), how signed downloads will be checked (`sha256sum -c`, `app.release verify`, 2+ signers), how to report a fake or a security hole, FAQ. Linked from the /contribute and /reports/ footers and the contribute scam FAQ; homepage unchanged. Site and accessibility checks pass; no sideways scroll at 390/768/1440 px; new `tests/test_site_pages.py` (31 tests) also guards every subpage against hype/money claims. Your own draft can still replace it: upload it as `stay-safe.html`)*
- [ ] Rebuild or upload Mine and Tokenomics pages (need the supply/launch decisions first)
- [ ] Litepaper, Casino coming-soon, Meme Lab
- [x] Waitlist backend: Netlify Forms (`gpu-testers`)
- [x] Netlify project created (`boatsnhoes-dik6`), form detection on
- [x] Link Netlify to GitHub and confirm live deploy
- [x] Point boatsnhoes.meme DNS at Netlify (Cloudflare CNAMEs, DNS only)
- [x] Check every page's links, files, share cards and CSP fit offline, in CI *(`python -m app.site_check static`, 0.5.45: passes on all 5 pages; homepage has no canonical URL, noted only)*
- [ ] Verify HTTPS, security headers and share card on the live URL (same site-check workflow as above)
- [x] Accessibility check for every page, in CI *(`a11y-check-v1`, 0.5.50: `python -m app.a11y_check`; names for links, buttons, images and fields, headings, landmarks, skip link, zoom, focus rings, reduced motion, text contrast against the real background. All 4 subpages pass with nothing to fix; axe-core agrees (0 violations at 1440 and 390 px). Homepage findings are notes only; 70 tests, 994 total)*
- [ ] Homepage accessibility notes (owner decision; your design, so not changed): white button text on the light end of the orange gradient (1.8:1), dim `↓` arrows (3.9:1), the CTA box's small text (4.1:1 and 2.5:1), no skip link, an h1→h3 jump, no reduced-motion rule, an `<aside>` inside `<main>`. See USER_TODO

## Phase 7: Launch
- [ ] Mainnet plan, miner onboarding, listings outreach
- [ ] Community campaign, post-launch monitoring
