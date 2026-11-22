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
