# Incident Response Plan (draft)

**Status:** draft, version 0.5.29 (2026-11-23). Written for today's setup: one owner, a static
website on Netlify, and a GPU-worker prototype that pays **simulated off-chain points only**.
No token, wallet, chain or real money exists, so nobody can lose funds through this project
today. When any of those exist, this plan must be redone (and reviewed by someone else)
before launch.

The blanks marked **(owner)** need your decision. Nothing else here needs one.

## The first 15 minutes, any incident

1. **Stop new damage.** For the worker coordinator: pause it (below). For the website: see
   "Website or account taken over".
2. **Write down the time** and what you saw (screenshots, URLs, log lines). Don't delete
   anything yet: logs and the database are your evidence.
3. **Don't post yet** unless people are actively at risk (for example a fake site asking
   for keys). A wrong first post is worse than a 30-minute delay.
4. **Work through the matching section below**, then resume and write a short public note.

## Who does what

| Role | Who | Backup |
|------|-----|--------|
| Decides, fixes, posts | Owner (turnflockoff) | **(owner)** a trusted second person, if any |
| Second pair of eyes before resuming | **(owner)** | none yet |
| Where the public hears about it | **(owner)**: the pinned post on the official X/Telegram/Discord, once the handles exist | the homepage, by a normal pull request |

With one person, the realistic rule is: **pause first, think second, resume only when you
can say in one sentence what was wrong and what you changed.**

## Emergency pause (worker coordinator, built in 0.5.29)

Stops the coordinator handing out or creating work and enrolling new rigs, at once, and
stays stopped across restarts. Results for jobs already handed out are still accepted, and
revoking a rig still works.

```bash
# From anywhere, with the operator token:
curl -X POST https://<coordinator>/v1/operator/pause \
     -H "X-Operator-Token: $OPERATOR_TOKEN" -H "Content-Type: application/json" \
     -d '{"reason": "short note, operator-only"}'

# Or on the server itself (from prototype/):
python -m app.incident pause  --reason "short note"
python -m app.incident status
python -m app.incident resume --note "what you checked or fixed"   # server only
```

There is no "resume" over the network on purpose: a leaked operator token can stop the
coordinator but never restart it. Workers see `503` with `Retry-After: 60`; the public
`/health` says `"status": "paused"` (no reason shown).

## Playbooks

### A rig's worker key leaked, or a rig is cheating
1. `python -m app.workers revoke <worker_id>` (or `POST /v1/workers/<id>/revoke`). Its key
   stops working and any job it holds goes back in the queue.
2. Look at `/v1/workers/<id>/trust` and `python -m app.review` for its held results.
3. If it was cheating, its results stay held; what to do with its points is the scoring-v2
   owner decision (see TASKS.md).
4. Give the rig owner a new enrollment code only after you know how the key leaked.
   Pause is optional here: revoking is usually enough.

### The operator token leaked
1. Pause (CLI on the server, since you can't trust the token).
2. Set a new `OPERATOR_TOKEN` (24+ random characters) in the server's environment and
   restart the coordinator. The old token stops working.
3. Check `/v1/audits` and the events table for hidden tests or enrollment codes you didn't
   create; revoke rigs enrolled with codes you didn't hand out.
4. Resume with a note.

### The coordinator's signing key file may have been copied
1. Pause.
2. Rotate: `python -m app.key_rotation rotate --handover-key-file <USB stick>/handover.key`.
   With the offline handover key set up (TASKS.md owner step), a thief with only the old
   key file can't move workers to their own key.
3. Without a handover key, tell rig owners privately to re-pin the new public key
   fingerprint, which you read to them from `python -m app.key_rotation show`.
4. Resume.

### A bug is paying the wrong points
1. Pause, so no more jobs are handed out under the bug.
2. Keep the database file as it is (copy it first). Points are simulated, so nothing is
   lost; the fix is a corrected report, never editing old rows by hand.
3. Fix the code with a test that shows the bug, then publish a new signed report for the
   affected period (`python -m app.rewards report ... --sign`) with a note.

### Website or account taken over (GitHub, Netlify, domain, social)
1. From a clean device, change the password and kill other sessions for that account;
   turn on hardware-key or authenticator 2FA if it wasn't on.
2. **Netlify:** roll back to the last good deploy (Deploys > pick one > "Publish deploy").
   Changing DNS, Netlify or Cloudflare settings is the owner's job, not Claude's.
3. **GitHub:** check recent commits on `main`, deploy keys, collaborators and installed apps;
   revert bad commits with a normal pull request.
4. **Domain:** check the registrar lock and that DNS still points at Netlify (USER_TODO.md
   has the expected records).
5. Post once the site is back, from an account you control: what happened, what you never
   ask for (recovery phrases, keys, payments), and the official links.

### Someone is impersonating the project (fake site, fake "admin", fake miner)
1. Don't engage with the fake. Screenshot it.
2. Report it to the host/platform and the registrar.
3. Post a short warning with the official links. Never link to the fake directly.

## After every incident (within a week)
- A short note in `docs/BUILD-LOG.md`: what happened, when it was paused and resumed, what
  changed. Keep keys, tokens and private details out of it.
- If a tool was missing or confusing during the incident, add it to `docs/plan/TASKS.md`.

## Not covered yet
Casino, wallet, chain and token incidents (theft, 51% attack, inflation bug) don't exist yet
because those parts don't exist. They need their own plan, reviewed by someone with security
experience, before any of them launches.
