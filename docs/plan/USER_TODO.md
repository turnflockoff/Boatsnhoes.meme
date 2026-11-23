# YOUR Personal To-Do List (things only you can do)
**Version:** 0.5.38 | **Last updated:** 2026-11-23
Never share passwords, API keys, or recovery phrases with anyone, including Claude.

## This week
- [ ] ~~Cloudflare Pages upload~~ superseded 2026-11-21: hosting is Netlify
- [x] Merge the GitHub pull requests into main (#2, #3)
- [x] Netlify linked to GitHub (branch `main`, auto-deploys on merge)
- [x] Domain live: `boatsnhoes.meme` primary, `www` redirects. Cloudflare DNS: `@` CNAME → apex-loadbalancer.netlify.com, `www` CNAME → boatsnhoes-dik6.netlify.app, both DNS only (grey cloud). Unused Netlify DNS zone deleted
- [x] HTTPS certificate requested (Let's Encrypt via Netlify)
- [ ] Optional: block email spoofing with Cloudflare TXT records `@` = `v=spf1 -all` and `_dmarc` = `v=DMARC1; p=reject;`
- [ ] Netlify: Forms > `gpu-testers` > add an email notification to your Netlify account email
- [ ] Upload mine.html, tokenomics.html, stay-safe.html (not on GitHub yet)
- [ ] Turn on hardware-key or authenticator 2FA for Netlify, GitHub, your registrar, email, and every social account
- [ ] Lock the domain at the registrar and enable auto-renew
- [ ] Registrar: enable DNSSEC if supported. (Cloudflare WAF/Bot Fight only if you later put Cloudflare DNS in front)
- [ ] Check availability of agentwiki domains and register early if you want it (also search for existing trademarks)
- [ ] Register the same handle on X, Telegram, Discord, GitHub; pin official links

## Before the testnet
- [ ] On the RX 470 rig (Ubuntu): from `prototype/`, run `python -m app.hw_inventory --out inventory.json`. No sudo needed; it only reads. Look over the file (hostname and username are already blanked) and share it or commit it so the next step (a Vulkan smoke test on each card) can be planned. If it says a tool is missing, installing `pciutils` or `vulkan-tools` is up to you
- [ ] Also on the rig, any time: `python -m app.gpu_sensors` from `prototype/` prints each card's temperature, power, VRAM and clocks (read-only, no sudo). Later, when a test runs on one card, run `python -m app.gpu_sensors --card card1 --samples 120 --interval 1 --out gpu1-sensors.json` in a second terminal to record how hot and full it got
- [ ] Later, for the first real test on a card: run it through the recorder, e.g. `python -m app.gpu_run --card card1 --out gpu1-run1.json -- <your test command>` from `prototype/` (no sudo). The report says whether card1 really did the work, or card0, or the CPU. Repeat with `--card card0` for the other card
- [ ] After both cards have a run report: `python -m app.phase3_check --inventory inventory.json gpu0-run1.json gpu1-run1.json` (from `prototype/`, reads files only). It prints the hardware checklist with [x] done, [~] partly, [ ] not yet, [!] something wrong, and why. Give both runs the same test and the same `--expect-file` so "repeat on GPU 1" can count
- [ ] Decide: chain path, final supply, fair launch (no premine)
- [ ] Decide: should a rig caught cheating lose its other unchecked points for that period? (simulated scoring v2; see TASKS.md). It's built and switched off: putting `REWARD_SCORING=sim-points-v2-forfeit` in `prototype/.env` turns it on. Upside: one caught fake costs all of that period's unchecked work, so faking stops paying. Downside: until the RX 470s show how much honest cards disagree, an honest rig could lose points over a GPU quirk. Simulated points only
- [ ] When the RX 470 rig is ready: set your own `OPERATOR_TOKEN` in `prototype/.env` (never share it; you now also need it, or a `SUBMITTER_TOKEN`, to queue jobs), then make one enrollment code per card with `python -m app.workers enroll --owner owner-gpu0` (and `owner-gpu1`). Keep the codes and the saved `.key` files private
- [ ] Decide: if a rig keeps dropping audits it was picked for (the operator health page warns after 3), should anything happen automatically, like checking its own work more often? Today it's only a warning, because a rig that went offline looks the same as one dodging checks
- [ ] Optional, when rigs are running: `python -m app.review` lists held results that need a person (side by side). It changes nothing. Decide, together with the scoring v2 question above, what your verdict on a case should do to its points
- [ ] Before you give a program the `SUBMITTER_TOKEN`: pick fixed points per job type and put them in `prototype/.env`, e.g. `POINTS_TABLE=demo:sha256=1,llm:generate=10` (whole numbers 0-1000, every job type listed). Then nobody, you included, can queue a job worth more than that. Simulated points only
- [ ] Decide: should rigs caught cheating stop being picked to check other rigs' work? Setting `AUDITOR_PICK=trusted-first` in `prototype/.env` does that (they're only used when nobody else is around). Downside: checking work pays points, so trusted rigs earn a bigger share of those. Default stays `uniform` until you choose
- [ ] Decide: when a tie-breaker settles an argument between two rigs, should it only count if both rigs on the winning side have a clean, proven record (`established`)? Setting `TIEBREAK_DECIDES=established-only` in `prototype/.env` does that; otherwise the case waits for you in `python -m app.review`. Upside: two brand-new rigs run by one person can't outvote an honest one. Downside: early on, while every rig is new, every argument waits for you, and the points stay held until scoring v2 says what your verdict does. Default stays `always` until you choose
- [ ] When you start one worker per card for AI jobs: give each its own card number, e.g. `LLM_VK_DEVICE=0` for the first worker and `LLM_VK_DEVICE=1` for the second (in that worker's environment, not a shared `.env`). Without it both workers may land on the same card. If a worker prints `Could not run job ...`, the reason is on that line; the job goes back to the queue on its own
- [ ] Before rigs go online: plug in a USB stick and run `python -m app.key_rotation make-handover-key --out <stick>/bnh-handover.key` from `prototype/`. Put the printed `COORDINATOR_HANDOVER_PUBLIC_KEY=...` line in `prototype/.env`, restart the coordinator, then unplug the stick and put it somewhere safe. You only need it again to change the signing key. Losing it means re-pinning every rig by hand, so a second copy on another stick (kept elsewhere) is a good idea
- [ ] Once a model runs on the rig: keep a private stock of hidden tests. Make a private folder outside the repo (`mkdir -m 700 ~/bnh-private`), then from `prototype/` run `python -m app.challenge_pool init --pool ~/bnh-private/tests.json`, then `add --file my-tests.jsonl` with prompts you wrote and the answers the pinned model gave, then e.g. `OPERATOR_TOKEN=... python -m app.challenge_pool issue --pool ~/bnh-private/tests.json --url http://127.0.0.1:8000 --count 3 --tier new` now and then. Never put that file in the repo or on a rig. To practise before then: `make-demo --count 10` adds SHA-256 tests
- [ ] Decide where signed reward reports and ledger copies get posted (for example a `reports/` page on boatsnhoes.meme, or GitHub releases) and how long to wait after a period closes before sharing a copy. Keep every old copy there (never replace one): `python -m app.ledger_copy compare old-copy new-copy` can only show history wasn't rewritten if the old copy is still around
- [ ] Find a blockchain developer or team to build the chain and miner
- [ ] Set up a project email and a shared password manager
- [ ] Back up recovery info offline (paper, in two places)
- [ ] Set up multiple release signers (never one person)
- [ ] Budget for an independent code audit and a bug bounty

## Before launch
- [ ] Hire a crypto and gambling lawyer; choose jurisdiction
- [ ] Decide KYC/AML and age verification provider
- [x] Waitlist planned: Netlify Forms on /contribute
- [ ] Prepare moderators and rules for community channels
- [ ] Choose cold-storage method for casino funds
- [ ] Incident plan: a draft is in `docs/INCIDENT-RESPONSE.md`. Fill in the three **(owner)** blanks (a backup person, who checks before you resume, where you post notices) and read the pause section once so you know the commands before you need them
