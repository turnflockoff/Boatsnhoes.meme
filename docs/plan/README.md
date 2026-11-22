# BOATS N HOES (boatsnhoes.meme)

> **Canonical source of truth.** If a chat conflicts with this file, this file wins until deliberately updated.

**Version:** 0.5.7 | **Last updated:** 2026-11-22 | **Status:** Site LIVE at boatsnhoes.meme (Netlify); GPU worker backend Phases 1, 2, 4 and 5 (audits, tie-breakers, threat model, simulated reward reports) plus per-worker keys built, untested on real GPUs

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
