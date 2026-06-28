# 🐺 Word Wolf / Imposter — Telegram Party Game Bot

A fully-featured **Word Wolf / Imposter** party game bot for Telegram group chats, written in Python using `python-telegram-bot` v20.

---

## Quick Start

### 1. Create a Bot via @BotFather

1. Open Telegram and search for [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts to name your bot.
3. Copy the **API token** BotFather gives you.

### 2. Get Your Token

Paste the token into a `.env` file in the project directory:

```bash
cp .env.example .env
# Edit .env and set TELEGRAM_BOT_TOKEN=your_actual_token
```

### 3. Install Dependencies

Python 3.11+ is required.

```bash
pip install -r requirements.txt
```

### 4. Run the Bot

```bash
python bot.py
```

The bot will start polling Telegram for updates. Add it to a group chat and use `/newgame` to get started.

---

## How to Play

1. Add the bot to a group chat.
2. **Everyone** who wants to play must first DM the bot directly (search its username) — this is required so the bot can send private role messages.
3. One player runs `/newgame` in the group to open a lobby.
4. Other players use `/join` to enter the lobby.
5. The host uses `/settings` to configure the game (optional) and `/begin` to start.
6. Each player is privately messaged their **secret word** and **role**.
7. The game runs **3 rounds** of one-word hints from each player in turn.
8. After round 3: a **discussion phase**, then a **vote** on who the Imposter is.
9. If caught, the Imposter gets one chance to **guess the civilians' word** to steal the win.
10. Results, recap, and awards are posted to the group.

---

## Difficulty Modes

### Easy Mode (default)
- The **Imposter is told** they are the Imposter via DM.
- For **Round 1 only**, the Imposter receives a "blend-in hint word" (the decoy word) — a plausible alternative they can use to seem normal in round 1.
- Rounds 2 and 3: the Imposter is on their own and must bluff based on what they've heard from others.

### Hard Mode
- The **Imposter does NOT know** they are the Imposter.
- Instead, they receive a slightly different "decoy word" (e.g. majority = Coffee, decoy = Tea) and genuinely play as if it's their real word.
- The Imposter's accidental "off" hints are what civilians must detect.
- This creates a more organic and surprising game — the Imposter is genuinely confused by the vote!

---

## Commands Reference

| Command | Who | Description |
|---|---|---|
| `/newgame` | Anyone | Opens a lobby (auto-cancels after 5 min) |
| `/join` | Anyone | Join the current lobby |
| `/settings` | Host | Configure mode, category, imposters, timer |
| `/customcategory` | Host | Set a custom word pair list |
| `/begin` | Host | Start the game (min 3 players) |
| `/kick @user` | Host | Remove a player from lobby or game |
| `/pause` | Host | Pause all active timers |
| `/resume` | Host | Resume paused timers |
| `/endgame` | Host | Force-end the game immediately |
| `/rematch` | Anyone | Replay with the same group |
| `/score` or `/leaderboard` | Anyone | Show chat leaderboard |
| `/help` | Anyone | Show rules and command list |

---

## Custom Category Format

The host can supply their own word pairs with `/customcategory`. Send the pairs right after the command, one per line, in the format:

```
/customcategory Apple | Pear
Mountain | Hill
Guitar | Ukulele
```

- Each line: `MajorityWord | DecoyWord`
- At least 2 pairs required.
- Words can be hyphenated (e.g. `Ice-cream`) but must not contain unquoted spaces as part of a single word.
- The custom list is only active for the current session.

---

## Tie-Break Rule

If the vote results in a tie between two or more players:
1. A **re-vote** is held, but this time only the tied players' names appear as options. All active players vote again.
2. If the re-vote is **still tied**, Civilians lose by default (Imposters win).

---

## Multiple Imposters Mode

Enable via `/settings → Imposters`. Suggested: ~1 imposter per 4–5 players.

- In this mode, the group is **told how many imposters** there are, but not who they are.
- Voting becomes **multi-select**: each player picks as many suspects as there are imposters.
- Civilians win only if their selected set **exactly matches** the real imposter set.
- The **guess-back phase is skipped** in multi-imposter mode (too complex to adjudicate fairly).

---

## Persistence & Crash Recovery

### What is saved
The bot uses **SQLite** (`wordwolf.db`) for two things:

1. **Leaderboard data** — permanent per-chat scores that survive bot restarts indefinitely.
2. **Active game state snapshots** — the full game state is written to the DB after every significant action (join, hint, vote, etc.).

### How recovery works
When the bot starts up, it reads all saved game states from `game_state` table and restores them into memory. For any chat with an in-progress game, it sends a recovery announcement and the game can continue.

### Recovery limits
- **In-flight timers** (hint countdown, discussion timer, vote timer) are **not** resumed — they restart fresh when the next action occurs.
- If the bot crashes mid-vote, players may need to vote again (voting state is restored but the timer restarts).
- If nobody does anything after a restart, the host can use `/endgame` and `/newgame` to start fresh.
- The recovery relies on the `wordwolf.db` file being present in the same directory. If the DB is deleted, all in-progress games and leaderboard data are lost.

---

## Project Structure

```
wordwolf/
├── bot.py           # Entry point, Telegram handlers, async orchestration
├── game.py          # State machine, game logic, role assignment, validation
├── db.py            # SQLite persistence (leaderboard + game state snapshots)
├── words.json       # Word bank (7 categories, 24+ pairs each)
├── requirements.txt
├── .env.example
└── README.md
```

---

## Leaderboard Stats Tracked

Per user, per chat:
- Games played
- Wins/losses as Civilian
- Wins/losses as Imposter
- Times correctly identified as Imposter
- Times successfully guessed back (Imposter stole the win after being caught)

---

## Notes & Design Decisions

- **Rate limiting**: Commands are rate-limited to 1 per 2 seconds per user to prevent accidental spam.
- **AFK handling**: Players who don't submit a hint in 30 seconds are skipped for that round. Two consecutive skips = auto-removal from the game.
- **Host transfer**: If the host is auto-removed (AFK), host privileges automatically transfer to the earliest-joined active player.
- **DM requirement**: Players must have DM'd the bot before joining so role messages can be delivered privately.
- **Single file DB**: The SQLite DB (`wordwolf.db`) is created automatically on first run in the working directory.
