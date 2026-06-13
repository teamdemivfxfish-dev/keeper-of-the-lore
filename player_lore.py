"""
player_lore.py — durable store for community-submitted lore and per-guild setup.

Kept deliberately OUT of lore.txt (which stays BARUNN's pristine canon). This holds
three things in one JSON file (keeper_state.json):

  - guilds:   per-guild config (which role may approve, which channel reviews go to)
  - pending:  submissions awaiting an admin decision, keyed by the review message id
  - approved: accepted player lore, the bot's "memory" of community canon

The bot injects 'approved' into Gemini as a secondary lore block and embeds it into
the local RAG index, so approved lore is remembered by both backends. No discord or
model imports here, so it can be edited/inspected standalone.
"""

import os
import json
import threading
from typing import Optional

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keeper_state.json")
_lock = threading.RLock()
_state: Optional[dict] = None


def _load() -> dict:
    global _state
    if _state is None:
        if os.path.exists(_PATH):
            try:
                with open(_PATH, encoding="utf-8") as fh:
                    _state = json.load(fh)
            except Exception:
                _state = {}
        else:
            _state = {}
    _state.setdefault("guilds", {})
    _state.setdefault("pending", {})
    _state.setdefault("approved", [])
    return _state


def _save() -> None:
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_state, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, _PATH)


# --- per-guild config ----------------------------------------------------- #

def get_approver_role(guild_id) -> Optional[int]:
    with _lock:
        return _load()["guilds"].get(str(guild_id), {}).get("approver_role_id")


def set_approver_role(guild_id, role_id: int) -> None:
    with _lock:
        _load()["guilds"].setdefault(str(guild_id), {})["approver_role_id"] = role_id
        _save()


def get_review_channel(guild_id) -> Optional[int]:
    with _lock:
        return _load()["guilds"].get(str(guild_id), {}).get("review_channel_id")


def set_review_channel(guild_id, channel_id: int) -> None:
    with _lock:
        _load()["guilds"].setdefault(str(guild_id), {})["review_channel_id"] = channel_id
        _save()


# --- pending submissions (keyed by the review message id) ----------------- #

def add_pending(message_id, record: dict) -> None:
    with _lock:
        _load()["pending"][str(message_id)] = record
        _save()


def get_pending(message_id) -> Optional[dict]:
    with _lock:
        return _load()["pending"].get(str(message_id))


def remove_pending(message_id) -> Optional[dict]:
    with _lock:
        rec = _load()["pending"].pop(str(message_id), None)
        _save()
        return rec


# --- approved community lore (the bot's memory) --------------------------- #

def add_approved(entry: dict) -> None:
    with _lock:
        _load()["approved"].append(entry)
        _save()


def approved_entries() -> list:
    with _lock:
        return list(_load()["approved"])


def approved_texts() -> list:
    """One string per approved entry. Headed by a natural credit line (community lore,
    by its author) so the model can attribute it without echoing a raw bracket tag."""
    return [
        f"(community lore, established by {e.get('author_name', '?')})\n{e['text']}"
        for e in approved_entries()
    ]


def remove_approved(entry_id: str) -> bool:
    with _lock:
        st = _load()
        before = len(st["approved"])
        st["approved"] = [e for e in st["approved"] if e.get("id") != entry_id]
        changed = len(st["approved"]) != before
        if changed:
            _save()
        return changed
