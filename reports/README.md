# Public logbook: signed reports and ledger copies

**Simulated, off-chain points from a test network. Not tokens, not money, and no promise of either.**

This folder is the project's one fixed public place for closed test periods (`reports-v1`).
The same files are served on the website at <https://boatsnhoes.meme/reports/>, which lists them;
GitHub keeps the full history of every change to this folder.

**Status:** nothing published yet. The first period goes up after test rigs run real jobs and
that period has been closed for 7 days.

## What's here

```
reports/
  index.json                                   list of published periods (bnh-reports-index/1)
  index.html                                   the website page (reads index.json)
  2026-12-01T0000Z_2026-12-08T0000Z/           one folder per closed period (UTC)
    report.json                                signed reward report for that period
    ledger-copy.sqlite3                        redacted ledger up to the end of the period
    SHA256SUMS                                 hashes of the two files
```

- **report.json**: points per rig for the period, signed with the coordinator's Ed25519 key.
- **ledger-copy.sqlite3**: every job, check and result behind the report, with worker keys,
  sign-up codes, prompts, answers, hidden tests' expected answers and real owner names removed.
  Re-running the report on it gives the identical report.
- Copies are cumulative, so each one must keep the previous copy's history unchanged.

## Check it yourself

From a clone of the repo, in `prototype/` (`pip install -r requirements.txt` first):

```bash
# Everything at once: hashes, signatures, each report against its copy, each copy against the one before
python -m app.publish_report check --public-key <coordinator key>

# Or one period by hand
(cd ../reports/<period> && sha256sum -c SHA256SUMS)
python -m app.rewards verify ../reports/<period>/report.json \
    --db ../reports/<period>/ledger-copy.sqlite3 --public-key <coordinator key>
python -m app.ledger_copy compare ../reports/<older>/ledger-copy.sqlite3 ../reports/<newer>/ledger-copy.sqlite3
```

Get the coordinator's public key from more than one place (`index.json`, the website, a
project post) and check they match. After a key rotation, pass `--public-key` once per key.
The tools treat every copy as a file from a stranger: they refuse one that holds anything
but the plain ledger tables (views, triggers, extra tables), is damaged or is oversized, and
they never change the file while checking it (`copy-open-v1`, `prototype/app/copy_guard.py`).
Checking proves who issued a report, that its numbers come from the posted ledger, and that
later copies didn't rewrite earlier history. It can't prove the coordinator recorded
everything honestly to begin with (see `docs/VERIFICATION-THREAT-MODEL.md`).

## Publishing a period (operator)

```bash
cd prototype
python -m app.rewards report --since <start> --until <end> --sign --out ~/bnh-private/report.json
python -m app.ledger_copy export --until <end> --out ~/bnh-private/ledger-copy.sqlite3
python -m app.publish_report add ~/bnh-private/report.json ~/bnh-private/ledger-copy.sqlite3 \
    --public-key <coordinator key> --dry-run      # every check, writes nothing
python -m app.publish_report add ~/bnh-private/report.json ~/bnh-private/ledger-copy.sqlite3 \
    --public-key <coordinator key>
```

`add` refuses the live ledger (only redacted copies), unsigned or edited reports, a copy that
doesn't match the report, a period that ended less than 7 days ago (`--min-wait-hours`), a
period that overlaps or comes before one already posted, a copy that changes already-posted
history, and anything already published (files here are never replaced). Then commit the new
folder and `index.json`, open a pull request, and it goes live when merged into `main`.
CI re-runs `check` on this folder for every change.

Rig names (`worker_id`) appear in both files. Pick rig names that don't reveal who runs them.
