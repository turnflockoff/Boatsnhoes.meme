# BOATS N HOES (boatsnhoes.meme)

> **Canonical source of truth.** If a chat conflicts with this file, this file wins until deliberately updated.

**Version:** 0.5.18 | **Last updated:** 2026-11-22 | **Status:** Site LIVE at boatsnhoes.meme (Netlify); GPU worker backend Phases 1, 2, 4 and 5 (audits, tie-breakers, threat model, simulated reward reports) plus per-worker keys, random auditor assignment, Ed25519 job signatures, versioned job/receipt checks, signed reward reports, shareable ledger copies, structured logs/health checks, a read-only rig inventory tool, trust-based audit rates, planned signing-key rotation, audit follow-through counts and an operator review queue built, untested on real GPUs

## SESSION PROTOCOL (Claude must follow every session)
1. **Start:** Read README.md, TASKS.md, and USER_TODO.md. Summarize status in 3 lines.
2. **During:** Check proposals against Decisions, Constraints, and Security. Flag conflicts.
3. **End:** Output updated README.md, TASKS.md, USER_TODO.md: bump version, date, changelog entry, move settled items from Open Questions to Decisions, check off done tasks.
4. **Never** silently drop earlier decisions. Mark them "superseded" with the reason.

## Project Summary
A viral meme coin, BOATS N HOES ($BNH): proof-of-work mined on consumer GPUs/CPUs, hard-capped with halvings, used as currency on a gambling site, and possibly tied to a hosted site called AgentWiki (see Open Questions). **Merged 2026-11-21** with the GitHub repo track (useful GPU compute, RX 470 hardware validation): step one is proving the hardware in public before any chain or token exists.

## Constraints and Known Facts
- Ethereum L1 is proof of stake (since Sept 2022): it cannot be mined and is not ending.
- Ethereum Classic is still proof of work (checked Nov 2026), EVM-compatible, but ASIC-dominated and has had 51% attacks. A custom algorithm is needed to stay GPU/CPU friendly.
- Plain hashing does not "host" anything. Hosting needs disk and bandwidth (see AgentWiki).
- Gambling use triggers licensing, KYC/AML, age-verification, and securities questions. Lawyer review before launch.
- No price, return, or "moon" claims anywhere. Anything not final is labeled draft.

## Decisions
| # | Decision | Status | Date |
|---|----------|--------|------|
| 1 | Domain: boatsnhoes.meme | Final | 2026-11-19 |
| 2 | Block time: 60 seconds, difficulty adjusts every block | Proposed | 2026-11-20 |
| 3 | Halving: every 3.5 years (1,840,860 blocks), 75% mined at 7 years | Proposed | 2026-11-20 |
| 4 | Max supply: hard cap near 100M; starting reward 27 (about 99.4M) | Leaning | 2026-11-20 |
| 5 | Ticker: $BNH, community nickname $BOATS | Proposed | 2026-11-19 |
| 6 | Launch style: fair launch, no premine | Leaning | 2026-11-20 |
| 7 | Chain path | Open | |
| 8 | Merge meme-coin brand with GitHub GPU-compute track; hardware validation comes first | Final | 2026-11-21 |
| 9 | Hosting: Netlify (project `boatsnhoes-dik6`), deploys from GitHub. **Supersedes** Cloudflare Pages plan: owner chose Netlify on 2026-11-21. `_headers` works unchanged on Netlify | Final | 2026-11-21 |
| 10 | Waitlist backend: Netlify Forms (`gpu-testers` form on /contribute) | Final | 2026-11-21 |
| 12 | Homepage: orange/black orbital design (GitHub version A), copy kept as written. Supersedes the merged pink homepage; other pages keep decision 11 styling for now | Final | 2026-11-21 |
| 11 | Brand art: hoe-mast boat, $BNH sail, GPU-chip jib, GPU-fan portholes; palette cream/navy/pink/blue/sun | Final | 2026-11-21 |

## Open Questions
- Chain path: new PoW chain, EVM PoW fork, or other?
- **AgentWiki** (domain not yet bought, check availability): a site for AI agents and humans where mining ties to hosting. Recommended design: PoW secures the chain; optional hosting nodes earn a bonus for storage proofs. Needs content moderation, spam fees in $BNH, and legal review. Consider a name other than "rogue" agents.
- Tail emission vs strict hard cap?
- Final supply, premine (leaning none), gambling jurisdiction?

## SECURITY
Threats and defenses. Status: planned unless marked done.

| # | Threat | Defense | Status |
|---|--------|---------|--------|
| 1 | 51% attack (rented hash). ETC rental cost about $3,800 vs $513,000+ for Bitcoin | Unique algorithm, hashrate monitoring, checkpoints/finality rules, many confirmations for exchanges and casino | Planned |
| 2 | Fake sites, fake miners, wallet drainers, fake admins | Registrar lock, hardware-key 2FA everywhere, signed miner downloads with checksums, pinned official links, "team never asks for recovery phrases" | Page built; rest planned |
| 3 | Casino theft (hot wallet, early credit, bad randomness, bonus abuse, insiders) | Cold storage, confirmations scaled to deposit size, withdrawal limits and delays, independent audit | Planned |
| 4 | Code bugs (inflation bug, chain split) | Fork battle-tested code, long public testnet, audit, bug bounty | Planned |
| 5 | Mining unfairness (secret fast miner, botnet, dominant pool) | Public testnet, algorithm review, visible pool distribution | Planned |
| 6 | Network attacks (flooding, isolating nodes/seeds/pools) | Many seed nodes, rate limits, DDoS protection | Planned |
| 7 | Market manipulation, rug-pull claims | Fair launch, transparent allocations | Planned |
| 8 | Team and key risk | Multiple signers, offline key storage | Planned |

**Legal risk** is not an attack but can shut the project down faster than any hacker.

### DDoS and web hardening
- Website: Netlify (decision 9). Netlify's CDN gives baseline DDoS protection. ~~Cloudflare Pages~~ superseded 2026-11-21. Cloudflare can still sit in front for DNS/WAF later if wanted.
- `_headers` file (built): HSTS, CSP, no framing, nosniff, locked-down permissions. Upload it with the site.
- If Cloudflare DNS is used in front later: Bot Fight Mode, WAF managed rules, rate limiting on forms/APIs, DNSSEC, Always Use HTTPS, "Under Attack" mode as an emergency switch.
- Chain/node level: many geographically spread seed nodes, peer limits, rate limits, a public status page (see TASKS.md).

## Site Pages
- Built and in repo: Home v2 (merged: hero, tap-to-mine demo, deal, honest status, voyage, FAQ, share card), Contribute a GPU (/contribute: browser GPU check, Linux check, tester sign-up), /thanks, 404.
- Drafts not in repo (owner to upload): Mine, Tokenomics, Stay Safe. Stay-safe basics are folded into the home FAQ and contributor FAQ for now.
- Not built: Litepaper, Casino coming-soon, Meme Lab, AgentWiki.

## Changelog
- **0.5.18 (2026-11-22):** Operator review queue (threat model #7). `GET /v1/operator/review` and `python -m app.review` (read-only) list every case with points held: stuck disputes (tie-breaker never ran or wasn't scheduled), three-way disagreements and failed hidden tests are `needs_review`; disputes with a tie-breaker running are `waiting`; two-against-one outcomes are `decided_by_majority` (spot checks). Each case shows every answer side by side with rig, owner label, strikes, which answers agree under the task's tolerance rule, points held and age. Operator health adds `review` counts and a warning. No override: what a reviewer's decision does to points is scoring v2 (owner decision). Software only. 190 tests.
- **0.5.17 (2026-11-22):** Audit follow-through (threat model #4 follow-up). The operator trust report now counts, per rig, the audits/tie-breakers it was randomly handed and `completed`, and the ones it dropped: `not_claimed` (new private event `audit_assignment_expired`), `lease_expired`, `refused`. Re-picks judged on the latest pick only; revoked picks not counted. `/v1/operator/health` adds `audit_dropouts` and a warning at `AUDIT_DROP_WARN` (default 3). Lifetime counts, operator-only, no effect on points or audit rates; an offline rig looks the same as one skipping checks. Software only. 177 tests.
- **0.5.16 (2026-11-22):** Planned coordinator key rotation (threat model #9 follow-up). `python -m app.key_rotation rotate|show`: new Ed25519 key plus a handover (old → new, sequence, time; `purpose: bnh-coordinator-key-rotation/1`) signed by both keys, saved in the public `coordinator-signing.key.history.json`; old private key not kept. `/v1/coordinator/key` serves the chain; the demo worker updates its pin only along a valid chain. Jobs queued under a retired key are re-signed on claim (`job_resigned`); foreign signatures are never re-signed. Crash-safe; coordinator refuses to start on an interrupted rotation or a key that doesn't match its history. Doesn't help if the old key was stolen. Software only. 168 tests.
- **0.5.15 (2026-11-22):** Trust-based audit rate (threat model #1, `audit-tier-v1`). Each rig's unproven results are re-checked at a rate set by its own record: `suspect` (any failed hidden test or outvoted result) 1.0, `new` (fewer than 5 independently checked results) 0.5, `established` the base `AUDIT_RATE` 0.25; never below the base. New settings `AUDIT_RATE_NEW`, `AUDIT_RATE_SUSPECT`, `AUDIT_NEW_UNTIL`. Operator-only: trust report shows tier, rate, checks passed and strikes; `/v1/audits` lists the policy; operator health names rigs with a strike. Changes how often a rig is checked, never its points. Software only. 158 tests.
- **0.5.14 (2026-11-22):** Read-only hardware inventory (Phase 3 prep, Track B). `python -m app.hw_inventory --out inventory.json` reads /etc/os-release and runs only `uname -r`, `lspci -nnk` and `vulkaninfo --summary` (allowlisted, no shell, no sudo, no installs), lists /dev/dri, and writes a versioned JSON report (`bnh-hw-inventory/1`) with plain-language findings: AMD cards on PCI, bound kernel driver (warns on `radeon` or none), Vulkan devices and driver (warns when only a CPU renderer like llvmpipe is visible, or when lspci and Vulkan disagree on the AMD card count), and render nodes the user can't open. Hostname, username and UUIDs are redacted; refuses to overwrite. `proves_inference` is always false. Tested against format samples only, not the real rig. 151 tests.
- **0.5.13 (2026-11-22):** Shareable ledger copies (threat model #10, second step). `python -m app.ledger_copy export --until T --out copy.sqlite3` writes a redacted copy of a closed, settled period: worker key hashes, enrollment codes, payloads, signatures, answers, expected answers and event details removed; owner labels pseudonymized; payload hashes re-keyed with a throwaway HMAC key; deleted rows wiped. Anyone can re-run a signed report and all consistency checks against it (`rewards verify --db copy`, opened read-only). Refuses open periods and unfinished jobs. Reveals which past jobs were tests (share with a delay). Software only. 137 tests.
- **0.5.12 (2026-11-22):** Structured logs and health reporting (Track D). Coordinator logs one JSON object per line (`LOG_FORMAT`, `LOG_LEVEL`) with a request id echoed in `X-Request-ID`; keys, tokens, codes, prompts, answers and query strings are never logged, and challenges log like normal jobs. Public `/health` returns 503 when the database fails; new operator-only `/v1/operator/health` reports workers, queue, audits waiting for an eligible auditor, recent events, consistency problems and warnings. Software only. 127 tests.
- **0.5.11 (2026-11-22):** Signed reward reports (threat model #10, first step). `python -m app.rewards report --sign` / `?sign=true` adds the coordinator's Ed25519 signature (separate `purpose` from job signatures) to a closed period's report; `verify --no-db --public-key` checks it with no database. Refuses open periods, reports failing their own checks, and never creates a new key. Proves who issued a report, not that its numbers are right. Software only. 109 tests.
- **0.5.10 (2026-11-22):** Versioned job and receipt schemas (Track E). Jobs carry a signed `schema_version` (`bnh-job/1`) and, for AI jobs, the pinned `model_sha256`; audits copy it from the original. Workers refuse unknown versions or a different local model pin. Receipts (`bnh-receipt/1`) must echo the job version, input hash and model hash or get `422` (logged as `receipt_mismatch`). Pre-versioning jobs are marked `bnh-job/0` and refused. Catches mistakes, not liars. Software only. 97 tests.
- **0.5.9 (2026-11-22):** Job manifests are now signed with the coordinator's Ed25519 private key instead of a shared HMAC secret (threat model #9). Workers verify with the public key (`GET /v1/coordinator/key`), pinned on first contact or given by the operator, and stop if it changes. `PROTOTYPE_SECRET` retired; `python -m app.signing` prints the key. New dependency: `cryptography`. Software only. 89 tests.
- **0.5.8 (2026-11-22):** Audits and tie-breakers now go to one randomly chosen active, eligible worker instead of whoever asks first (threat model #4). Re-assigned at random if the pick doesn't claim it in time, is revoked, or gives it back. New settings `AUDITOR_ACTIVE_SECONDS`, `AUDIT_ASSIGN_SECONDS`; new consistency check. Software only. 82 tests.
- **0.5.7 (2026-11-22):** Worker sign-up now needs a one-time operator code; each rig gets its own key (stored only as a hash) for every claim and submit. Owner labels keep one person's rigs from auditing each other. Operator can revoke a rig. `python -m app.workers enroll|revoke|list`. Software only. 73 tests.
- **0.5.6 (2026-11-22):** GPU worker Phase 5: simulated reward reports. Versioned scoring rule `sim-points-v1` (confirmed + unverified are payable; pending and held are not), per-worker points and activity, reproducible `report_sha256`, `python -m app.rewards verify`, operator-only `/v1/rewards/report`. Off-chain points only. 61 tests.
- **0.5.5 (2026-11-22):** GPU worker Phase 4 finished in software: hidden tie-breaker re-run by a third worker when an audit disagrees (majority of three; outvoted/unresolved results stay held), new consistency checks, `TIEBREAK` setting. Written fraud/collusion threat model (`docs/VERIFICATION-THREAT-MODEL.md`). 51 tests.
- **0.5.4 (2026-11-22):** GPU worker Phase 4 (first slice): hidden re-run audits by a different worker, hidden known-answer challenge jobs, whitespace tolerance rule for AI text, worker trust report, public points split into confirmed/pending/unverified/held. Operator-only endpoints need `OPERATOR_TOKEN`. 44 tests.
- **0.5.3 (2026-11-22):** GPU worker Phase 2: task plug-in interface, llm:generate job type, pinned model registry (Qwen2.5-0.5B, Apache-2.0, unpinned until downloaded). Hourly automated build session scheduled.
- **0.5.2 (2026-11-21):** boatsnhoes.meme is live on Netlify. Domain and DNS connected (Cloudflare CNAMEs, DNS only), HTTPS requested, subpages restyled orange/black, GPU job queue Phase 1 built.
- **0.5.1 (2026-11-21):** Homepage switched to the orange/black orbital version you picked. Footer links fixed, GPU-page link and orange share image added, mobile icon overlap fixed.
- **0.5 (2026-11-21):** Merged meme-coin plan with GitHub GPU-compute repo. Built Home v2, Contribute a GPU page with Netlify Forms waitlist, custom graphics (logo, boat art, favicon, 1200x630 share card). Desktop/mobile tested (390/768/1440 px, real fonts, `_headers` CSP applied). Hosting switched to Netlify (supersedes Cloudflare Pages). Netlify project created; GitHub link pending.
- **0.4 (2026-11-20):** Added Security section, DDoS plan, Stay Safe page, `_headers`, USER_TODO.md. Logged 60s blocks, 3.5-year halving, ~100M cap, AgentWiki idea. Confirmed ETC is still proof of work.
- **0.1 (2026-11-19):** Project created. Corrected the Ethereum PoW assumption.
