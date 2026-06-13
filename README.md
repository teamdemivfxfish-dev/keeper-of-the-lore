# Keeper of the Lore

A Discord bot that knows *The Warborn Realm: The Complete Saga* by BARUNN, and does three things:

- **Answers lore questions** in the saga's voice. `/lore who is Luminara?` or just `@Keeper of the Lore <question>`. It answers from the saga only, quotes it and cites the book/chapter, and admits when the records are silent instead of inventing things.
- **Polices canon** (optional). In channels you designate, when a player clearly breaks the lore (declaring themselves a god, reviving a dead character, rewriting world history), the Keeper replies in character with a correction. It is deliberately conservative and stays quiet unless it is sure.
- **Accepts player lore.** Players run `/submit_lore`; the bot analyses it (contradictions + how it fits) and posts it for an admin to **Approve/Reject** with buttons. Approved lore becomes part of the bot's memory, kept separate from the original saga.

## How it answers (backends + failover)

1. **Google Gemini** (`gemini-2.5-flash` by default) is the primary brain. The whole saga is sent as the system instruction so it reasons over the entire story at once.
2. If a Gemini key is rate-limited, the bot **rotates** to the next key (`GEMINI_API_KEYS`).
3. If every key is exhausted or Gemini is unreachable, it **falls back to a local model** via [Ollama](https://ollama.com) (default `qwen2.5:7b`), using retrieval (RAG) so a small model stays fast. This makes the bot free and resilient even when the cloud quota runs out.

Approved player lore is stored in `keeper_state.json` (never in `lore.txt`) and is fed to both backends, so the bot "remembers" community canon without touching the original saga.

---

## Setup (run it on a server)

### 1. Install Python 3.10+
Windows: https://python.org/downloads — tick **"Add Python to PATH"** during install.
Linux: `sudo apt install python3 python3-venv python3-pip`

### 2. Create the Discord bot
- Go to https://discord.com/developers/applications → **New Application**.
- **Bot** tab → **Reset Token** → copy it. That is your `DISCORD_TOKEN`.
- Still on the **Bot** tab, under **Privileged Gateway Intents**, turn **ON** **Message Content Intent**. (Without it the bot can't read @mentions or moderate.)
- **Installation** (or **OAuth2 → URL Generator**) tab → scopes **`bot`** and **`applications.commands`**; bot permissions: **View Channels**, **Send Messages**, **Read Message History**. Use the generated URL to invite the bot to the server.

### 3. Get a Gemini API key
https://aistudio.google.com/apikey → that is your `GEMINI_API_KEY`. (The free tier has daily/per-minute limits; when they run out, the bot uses the local model if Ollama is set up — see step 6.)

### 4. Configure `.env`
Copy `.env.example` to `.env` and fill it in. The minimum is:
```
DISCORD_TOKEN=your-discord-bot-token
GEMINI_API_KEY=your-gemini-key
GUILD_ID=your-server-id        # makes slash commands appear instantly
```
To find your server ID: Discord → Settings → Advanced → **Developer Mode ON**, then right-click the server icon → **Copy Server ID**. (The bot also prints every server's ID in its log on startup.)

### 5. Install dependencies and run
**Windows:** double-click **`Start-Keeper.bat`** (it builds a virtual environment and installs everything on first run).

**Linux/Mac:**
```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python keeper_of_the_lore.py
```
The bot is online only while the script is running. To keep it up 24/7 on a Linux server, run it under `systemd`, `screen`/`tmux`, or `pm2`.

### 6. (Optional but recommended) Local fallback with Ollama
So the bot keeps working when the Gemini free quota runs out:
- Install Ollama: https://ollama.com/download
- Pull the models:
  ```sh
  ollama pull qwen2.5:7b
  ollama pull nomic-embed-text
  ```
- That's it. The bot auto-detects Ollama at `http://localhost:11434`. If Ollama isn't running it simply logs that the fallback is offline and stays cloud-only.
- Lighter hardware? Set `OLLAMA_MODEL=qwen2.5:3b` in `.env` (faster, a bit less eloquent).

### 7. Finish setup inside Discord
Run these once as the **server owner or an administrator**:
- `/keeper_set_review_channel channel:#your-review-channel` — where `/submit_lore` submissions are posted for approval.
- `/keeper_set_approver role:@SomeRole` — *optional*, lets a specific non-admin role approve lore too. (The owner and anyone with **Administrator** can already approve without this.)

---

## Commands

| Command | Who | What |
|---|---|---|
| `/lore <question>` or `@Keeper <question>` | anyone | Ask about the saga. |
| `/submit_lore <text>` | anyone | Submit your own lore for review. |
| `/keeper_set_review_channel <channel>` | owner / admin | Where submissions appear. |
| `/keeper_set_approver <role>` | owner / admin | Extra role allowed to approve. |

**Who can approve/reject lore:** the server owner, anyone with the **Administrator** permission, and (if set) the designated approver role.

---

## Configuration reference (`.env`)

| Key | Default | Meaning |
|---|---|---|
| `DISCORD_TOKEN` | — | Discord bot token (required). |
| `GEMINI_API_KEY` | — | Primary Gemini key (required). |
| `GEMINI_API_KEYS` | empty | Extra keys to rotate on quota, comma-separated. Only adds quota if on **separate Google projects**. |
| `GUILD_ID` | empty | Server ID(s) for instant slash-command sync. Empty = global (can take ~1h). |
| `MODEL` | `gemini-2.5-flash` | Q&A model. |
| `MOD_MODEL` | `gemini-2.5-flash` | Moderation model (keep on a Flash model). |
| `MOD_CHANNELS` | empty | Channel IDs the lore-police watches. Empty = Q&A only. |
| `MOD_MIN_CONFIDENCE` | `high` | `low`/`medium`/`high`/`certain` — higher = quieter. |
| `MOD_MIN_LENGTH` | `25` | Ignore messages shorter than this. |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Local fallback model. |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Local embedding model (for RAG). |
| `OLLAMA_HOST` | `http://localhost:11434` | Where Ollama is served. |
| `RAG_TOP_K` | `8` | How many lore passages the local model sees per question. |
| `OLLAMA_NUM_CTX` | `8192` | Context window for the local model. |

---

## Updating the lore

Replace `lore.txt` with new text and restart. To regenerate it from a PDF (needs `pip install pymupdf`):
```sh
python -c "import fitz; d=fitz.open('The Warborn Realm - Complete Saga.pdf'); open('lore.txt','w',encoding='utf-8').write('\n'.join(p.get_text() for p in d))"
```
The local search index rebuilds automatically when `lore.txt` changes.

## Tuning the Keeper's voice and rules

All the prompts live near the top of `keeper_of_the_lore.py`, edit and restart:
- `QA_INSTRUCTIONS` — how it answers questions (length, voice, quoting, citing).
- `MOD_INSTRUCTIONS` — what counts as a canon contradiction (players' own characters are explicitly protected).
- `ANALYSIS_INSTRUCTIONS` — how it judges `/submit_lore` submissions (new additions are allowed; only real contradictions are flagged).

## Files

| File | Purpose |
|---|---|
| `keeper_of_the_lore.py` | The bot: Discord wiring, Q&A, moderation, submissions. |
| `local_backend.py` | Local Ollama fallback + RAG retrieval. |
| `player_lore.py` | Storage for approved community lore + per-server config. |
| `lore.txt` | The full saga (the bot's source of truth). |
| `requirements.txt` | Python dependencies. |
| `.env.example` | Config template — copy to `.env` and fill in. |
| `Start-Keeper.bat` | Windows launcher (builds the venv on first run). |

> **Not in this repo (on purpose):** your live `.env` (secrets), the `.venv/`, the cache files, and the Ollama models. The `.env` must be created on each machine that runs the bot; the models are downloaded by Ollama.
