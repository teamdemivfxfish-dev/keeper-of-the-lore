"""
local_backend.py — the offline fallback brain for Keeper of the Lore.

When Gemini is rate-limited or unreachable, the bot falls back to a local model
served by Ollama. A small local model cannot chew the whole ~53K-token saga on
modest hardware, so this module does retrieval (RAG): it embeds the saga once,
caches the vectors to disk, and at query time feeds the model only the handful of
passages most relevant to the question. One engine (Ollama) does both the
embedding and the generation, so if Ollama is up, the whole fallback works with
zero cloud calls.

Nothing here imports discord or google-genai, so it can be tested standalone.
"""

import os
import re
import json
import pickle
import hashlib
import logging
from typing import Optional

import numpy as np
from ollama import Client
from dotenv import load_dotenv

# Load .env here too: this module is imported before keeper_of_the_lore.py calls
# load_dotenv(), so without this its OLLAMA_* settings would fall back to defaults.
load_dotenv()

log = logging.getLogger("keeper.local")

# --- config (all overridable from .env) ---
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:latest")
EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
TOP_K = int(os.getenv("RAG_TOP_K", "8"))
NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
CHUNK_CHARS = int(os.getenv("RAG_CHUNK_CHARS", "1400"))

_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".rag_cache.pkl")

# Bump when the embedding/chunking scheme changes so old caches rebuild.
_EMBED_SCHEME = "nomic-prefix-v3-cite"

_BOOK_RE = re.compile(r'^\s*BOOK\s+[IVXLCDM]+:\s*.+$')
_CHAP_RE = re.compile(r'^\s*Chapter\s+\d+:\s*.+$')

_client = Client(host=OLLAMA_HOST)
_index: Optional[dict] = None   # canon lore index: {"scheme", "hash", "chunks", "embeds"}
_player: Optional[dict] = None  # approved player-lore index: {"chunks", "embeds"}


# --------------------------------------------------------------------------- #
# Embeddings + index
# --------------------------------------------------------------------------- #

def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scheme() -> str:
    """Identifies the embedding setup; changing it invalidates the cache."""
    return f"{EMBED_MODEL}|{_EMBED_SCHEME}"


def _embed(text: str, kind: str = "document") -> np.ndarray:
    """Unit-normalized embedding. nomic-embed-text needs task prefixes; add them
    when the embed model is a nomic model, otherwise embed the raw text."""
    if "nomic" in EMBED_MODEL.lower():
        text = ("search_query: " if kind == "query" else "search_document: ") + text
    resp = _client.embeddings(model=EMBED_MODEL, prompt=text)
    vec = np.asarray(resp["embedding"], dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm else vec


def chunk_lore(text: str) -> list[tuple[str, str]]:
    """Walk the saga line by line, tracking the current BOOK and Chapter, and pack the
    body into ~CHUNK_CHARS chunks that never span a chapter boundary. Returns a list of
    (display, body) pairs: 'display' is headed by [BOOK ... | Chapter ...] so the model
    can cite where a line is written; 'body' is the bare text used for embedding."""
    book = ""
    chapter = ""
    out: list[tuple[str, str]] = []
    buf: list[str] = []
    buflen = 0

    def loc() -> str:
        return ", ".join(x for x in (book, chapter) if x)

    def flush():
        nonlocal buf, buflen
        body = "\n".join(buf).strip()
        if body:
            display = f"(from {loc()})\n{body}" if loc() else body
            out.append((display, body))
        buf = []
        buflen = 0

    for raw in text.splitlines():
        line = raw.rstrip()
        if _BOOK_RE.match(line):
            flush()
            book = line.strip()
            chapter = ""
            continue
        if _CHAP_RE.match(line):
            flush()
            chapter = line.strip()
            continue
        if not line.strip():
            if buf and buf[-1] != "":
                buf.append("")
                buflen += 1
            continue
        seg = line
        while len(seg) > CHUNK_CHARS:           # rare: a single overlong line
            if buf:
                flush()
            piece = seg[:CHUNK_CHARS]
            out.append((f"(from {loc()})\n{piece}" if loc() else piece, piece))
            seg = seg[CHUNK_CHARS:]
        if buf and buflen + len(seg) + 1 > CHUNK_CHARS:
            flush()
        buf.append(seg)
        buflen += len(seg) + 1
    flush()
    return out


def _build(text: str) -> dict:
    pairs = chunk_lore(text)
    log.info("Embedding %d lore chunks for local RAG (one-time, then cached)...", len(pairs))
    embeds = np.vstack([_embed(body, "document") for _disp, body in pairs]).astype(np.float32)
    chunks = [disp for disp, _body in pairs]
    return {"scheme": _scheme(), "hash": _hash(text), "chunks": chunks, "embeds": embeds}


def _valid(cached: dict, h: str) -> bool:
    return cached.get("hash") == h and cached.get("scheme") == _scheme()


def get_index(text: str) -> dict:
    """Load the cached index if lore + embed scheme are unchanged, else rebuild."""
    global _index
    h = _hash(text)
    if _index is not None and _valid(_index, h):
        return _index
    if os.path.exists(_CACHE_PATH):
        try:
            with open(_CACHE_PATH, "rb") as fh:
                cached = pickle.load(fh)
            if _valid(cached, h):
                _index = cached
                return _index
            log.info("Lore or embed scheme changed; rebuilding RAG index.")
        except Exception as exc:  # noqa: BLE001
            log.warning("RAG cache unreadable (%s); rebuilding.", exc)
    _index = _build(text)
    try:
        with open(_CACHE_PATH, "wb") as fh:
            pickle.dump(_index, fh)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not write RAG cache: %s", exc)
    return _index


def set_player_lore(entries: list[str]) -> None:
    """(Re)build the in-memory index of approved player lore so it is retrievable
    locally alongside the canon. Call after any approval; cheap (few entries)."""
    global _player
    entries = [e for e in entries if e and e.strip()]
    if not entries:
        _player = None
        return
    embeds = np.vstack([_embed(e, "document") for e in entries]).astype(np.float32)
    _player = {"chunks": entries, "embeds": embeds}
    log.info("Local player-lore index updated (%d entries).", len(entries))


def retrieve(text: str, query: str, k: int = TOP_K) -> tuple[list[str], list[str]]:
    """Return (saga_passages, community_passages): the strongest k passages overall,
    split by source so the prompt can keep the original saga and player-established
    lore clearly apart (and the model cannot fuse one into the other)."""
    idx = get_index(text)
    q = _embed(query, "query")
    scored: list[tuple[float, str, bool]] = []  # (score, text, is_player)

    sims = idx["embeds"] @ q
    for i in np.argsort(-sims)[:k]:
        scored.append((float(sims[i]), idx["chunks"][i], False))

    if _player is not None:
        psims = _player["embeds"] @ q
        for i in np.argsort(-psims)[:k]:
            scored.append((float(psims[i]), _player["chunks"][i], True))

    scored.sort(key=lambda s: -s[0])
    top = scored[:k]
    saga = [t for _, t, p in top if not p]
    player = [t for _, t, p in top if p]
    return saga, player


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #

def _rag_system(preamble: str, saga: list[str], player: list[str], instructions: str) -> str:
    """Build the system prompt with the two sources in clearly separated sections."""
    parts = [preamble]
    if saga:
        parts.append(
            "===== PASSAGES FROM THE ORIGINAL SAGA (BARUNN's canon) =====\n"
            + "\n\n---\n\n".join(saga)
            + "\n===== END SAGA PASSAGES ====="
        )
    if player:
        parts.append(
            "===== COMMUNITY LORE (established by players; a SEPARATE source, NOT part of "
            "the original saga, and never to be blended with it) =====\n"
            + "\n\n---\n\n".join(player)
            + "\n===== END COMMUNITY LORE ====="
        )
    if not saga and not player:
        parts.append("(No passages were found for this question.)")
    parts.append(instructions)
    return "\n\n".join(parts)


def local_answer(text: str, question: str, preamble: str, instructions: str) -> str:
    """Q&A through the local model over retrieved passages."""
    saga, player = retrieve(text, question)
    system = _rag_system(preamble, saga, player, instructions)
    resp = _client.chat(
        model=OLLAMA_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ],
        # Headroom so a tight answer-first telling always finishes its last sentence
        # (the prompt keeps it ~900-1300 chars; this cap just prevents mid-word cutoff).
        options={"num_ctx": NUM_CTX, "temperature": 0.5, "num_predict": 800},
    )
    return (resp["message"]["content"] or "").strip()


def _local_json(text: str, retrieve_query: str, user_content: str,
                preamble: str, instructions: str, schema: dict) -> Optional[dict]:
    """Shared helper: RAG-retrieve, then a JSON-constrained local chat call."""
    saga, player = retrieve(text, retrieve_query)
    system = _rag_system(preamble, saga, player, instructions)
    resp = _client.chat(
        model=OLLAMA_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        options={"num_ctx": NUM_CTX, "temperature": 0.0},
        format=schema,
    )
    raw = resp["message"]["content"] or ""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("Local model gave non-JSON: %r", raw[:200])
        return None


def local_moderate(text: str, message: str, preamble: str,
                   instructions: str, schema: dict) -> Optional[dict]:
    """Canon-contradiction check through the local model, constrained to JSON."""
    return _local_json(
        text, message,
        f"Review this player message for canon contradictions:\n\n<message>\n{message}\n</message>",
        preamble, instructions, schema,
    )


def local_analyze(text: str, submission: str, preamble: str,
                  instructions: str, schema: dict) -> Optional[dict]:
    """Analyse a submitted piece of lore (contradictions + synopsis), constrained to JSON."""
    return _local_json(
        text, submission,
        f"Evaluate this submitted lore:\n\n<submission>\n{submission}\n</submission>",
        preamble, instructions, schema,
    )


# --------------------------------------------------------------------------- #
# Health / warm-up
# --------------------------------------------------------------------------- #

def available() -> bool:
    """True if the Ollama server answers."""
    try:
        _client.list()
        return True
    except Exception:  # noqa: BLE001
        return False


def warm(text: str) -> None:
    """Build the index ahead of the first real query so the first fallback is fast."""
    try:
        get_index(text)
        log.info("Local RAG index ready (%d chunks).", len(_index["chunks"]) if _index else 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("Local RAG warm-up failed (Ollama down?): %s", exc)
