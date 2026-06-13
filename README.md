# Keeper of the Lore

A Discord bot that knows *The Warborn Realm: The Complete Saga* by BARUNN cold, and does two things:

- **a) Answers lore questions.** `/lore how many gods are still alive?` or just `@Keeper of the Lore <question>`. It answers from the saga only, and admits when the records are silent rather than inventing things.
- **b) Polices canon.** In channels you designate, when a player writes something that clearly breaks the lore (declaring themselves a god, reviving a dead character, rewriting world history), the Keeper replies in character with the correction. It is deliberately conservative: it stays quiet unless it is sure, so legitimate roleplay is not nagged.

The whole saga (~53K tokens) is fed to the model on every call as the system instruction, so the bot reasons over the entire story at once. No vector database, no chunking, no "the answer wasn't in the retrieved snippet" misses.

**Backend:** Google Gemini (`gemini-2.5-flash` by default). Gemini 2.5's implicit context caching automatically discounts the repeated lore, so re-sending it every call is cheap.

## Setup

1. **Install Python 3.10+** if you don't have it.

2. **Create the Discord bot:**
   - Go to https://discord.com/developers/applications → New Application.
   - **Bot** tab → Reset Token → copy it. That's your `DISCORD_TOKEN`.
   - Still on the **Bot** tab, scroll to **Privileged Gateway Intents** and turn ON **Message Content Intent**. The bot cannot read messages for moderation or answer @mentions without it.
   - **Installation** (or **OAuth2 → URL Generator**) tab → scopes `bot` and `applications.commands`; bot permissions: **Read Messages/View Channels**, **Send Messages**, **Read Message History**. Use the generated URL to invite the bot to your server.

3. **Get a Google AI Studio API key** at https://aistudio.google.com/apikey → that's your `GEMINI_API_KEY`.

4. **Configure:** copy `.env.example` to `.env` and fill in the values. To enable the lore-police, put the channel IDs to watch in `MOD_CHANNELS` (turn on Developer Mode in Discord, right-click a channel → Copy Channel ID). Leave it empty to run Q&A only.

5. **Run it:** double-click `Start-Keeper.bat` (it creates a virtual environment and installs dependencies on first run), or manually:
   ```sh
   pip install -r requirements.txt
   python keeper_of_the_lore.py
   ```

The bot is online only while the script is running.

## Updating the lore

Replace `lore.txt` with new text and restart. To regenerate it from a PDF:
```sh
python -c "import fitz; d=fitz.open('The Warborn Realm - Complete Saga.pdf'); open('lore.txt','w',encoding='utf-8').write('\n'.join(p.get_text() for p in d))"
```
(needs `pip install pymupdf`).

## Cost notes — read this

The default model, `gemini-2.5-flash`, is cheap and has a generous free tier, so for a typical server you may pay little or nothing. The lore is re-sent every call, but Gemini 2.5's implicit caching discounts that repeated text.

**The moderation side runs the model on (almost) every message in watched channels.** That's the main cost driver if a channel is busy. Two levers:

- `MOD_MIN_CONFIDENCE` (default `high`; one of `low`/`medium`/`high`/`certain`) and `MOD_MIN_LENGTH` (default `25`) — raise these to make the bot quieter and skip more messages.
- Watch fewer / less active channels via `MOD_CHANNELS`.

For sharper Q&A reasoning, set `MODEL=gemini-2.5-pro` in `.env`. Keep `MOD_MODEL` on a Flash model — Pro can't disable thinking, which the moderator relies on for speed.

The bot also ignores other bots, itself, out-of-character messages (lines starting with `((`, `//`, `[ooc`, `/`, `!`), and anything below the length floor.

## Tuning the lore-police

If it is too trigger-happy or too quiet, the behavior lives in `MOD_INSTRUCTIONS` near the top of `keeper_of_the_lore.py` — that prompt defines exactly what counts as a contradiction and what must never be flagged (players' own original characters and actions are explicitly protected). Edit it and restart.
