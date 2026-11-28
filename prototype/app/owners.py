"""Owner labels and worker ids that look the same (`owner-name-v1`, code audit pass 27).

The rule "rigs with the same owner never audit or break ties on each other's results"
(threat model #2, #3, #6) is only as good as the owner label. The operator types that label
each time they make an enrollment code, so the same person can easily end up as `Alice` one
day and `alice` the next, or `bob-smith` and `bob_smith`. Before this rule those were
different owners, and one person's second rig could re-run (and confirm) their first rig's
answer.

  * Owner labels are saved in lower case (`canonical`), and every owner comparison in the
    ledger ignores letter case, which also covers labels written by older code.
  * A new label that matches an existing one once `.`, `_` and `-` are dropped
    (`owner_key`), but isn't the same label, is refused with the existing name, so the
    operator either types that exact name or picks a clearly different one.
  * Worker ids are chosen by the rig at registration. One that matches an existing worker
    id once letter case and `.`, `_`, `-`, `:` are ignored (`worker_key`) is refused, so a
    new rig can't call itself `WK-A` or `wk_a` next to `wk-a` on the operator's screens.

Not covered: look-alike letters and digits (`0`/`o`, `1`/`l`), spelling variants, or one
person using two genuinely different names. Labels are still only as good as the operator's
hand-out (threat model #2).
"""
from __future__ import annotations

import re

OWNER_RULE = "owner-name-v1"

_OWNER_SEPARATORS = re.compile(r"[._-]")
_WORKER_SEPARATORS = re.compile(r"[._:-]")


class OwnerLookalike(ValueError):
    """A new owner label or worker id reads as an existing, different one."""

    def __init__(self, kind: str, existing: str) -> None:
        super().__init__(lookalike_message(kind, existing))
        self.kind = kind
        self.existing = existing


def canonical(owner: str) -> str:
    """How an owner label is saved: lower case. Labels are ASCII (see the model's pattern)."""
    return owner.lower()


def owner_key(owner: str) -> str:
    """Owner labels with the same key read as the same person."""
    return _OWNER_SEPARATORS.sub("", owner.lower())


def worker_key(worker_id: str) -> str:
    """Worker ids with the same key read as the same rig."""
    return _WORKER_SEPARATORS.sub("", worker_id.lower())


def _say(text: str, limit: int = 80) -> str:
    """A stored name in a message: printable ASCII only, cut short. The message goes to the
    operator's terminal, logs and HTTP replies."""
    clean = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in text)
    return clean if len(clean) <= limit else clean[:limit] + "..."


def lookalike_message(kind: str, existing: str) -> str:
    if kind == "owner":
        return (f"owner looks like the existing owner '{_say(existing)}' (only letter case or "
                f". _ - differ). Use exactly that name if it's the same person, or a clearly "
                f"different one")
    return (f"worker id looks like the existing worker '{_say(existing)}' (only letter case or "
            f". _ - : differ); pick a clearly different id")
