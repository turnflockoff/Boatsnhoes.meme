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
