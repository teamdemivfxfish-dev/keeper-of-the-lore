"""
Keeper of the Lore — a Discord bot for "The Warborn Realm" by BARUNN.

Two jobs:
  (a) Answer lore questions from the saga. Ask via /lore or by @mentioning the bot.
  (b) Watch designated roleplay channels and, when a message clearly contradicts
      established canon (e.g. a player declaring themselves a god), reply in
      character with the correction.

The entire saga fits in one model context, so there is no vector database. The
lore is sent as the system instruction on every call. Gemini 2.5's implicit
context caching automatically discounts the repeated lore prefix, so re-sending
it on every message is cheap.

Backend: Google Gemini (google-genai SDK).
"""

import os
import json
import time
import uuid
import logging
from typing import Literal

import discord
from discord import app_commands
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel

import local_backend
import player_lore

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]

# One or more Gemini keys. The bot rotates through them on quota/rate-limit
# errors before it ever falls back to the local model. Put extras (ideally from
# SEPARATE Google projects/accounts, or the shared quota makes rotation pointless)
# in GEMINI_API_KEYS as a comma-separated list; GEMINI_API_KEY is the primary.
def _gather_keys() -> list[str]:
    raw = [os.getenv("GEMINI_API_KEY", "")] + os.getenv("GEMINI_API_KEYS", "").split(",")
    seen, out = set(), []
    for k in (s.strip() for s in raw):
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


GEMINI_KEYS = _gather_keys()
if not GEMINI_KEYS:
    raise SystemExit("No Gemini key found. Set GEMINI_API_KEY (and optionally GEMINI_API_KEYS) in .env.")

# Optional: server (guild) IDs to sync slash commands to INSTANTLY. Global sync can
# take up to ~1h to show new commands; a guild sync is immediate. Comma-separated.
GUILD_IDS = [int(g) for g in os.getenv("GUILD_ID", "").replace(" ", "").split(",") if g.strip().isdigit()]

# Q&A model. Flash is fast, cheap, and has a generous free tier. Bump to
# gemini-2.5-pro in .env if you want sharper reasoning on hard questions.
QA_MODEL = os.getenv("MODEL", "gemini-2.5-flash")
# Moderation runs on (almost) every message in watched channels. Flash keeps
# that cheap. (Note: gemini-2.5-pro cannot disable thinking, so keep this on a
# Flash model unless you know what you're doing.)
MOD_MODEL = os.getenv("MOD_MODEL", "gemini-2.5-flash")

# Comma-separated channel IDs the lore-police watches. Empty = moderation off.
MOD_CHANNELS = {
    int(c) for c in os.getenv("MOD_CHANNELS", "").replace(" ", "").split(",") if c
}

# Only act on a contradiction at or above this confidence level. Order is
# low < medium < high < certain. "high" is a good, quiet default.
CONFIDENCE_LEVELS = ["low", "medium", "high", "certain"]
MOD_MIN_CONFIDENCE = os.getenv("MOD_MIN_CONFIDENCE", "high").strip().lower()
if MOD_MIN_CONFIDENCE not in CONFIDENCE_LEVELS:
    MOD_MIN_CONFIDENCE = "high"
# Skip messages shorter than this — too little to contradict anything.
MOD_MIN_LENGTH = int(os.getenv("MOD_MIN_LENGTH", "25"))

LORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lore.txt")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("keeper")

with open(LORE_PATH, "r", encoding="utf-8") as fh:
    LORE_TEXT = fh.read()

GEMINI_CLIENTS = [genai.Client(api_key=k) for k in GEMINI_KEYS]


class QuotaExhausted(Exception):
    """Every Gemini key is rate-limited / out of quota."""


def _is_quota_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, "429"):
        return True
    s = str(exc).lower()
    return any(t in s for t in ("resource_exhausted", "429", "quota", "rate limit", "ratelimit"))


_key_cursor = 0  # index of the key we're currently using


def _gemini_generate(make_call):
    """Run make_call(client) against each key in turn, rotating on quota errors.

    Sticks with the last working key. Non-quota errors propagate immediately so the
    caller can fall back to local. Raises QuotaExhausted only if every key is out.
    """
    global _key_cursor
    n = len(GEMINI_CLIENTS)
    last = None
    for off in range(n):
        i = (_key_cursor + off) % n
        try:
            resp = make_call(GEMINI_CLIENTS[i])
            _key_cursor = i
            return resp
        except Exception as exc:  # noqa: BLE001
            last = exc
            if _is_quota_error(exc):
                if n > 1:
                    log.warning("Gemini key #%d rate-limited; rotating to next key.", i + 1)
                continue
            raise
    raise QuotaExhausted(str(last))

# A war saga full of gods, death, and battle trips Gemini's default content
# filters, which would make the bot randomly refuse to discuss its own lore.
# Turn the filters off so it can talk about the story it was built for.
SAFETY = [
    types.SafetySetting(category=c, threshold=types.HarmBlockThreshold.BLOCK_NONE)
    for c in (
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
    )
]

# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

# This block is byte-identical and placed first in the system instruction for
# both Q&A and moderation, so Gemini's implicit caching can reuse the lore
# prefix across calls.
LORE_BLOCK = (
    "You are the Keeper of the Lore, the appointed loremaster and archivist of "
    "the world of THE WARBORN REALM, the complete saga written by BARUNN. The "
    "full text of the saga follows between the markers. It is the single source "
    "of truth about this world. Treat nothing outside it as canon.\n\n"
    "===== BEGIN THE WARBORN REALM: THE COMPLETE SAGA =====\n"
    f"{LORE_TEXT}\n"
    "===== END THE WARBORN REALM: THE COMPLETE SAGA =====\n"
)

# Short role intro for the LOCAL fallback path, where RAG supplies only the
# relevant passages instead of the whole saga. Goes in front of those passages.
KEEPER_PREAMBLE = (
    "You are the Keeper of the Lore, the appointed loremaster and archivist of the "
    "world of THE WARBORN REALM, the complete saga written by BARUNN. Only the passages "
    "provided below are canon to you here, and each is headed in brackets by the book "
    "and chapter it comes from. If they do not cover what is asked, say so in voice; "
    "never invent lore, names, or events."
)

QA_INSTRUCTIONS = (
    "A member of the community asks you about the lore. Answer as the Keeper: a "
    "storyteller with fire in the telling, in the grave, vivid, mythic voice of the "
    "saga. Give the tale energy and weight. Be dramatic, but never melodramatic; let "
    "the saga's own deeds carry the drama.\n"
    "- ANSWER FIRST, THEN TELL. If the asker poses a question (did this happen? is one "
    "alive? who did this?), your VERY FIRST words must be the plain answer, the yes or "
    "no and the heart of it. Do NOT open with a citation, a quote, or scene-setting; "
    "open with the answer, THEN tell the tale and bring in the quote and source to bear "
    "it out. Never bury the answer under story, never wander into tangents unasked.\n"
    "- LENGTH: keep it tight. About 900 to 1300 characters, two or three short "
    "paragraphs at most. A 'tell me about' may reach the fuller end; a yes/no question "
    "stays near the short end. ALWAYS finish your final sentence; never trail off "
    "mid-thought. Stop once the answer is given and borne out; do not pad.\n"
    "- STAY BOUND TO THE TEXT. Every event, name, and deed must come from the saga. "
    "NEVER invent to make the tale grander. If the saga is silent, say so in voice: "
    "'Of that, the tale does not speak.' The drama is in HOW you tell what is true, "
    "never in inventing what is not.\n"
    "- QUOTE THE SOURCE, as a priest quotes scripture: at least one line word for word "
    "in quotation marks, only what truly appears in the passages before you, never "
    "fabricated.\n"
    "- CITE WHERE IT COMES FROM, woven naturally into your prose, never as a raw tag. "
    "Each passage is headed by its source in parentheses. If it names a book and "
    "chapter, cite them (for example: 'so it is written in BOOK II: THE ANCIENT, "
    "Chapter 1: The City'). If it is COMMUNITY LORE established by a player, credit that "
    "person and give it NO book or chapter (for example: 'as set down by Barunn in the "
    "community annals'). NEVER print a bracketed tag such as '[Player-established lore]' "
    "or '[BOOK ... | Chapter ...]' in your answer.\n"
    "- TELL IT, do not list it. Even when the source is a plain document, a charter, or "
    "a list of terms, retell it as flowing narrative in the saga's mythic voice; never "
    "answer in bullet points or as a dry report.\n"
    "- Answer plainly within the telling; do not be cryptic. No meta preamble; begin."
)

MOD_INSTRUCTIONS = (
    "You are reviewing a single message a player posted in an in-character "
    "roleplay channel. Your ONLY job is to decide whether the message asserts "
    "something that directly CONTRADICTS the established canon of the saga above.\n\n"
    "Flag a contradiction ONLY when the message states, as fact about the world, "
    "something the saga makes impossible. Examples worth flagging:\n"
    "- A player claiming to BE a god, or to have a god's powers/status, when the "
    "saga's pantheon is fixed.\n"
    "- Claiming a character the saga says is dead is alive (or vice versa), or "
    "rewriting a major established event.\n"
    "- Inventing world-level canon that conflicts with the text (false history, "
    "false cosmology, impossible bloodlines).\n\n"
    "Do NOT flag, ever:\n"
    "- A player's own original character, action, dialogue, or feelings, as long "
    "as it does not rewrite world canon. Players are allowed to invent their own "
    "characters and stories.\n"
    "- Opinions, jokes, banter, questions, or anything out-of-character.\n"
    "- Ambiguous, vague, or merely unusual statements. When in doubt, do not flag.\n\n"
    "TWO RULES THAT PREVENT FALSE ALARMS:\n"
    "1. ABSENCE IS NOT CONTRADICTION. If the saga simply does not mention "
    "something (a character having a sibling, a town, a journey, a custom), that "
    "is NOT a contradiction. Players may freely add detail the saga is silent on. "
    "Only flag a message that conflicts with something the saga EXPLICITLY "
    "STATES.\n"
    "2. A SHARED NAME IS NOT A CLAIM. A player using a name that resembles a "
    "canon character is presumed to be playing their OWN separate character, even "
    "if a same-named figure in the saga is dead or has a different story. Do NOT "
    "flag someone merely for sharing a name with a canon character, or for giving "
    "their character a life the canon figure didn't have. Only flag if the "
    "message clearly claims to BE that specific canon figure, with their identity, "
    "history, or divine status.\n\n"
    "Be conservative. A false flag that scolds a player for legitimate roleplay is "
    "worse than missing one. If you are not clearly sure it breaks canon, set "
    "is_contradiction to false.\n\n"
    "Set 'confidence' to how sure you are that this is a genuine canon "
    "contradiction: 'certain' (the message plainly rewrites fixed canon), "
    "'high' (very likely a real contradiction), 'medium' (possible but arguable), "
    "or 'low' (probably fine). Reserve 'high' and 'certain' for clear cases.\n\n"
    "When you DO flag, write 'correction' as the Keeper of the Lore speaking "
    "directly to the player: brief (1-3 sentences), firm but not cruel, and "
    "grounded in what the saga actually says. Otherwise leave 'correction' empty."
)

ANALYSIS_INSTRUCTIONS = (
    "A player has submitted a new piece of lore to be added to the COMMUNITY CANON of "
    "THE WARBORN REALM. An admin will read your analysis and decide whether to approve "
    "it. Judge it against the established saga and any already-approved player lore "
    "above. Do not invent.\n"
    "ADDITIONS ARE NOT CONTRADICTIONS. Players are expected to ADD new things the saga "
    "never mentions: new factions, orders, colonies, characters, customs, documents, "
    "or events at the edges of the world. A new entity the saga simply does not mention "
    "(a new faction and its policies, a new character) is NOT a contradiction; it is a "
    "welcome addition. Likewise a faction's stated GOAL or opinion (for example wanting "
    "a world with fewer factions) is its ambition, not a claim that the world already "
    "is that way, so do not flag it.\n"
    "Only flag a CONTRADICTION when the submission rewrites or negates something the "
    "saga EXPLICITLY establishes: killing a character the saga keeps alive (or reviving "
    "a dead one), undoing a fixed event (the breaking of the Crown, the Melding of "
    "Worlds), claiming a fixed divine status, or denying an established law of the "
    "world.\n"
    "- 'contradictions': list ONLY genuine clashes with explicit canon, naming what "
    "each clashes with. If there are none, write exactly 'None.'\n"
    "- 'synopsis': in 2-4 sentences, explain how the submission slots into the wider "
    "lore: what it adds and where it sits among the realm's factions, powers, and "
    "timeline.\n"
    "- 'verdict': 'fits' if it only ADDS to the world or sits consistently with it "
    "(the common case for player factions and characters); 'minor_conflict' for small, "
    "easily-fixable clashes; 'major_conflict' ONLY if it directly rewrites or negates "
    "explicit established canon.\n"
    "Be precise and grounded, and speak plainly here, not in the mythic story-voice."
)


class Verdict(BaseModel):
    is_contradiction: bool
    confidence: Literal["low", "medium", "high", "certain"]
    reason: str  # internal note, not shown to the player
    correction: str  # in-character correction, or empty string


class Analysis(BaseModel):
    verdict: Literal["fits", "minor_conflict", "major_conflict"]
    contradictions: str  # specific clashes with canon/approved lore, or "None."
    synopsis: str  # how the submission fits with the rest of the lore


# Prefixes that mark a message as out-of-character; moderation skips these.
OOC_PREFIXES = ("((", "//", "[ooc", "(ooc", "!", "/", "((ooc")


def _player_block() -> str:
    """Approved community lore, appended AFTER the cached canon block (so the canon
    prefix stays byte-identical for Gemini's implicit caching). Empty if none yet."""
    texts = player_lore.approved_texts()
    if not texts:
        return ""
    body = "\n\n".join(texts)
    return (
        "\n\n===== PLAYER-ESTABLISHED LORE (community canon, approved by the keepers. "
        "It is secondary to the saga above and must NEVER override or contradict it; "
        "where they conflict, the saga wins) =====\n"
        + body
        + "\n===== END PLAYER-ESTABLISHED LORE =====\n"
    )


def _qa_system() -> str:
    return LORE_BLOCK + _player_block() + "\n\n" + QA_INSTRUCTIONS


def _mod_system() -> str:
    return LORE_BLOCK + _player_block() + "\n\n" + MOD_INSTRUCTIONS


def _analysis_system() -> str:
    return LORE_BLOCK + _player_block() + "\n\n" + ANALYSIS_INSTRUCTIONS


# --------------------------------------------------------------------------- #
# Model calls
# --------------------------------------------------------------------------- #


def answer_lore_question(question: str) -> str:
    """Directive (a): answer a lore question. Gemini (with key rotation) first,
    local RAG model as the fallback when every key is rate-limited or Gemini errors."""
    try:
        resp = _gemini_generate(lambda c: c.models.generate_content(
            model=QA_MODEL,
            contents=question,
            config=types.GenerateContentConfig(
                system_instruction=_qa_system(),
                # Generous cap + thinking OFF: gemini-2.5-flash otherwise spends the budget
                # on hidden thinking and truncates the visible answer mid-sentence. Length
                # is governed by the prompt, not this cap.
                max_output_tokens=1500,
                temperature=0.85,
                safety_settings=SAFETY,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        ))
        um = getattr(resp, "usage_metadata", None)
        if um:
            log.info(
                "Q&A[gemini]  prompt=%s cached=%s out=%s",
                um.prompt_token_count,
                getattr(um, "cached_content_token_count", None),
                um.candidates_token_count,
            )
        text = (resp.text or "").strip()
        if text:
            return text
        log.warning("Gemini returned an empty answer; trying local model.")
    except QuotaExhausted:
        log.warning("All Gemini keys exhausted; answering with the local model.")
    except Exception as exc:  # noqa: BLE001
        log.warning("Gemini Q&A failed (%s); answering with the local model.", exc)

    try:
        log.info("Q&A[local]  using %s over retrieved passages.", local_backend.OLLAMA_MODEL)
        text = local_backend.local_answer(LORE_TEXT, question, KEEPER_PREAMBLE, QA_INSTRUCTIONS)
        if text:
            return text
    except Exception:  # noqa: BLE001
        log.exception("Local Q&A failed")
    return "The records blur before me; ask again."


def check_message(text: str) -> dict | None:
    """Directive (b): classify a message. Gemini (with rotation) first, local model
    as the fallback. Returns the verdict dict, or None on error."""
    try:
        resp = _gemini_generate(lambda c: c.models.generate_content(
            model=MOD_MODEL,
            contents=(
                "Review this player message for canon contradictions:\n\n"
                f"<message>\n{text}\n</message>"
            ),
            config=types.GenerateContentConfig(
                system_instruction=_mod_system(),
                max_output_tokens=600,
                temperature=0,
                safety_settings=SAFETY,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                response_mime_type="application/json",
                response_schema=Verdict,
            ),
        ))
        parsed = getattr(resp, "parsed", None)
        if isinstance(parsed, Verdict):
            return parsed.model_dump()
        try:
            return json.loads(resp.text or "")
        except (json.JSONDecodeError, TypeError):
            log.warning("Could not parse Gemini moderation output: %r", (resp.text or "")[:200])
            return None
    except QuotaExhausted:
        log.warning("All Gemini keys exhausted; moderating with the local model.")
    except Exception as exc:  # noqa: BLE001 — never let a bad message crash the loop
        log.warning("Gemini moderation failed (%s); moderating with the local model.", exc)

    try:
        return local_backend.local_moderate(
            LORE_TEXT, text, KEEPER_PREAMBLE, MOD_INSTRUCTIONS, Verdict.model_json_schema()
        )
    except Exception:  # noqa: BLE001
        log.exception("Local moderation failed")
        return None


def analyze_submission(text: str) -> dict | None:
    """Analyse submitted player lore: contradictions + synopsis for an admin to read.
    Gemini (with rotation) first, local model as fallback. Returns the dict or None."""
    try:
        resp = _gemini_generate(lambda c: c.models.generate_content(
            model=QA_MODEL,
            contents=f"Evaluate this submitted lore:\n\n{text}",
            config=types.GenerateContentConfig(
                system_instruction=_analysis_system(),
                max_output_tokens=1200,
                temperature=0.2,
                safety_settings=SAFETY,
                response_mime_type="application/json",
                response_schema=Analysis,
            ),
        ))
        parsed = getattr(resp, "parsed", None)
        if isinstance(parsed, Analysis):
            return parsed.model_dump()
        try:
            return json.loads(resp.text or "")
        except (json.JSONDecodeError, TypeError):
            return None
    except QuotaExhausted:
        log.warning("All Gemini keys exhausted; analysing submission with the local model.")
    except Exception as exc:  # noqa: BLE001
        log.warning("Gemini analysis failed (%s); analysing with the local model.", exc)

    try:
        return local_backend.local_analyze(
            LORE_TEXT, text, KEEPER_PREAMBLE, ANALYSIS_INSTRUCTIONS, Analysis.model_json_schema()
        )
    except Exception:  # noqa: BLE001
        log.exception("Local analysis failed")
        return None


def refresh_player_index() -> None:
    """Push approved player lore into the local RAG index (so local 'remembers' it too)."""
    try:
        local_backend.set_player_lore(player_lore.approved_texts())
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not refresh local player-lore index (Ollama down?): %s", exc)


# --------------------------------------------------------------------------- #
# Discord client
# --------------------------------------------------------------------------- #

intents = discord.Intents.default()
intents.message_content = True  # required for moderation + @mention questions


class KeeperClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Register the review buttons as a persistent view so they keep working
        # after a restart (static custom_ids; the pending record is found by message id).
        self.add_view(ReviewView())
        if GUILD_IDS:
            for gid in GUILD_IDS:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            log.info("Synced slash commands instantly to guild(s): %s", GUILD_IDS)
        else:
            await self.tree.sync()
            log.info("Synced slash commands globally (new ones can take up to ~1h to appear).")


client = KeeperClient()


@client.event
async def on_ready():
    log.info("Keeper of the Lore is awake as %s", client.user)
    log.info("Gemini keys: %d (rotate on quota) -> local fallback: %s",
             len(GEMINI_KEYS), local_backend.OLLAMA_MODEL)
    for g in client.guilds:
        log.info("Connected to server: %s  (GUILD_ID = %s)", g.name, g.id)
    if not GUILD_IDS:
        log.info("Tip: put one of the GUILD_ID values above into .env to make new "
                 "slash commands appear instantly instead of waiting on global sync.")
    if MOD_CHANNELS:
        log.info("Watching %d channel(s) for canon breaks.", len(MOD_CHANNELS))
    else:
        log.info("No MOD_CHANNELS set — lore-police is off; Q&A only.")

    napproved = len(player_lore.approved_entries())
    if napproved:
        log.info("Remembering %d piece(s) of approved player lore.", napproved)

    # Warm the local RAG index (+ player lore) in the background so the first fallback is instant.
    async def _warm():
        if await client.loop.run_in_executor(None, local_backend.available):
            await client.loop.run_in_executor(None, local_backend.warm, LORE_TEXT)
            await client.loop.run_in_executor(None, refresh_player_index)
        else:
            log.warning("Ollama unreachable at %s — local fallback is OFFLINE until it is up.",
                        local_backend.OLLAMA_HOST)
    client.loop.create_task(_warm())


@client.tree.command(name="lore", description="Ask the Keeper of the Lore about the Warborn Realm.")
@app_commands.describe(question="What do you wish to know?")
async def lore_command(interaction: discord.Interaction, question: str):
    await interaction.response.defer(thinking=True)
    try:
        answer = await client.loop.run_in_executor(None, answer_lore_question, question)
    except Exception:  # noqa: BLE001 — surface to the user, log the detail
        log.exception("Q&A failed")
        await interaction.followup.send(
            "The archive is sealed to me for the moment. Try again shortly."
        )
        return
    await interaction.followup.send(answer[:2000])


# --------------------------------------------------------------------------- #
# Player lore submissions + admin review
# --------------------------------------------------------------------------- #

VERDICT_COLORS = {
    "fits": discord.Color.green(),
    "minor_conflict": discord.Color.gold(),
    "major_conflict": discord.Color.red(),
}


def _is_admin(interaction: discord.Interaction) -> bool:
    """Server owner, or anyone holding Discord's Administrator permission."""
    if interaction.guild is None:
        return False
    if interaction.user.id == interaction.guild.owner_id:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


def _has_authority(interaction: discord.Interaction) -> bool:
    """Who may approve/reject lore: the owner, ANY Administrator, or a role the owner
    has explicitly designated as an approver."""
    if interaction.guild is None:
        return False
    if _is_admin(interaction):
        return True
    role_id = player_lore.get_approver_role(interaction.guild.id)
    if role_id and isinstance(interaction.user, discord.Member):
        return any(r.id == role_id for r in interaction.user.roles)
    return False


def _review_embed(author_label: str, lore: str, analysis: dict | None,
                  status: str | None = None) -> discord.Embed:
    verdict = (analysis or {}).get("verdict")
    color = VERDICT_COLORS.get(verdict, discord.Color.blurple())
    embed = discord.Embed(title="📜 Lore Submission", description=lore[:4000], color=color)
    embed.add_field(name="Submitted by", value=author_label, inline=False)
    if analysis:
        embed.add_field(
            name=f"Keeper's reading — verdict: {verdict or '?'}",
            value=(analysis.get("synopsis") or "—")[:1024], inline=False)
        embed.add_field(
            name="Contradictions",
            value=(analysis.get("contradictions") or "None.")[:1024], inline=False)
    else:
        embed.add_field(name="Keeper's reading", value="(analysis unavailable)", inline=False)
    if status:
        embed.add_field(name="Status", value=status, inline=False)
    return embed


class ReviewView(discord.ui.View):
    """Persistent Approve/Reject buttons. A single registered instance serves every
    review message; the specific submission is found from the message id."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="keeper:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_decision(interaction, approve=True)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="keeper:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_decision(interaction, approve=False)


async def _handle_decision(interaction: discord.Interaction, approve: bool):
    rec = player_lore.get_pending(interaction.message.id)
    if rec is None:
        await interaction.response.send_message("This submission is no longer pending.", ephemeral=True)
        return
    if not _has_authority(interaction):
        await interaction.response.send_message(
            "Only the highest keepers may approve or reject lore.", ephemeral=True)
        return

    player_lore.remove_pending(interaction.message.id)
    actor = interaction.user

    if approve:
        player_lore.add_approved({
            "id": rec.get("sid", uuid.uuid4().hex[:8]),
            "author_id": rec["author_id"],
            "author_name": rec["author_name"],
            "text": rec["text"],
            "approved_by": getattr(actor, "display_name", str(actor)),
            "guild_id": rec.get("guild_id"),
            "ts": time.time(),
        })
        await client.loop.run_in_executor(None, refresh_player_index)
        status = f"✅ Approved by {actor.mention} — now remembered as community canon."
    else:
        status = f"❌ Rejected by {actor.mention}."

    embed = _review_embed(rec["author_name"], rec["text"], rec.get("analysis"), status=status)
    disabled = ReviewView()
    for child in disabled.children:
        child.disabled = True
    await interaction.response.edit_message(embed=embed, view=disabled)

    try:  # tell the submitter privately; ignore if their DMs are closed
        user = await client.fetch_user(rec["author_id"])
        await user.send("Your lore was approved and is now woven into the realm's memory."
                        if approve else "Your lore submission was not accepted this time.")
    except Exception:  # noqa: BLE001
        pass


@client.tree.command(name="submit_lore",
                     description="Submit your own lore about the Warborn Realm for the keepers to judge.")
@app_commands.describe(lore="The lore you wish to add to the realm.")
async def submit_lore(interaction: discord.Interaction, lore: str):
    if interaction.guild is None:
        await interaction.response.send_message("Submit from within the server.", ephemeral=True)
        return
    review_id = player_lore.get_review_channel(interaction.guild.id)
    if not review_id:
        await interaction.response.send_message(
            "No review channel is set yet. An admin must run /keeper_set_review_channel first.",
            ephemeral=True)
        return
    review_ch = interaction.guild.get_channel(review_id)
    if not isinstance(review_ch, discord.TextChannel):
        await interaction.response.send_message(
            "The review channel is missing; ask an admin to set it again.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    analysis = await client.loop.run_in_executor(None, analyze_submission, lore)
    embed = _review_embed(interaction.user.mention, lore, analysis)
    msg = await review_ch.send(embed=embed, view=ReviewView())
    player_lore.add_pending(msg.id, {
        "sid": uuid.uuid4().hex[:8],
        "author_id": interaction.user.id,
        "author_name": interaction.user.display_name,
        "guild_id": interaction.guild.id,
        "text": lore,
        "analysis": analysis or {},
        "created": time.time(),
    })
    await interaction.followup.send(
        "Your lore has been carried to the keepers for judgment.", ephemeral=True)


@client.tree.command(name="keeper_set_approver",
                     description="Set an extra role allowed to approve/reject lore (owner or admin only).")
@app_commands.describe(role="An extra role that may approve lore, on top of admins.")
async def keeper_set_approver(interaction: discord.Interaction, role: discord.Role):
    if not _is_admin(interaction):
        await interaction.response.send_message(
            "Only the server owner or an administrator may set the approver role.", ephemeral=True)
        return
    player_lore.set_approver_role(interaction.guild.id, role.id)
    await interaction.response.send_message(
        f"Approver role set to {role.mention}. The owner, anyone with Administrator, and that role "
        "may approve lore.", ephemeral=True)


@client.tree.command(name="keeper_set_review_channel",
                     description="Set the channel where lore submissions are reviewed (owner or admin only).")
@app_commands.describe(channel="Channel where submissions are posted for approval.")
async def keeper_set_review_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not _is_admin(interaction):
        await interaction.response.send_message(
            "Only the server owner or an administrator may set the review channel.", ephemeral=True)
        return
    player_lore.set_review_channel(interaction.guild.id, channel.id)
    await interaction.response.send_message(
        f"Review channel set to {channel.mention}. Submissions will appear there for approval.",
        ephemeral=True)


@client.event
async def on_message(message: discord.Message):
    # Never react to ourselves or other bots.
    if message.author.bot:
        return

    content = (message.content or "").strip()

    # --- @mention question (works anywhere) ---
    if client.user in message.mentions:
        question = content
        for mention in (f"<@{client.user.id}>", f"<@!{client.user.id}>"):
            question = question.replace(mention, "")
        question = question.strip()
        if question:
            async with message.channel.typing():
                try:
                    answer = await client.loop.run_in_executor(
                        None, answer_lore_question, question
                    )
                except Exception:  # noqa: BLE001
                    log.exception("Mention Q&A failed")
                    answer = "The archive is sealed to me for the moment. Try again shortly."
            await message.reply(answer[:2000], mention_author=False)
        return  # a mention is a question, not something to police

    # --- lore moderation (only in watched channels) ---
    if message.channel.id not in MOD_CHANNELS:
        return
    if len(content) < MOD_MIN_LENGTH:
        return
    if content.lower().startswith(OOC_PREFIXES):
        return

    verdict = await client.loop.run_in_executor(None, check_message, content)
    if not verdict:
        return

    confidence = str(verdict.get("confidence", "low")).lower()
    confident_enough = (
        confidence in CONFIDENCE_LEVELS
        and CONFIDENCE_LEVELS.index(confidence)
        >= CONFIDENCE_LEVELS.index(MOD_MIN_CONFIDENCE)
    )
    if verdict.get("is_contradiction") and confident_enough and verdict.get("correction"):
        log.info(
            "Flagged msg from %s (conf=%s): %s",
            message.author,
            confidence,
            verdict.get("reason", ""),
        )
        await message.reply(verdict["correction"][:2000], mention_author=True)


if __name__ == "__main__":
    client.run(DISCORD_TOKEN, log_handler=None)
