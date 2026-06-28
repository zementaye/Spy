"""
bot.py — Word Wolf / Imposter Telegram Bot entry point.

Run: python bot.py

State machine phases:
  IDLE → LOBBY → HINT_ROUND[1..3] → DISCUSSION → VOTING
       → [RE_VOTE] → [GUESS_BACK] → REVEAL → IDLE / REMATCH_LOBBY
"""

import asyncio
import logging
import os
import re
from typing import Optional

from dotenv import load_dotenv
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
import game as G
from game import (
    Difficulty,
    GameSettings,
    GameState,
    Phase,
    Player,
    CATEGORIES,
    TOTAL_ROUNDS,
    TURN_TIMEOUT,
    LOBBY_TIMEOUT,
    DISCUSSION_DEFAULT,
    VOTE_TIMEOUT,
    MIN_PLAYERS,
)

load_dotenv()
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

async def send_group(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, parse_mode=ParseMode.HTML, **kwargs) -> None:
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, **kwargs)


def mention(player: Player) -> str:
    """Safe HTML mention that works regardless of username special chars."""
    name = player.display().replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={player.user_id}">{name}</a>'


async def send_dm(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str, **kwargs) -> None:
    try:
        await context.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.MARKDOWN, **kwargs)
    except Exception as e:
        logger.warning(f"Could not DM user {user_id}: {e}")


def get_player_display(update: Update) -> tuple[int, str, str]:
    user = update.effective_user
    return user.id, user.username or "", user.first_name


def cancel_task(context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    task: Optional[asyncio.Task] = context.chat_data.get(key)
    if task and not task.done():
        task.cancel()


def schedule_task(context: ContextTypes.DEFAULT_TYPE, key: str, coro) -> asyncio.Task:
    cancel_task(context, key)
    task = asyncio.create_task(coro)
    context.chat_data[key] = task
    return task


# ── DM check ─────────────────────────────────────────────────────────────────

async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Returns True if user is a Telegram group admin or creator."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def is_host_or_admin(bot: Bot, chat_id: int, user_id: int, gs: GameState) -> bool:
    return gs.is_host(user_id) or await is_admin(bot, chat_id, user_id)


async def has_dmed_bot(bot: Bot, user_id: int) -> bool:
    """Check if bot can DM this user by attempting to send a message."""
    try:
        await bot.send_message(chat_id=user_id, text="✅ Bot DM confirmed — you can join games!")
        return True
    except Exception:
        return False
    try:
        await bot.send_message(chat_id=user_id, text="✅ Bot DM confirmed — you can join games!")
        return True
    except Exception:
        return False


# ── /help ─────────────────────────────────────────────────────────────────────

HELP_TEXT = """
🐺 *Word Wolf / Imposter — Commands*

/newgame — Open a lobby (host)
/join — Join the current lobby
/settings — Configure game options (host)
/customcategory — Set a custom word list (host)
/begin — Start the game (host, min 3 players)
/kick @user — Remove a player (host)
/pause / /resume — Pause/resume timers (host)
/endgame — Force-end the game (host)
/rematch — Replay with same group
/score — Show leaderboard
/help — This message

*How to play:*
One player is the secret Imposter. On your turn, type your hint word followed by *!* (e.g. `cold!`). Only messages ending with ! count as hints — you can chat normally otherwise.
3 rounds of hints, then vote — who's the Imposter?

*Easy Mode*: Imposter knows their role + gets a hint word for round 1.
*Hard Mode*: Imposter doesn't know — they get a similar decoy word instead.

Before joining, you must DM this bot once so it can message you privately.
"""

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


# ── /newgame ──────────────────────────────────────────────────────────────────

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id, username, first_name = get_player_display(update)

    gs = G.get_game(chat_id)
    if gs and gs.phase not in (Phase.IDLE, Phase.REVEAL):
        await update.message.reply_text("⚠️ A game is already running. Use /endgame first.")
        return

    if not gs or gs.phase in (Phase.IDLE, Phase.REVEAL):
        gs = GameState(chat_id=chat_id, host_id=user_id)
        gs.phase = Phase.LOBBY
        gs.lobby_start = __import__("time").time()
        G.set_game(gs)
        gs.save()

    # Auto-add host to lobby
    host_player = Player(user_id=user_id, username=username, first_name=first_name)
    gs.players[user_id] = host_player
    gs.join_order = [user_id]

    await send_group(context, chat_id,
        f"🐺 <b>Word Wolf lobby opened!</b>\n"
        f"Host: {host_player.display()}\n\n"
        f"Players: 1 joined.\n"
        f"Use /join to join. Host uses /settings to configure, then /begin to start.\n"
        f"<i>(Lobby auto-cancels in 5 minutes if /begin isn't used)</i>"
    )

    # Lobby auto-cancel timer
    schedule_task(context, "lobby_timer", _lobby_timeout(context, chat_id))


async def _lobby_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    await asyncio.sleep(LOBBY_TIMEOUT)
    gs = G.get_game(chat_id)
    if gs and gs.phase in (Phase.LOBBY, Phase.REMATCH_LOBBY):
        gs.phase = Phase.IDLE
        G.remove_game(chat_id)
        await send_group(context, chat_id,
            "⏰ Lobby timed out — no game was started. Use /newgame to try again."
        )


# ── /join ─────────────────────────────────────────────────────────────────────

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id, username, first_name = get_player_display(update)

    gs = G.get_game(chat_id)
    if not gs or gs.phase not in (Phase.LOBBY,):
        await update.message.reply_text("No open lobby. Use /newgame to start one.")
        return

    if not gs.check_rate_limit(user_id):
        return

    if user_id in gs.players:
        await update.message.reply_text(
            f"You're already in the lobby, {first_name}! 😄"
        )
        return

    # Check max players
    if len(gs.players) >= 10 and not gs.settings.num_imposters > 1:
        await update.message.reply_text("Lobby is full (max 10 players).")
        return

    # Check DM capability
    dm_ok = await has_dmed_bot(context.bot, user_id)
    if not dm_ok:
        await update.message.reply_text(
            f"⚠️ {first_name}, please DM @{(await context.bot.get_me()).username} first "
            f"(just send /start or any message), then try /join again.\n"
            f"The bot needs to message you privately during the game."
        )
        return

    player = Player(user_id=user_id, username=username, first_name=first_name)
    gs.players[user_id] = player
    gs.join_order.append(user_id)
    gs.save()

    # Show full updated player list
    names = "\n".join(
        f"  {i+1}. {p.display()}"
        for i, p in enumerate(gs.active_players())
    )
    await send_group(context, chat_id,
        f"✅ <b>{player.display()} joined!</b>\n\n"
        f"<b>Players in lobby ({len(gs.players)}):</b>\n{names}\n\n"
        f"<i>{MIN_PLAYERS} minimum to start. Host uses /begin when ready.</i>",
        parse_mode=ParseMode.HTML,
    )


# ── /settings ────────────────────────────────────────────────────────────────

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    gs = G.get_game(chat_id)

    if not gs or gs.phase not in (Phase.LOBBY,):
        await update.message.reply_text("Settings can only be changed in the lobby.")
        return
    if not await is_host_or_admin(context.bot, chat_id, user_id, gs):
        await update.message.reply_text("Only the host or a group admin can change settings.")
        return

    await _show_settings_menu(context, chat_id, gs, query=None)


async def _show_settings_menu(context: ContextTypes.DEFAULT_TYPE, chat_id: int, gs: GameState, query=None) -> None:
    s = gs.settings
    diff_label = "EASY" if s.difficulty == Difficulty.EASY else "HARD"
    kbd = [
        [InlineKeyboardButton(f"Difficulty: {diff_label}", callback_data="set_difficulty")],
        [InlineKeyboardButton(f"Category: {s.category}", callback_data="set_category")],
        [InlineKeyboardButton(f"Imposters: {s.num_imposters}", callback_data="set_imposters")],
        [InlineKeyboardButton(f"Discussion: {s.discussion_time}s", callback_data="set_discussion")],
        [InlineKeyboardButton("✅ Done", callback_data="settings_done")],
    ]
    text = "⚙️ *Game Settings* (host only)"
    markup = InlineKeyboardMarkup(kbd)
    if query:
        # Edit the existing message instead of sending a new one
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)


async def _settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    await query.answer()

    gs = G.get_game(chat_id)
    if not await is_host_or_admin(context.bot, chat_id, user_id, gs):
        await query.answer("Only the host or a group admin can change settings.", show_alert=True)
        return

    data = query.data

    if data == "set_difficulty":
        new = Difficulty.HARD if gs.settings.difficulty == Difficulty.EASY else Difficulty.EASY
        gs.settings.difficulty = new
        gs.save()
        await _show_settings_menu(context, chat_id, gs, query=query)

    elif data == "set_category":
        cats = CATEGORIES
        cur = gs.settings.category
        idx = cats.index(cur) if cur in cats else -1
        gs.settings.category = cats[(idx + 1) % len(cats)]
        gs.save()
        await _show_settings_menu(context, chat_id, gs, query=query)

    elif data == "set_imposters":
        n = gs.settings.num_imposters
        max_imp = max(1, len(gs.players) // 3)
        gs.settings.num_imposters = (n % max_imp) + 1
        gs.save()
        await _show_settings_menu(context, chat_id, gs, query=query)

    elif data == "set_discussion":
        options = [60, 90, 120, 180]
        cur = gs.settings.discussion_time
        idx = options.index(cur) if cur in options else 1
        gs.settings.discussion_time = options[(idx + 1) % len(options)]
        gs.save()
        await _show_settings_menu(context, chat_id, gs, query=query)

    elif data == "settings_done":
        await query.edit_message_text(
            f"✅ Settings saved!\n"
            f"Mode: {gs.settings.difficulty} | Category: {gs.settings.category} | "
            f"Imposters: {gs.settings.num_imposters} | Discussion: {gs.settings.discussion_time}s",
            parse_mode=ParseMode.MARKDOWN,
        )


# ── /customcategory ───────────────────────────────────────────────────────────

async def cmd_customcategory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    gs = G.get_game(chat_id)

    if not gs or gs.phase != Phase.LOBBY:
        await update.message.reply_text("Custom categories can only be set in the lobby.")
        return
    if not await is_host_or_admin(context.bot, chat_id, user_id, gs):
        await update.message.reply_text("Only the host or a group admin can set a custom category.")
        return

    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text(
            "Send word pairs after the command, one per line:\n"
            "`/customcategory Apple | Orange\nPizza | Flatbread`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    pairs, err = G.parse_custom_pairs(text)
    if err:
        await update.message.reply_text(f"❌ {err}")
        return

    gs.settings.custom_pairs = pairs
    gs.settings.category = "Custom"
    gs.save()
    await update.message.reply_text(
        f"✅ Custom category set with {len(pairs)} word pair(s)."
    )


# ── /begin ────────────────────────────────────────────────────────────────────

async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    gs = G.get_game(chat_id)

    if not gs or gs.phase != Phase.LOBBY:
        await update.message.reply_text("No lobby to start. Use /newgame.")
        return
    if not await is_host_or_admin(context.bot, chat_id, user_id, gs):
        await update.message.reply_text("Only the host or a group admin can start the game.")
        return

    n_players = len(gs.players)
    n_imp = gs.settings.num_imposters
    if n_players < MIN_PLAYERS:
        await update.message.reply_text(f"Need at least {MIN_PLAYERS} players. Currently: {n_players}")
        return
    if n_imp >= n_players:
        await update.message.reply_text(
            f"Too many imposters ({n_imp}) for {n_players} players. Reduce via /settings."
        )
        return

    cancel_task(context, "lobby_timer")

    # Assign roles and words
    G.assign_roles(gs)
    gs.phase = Phase.HINT_ROUND
    gs.current_round = 1
    gs.turn_index = 0
    gs.save()

    # DM every player their role
    for player in gs.players.values():
        dm_text = G.get_role_dm_text(gs, player)
        await send_dm(context, player.user_id, dm_text)

    order_names = " → ".join(gs.players[uid].display() for uid in gs.turn_order)
    first_player = gs.players[gs.turn_order[0]]
    await send_group(context, chat_id,
        f"🎮 <b>Game started!</b> {n_players} players, {n_imp} imposter(s).\n"
        f"Mode: {gs.settings.difficulty} | Category: {gs.settings.category}\n\n"
        f"Turn order: {order_names}\n\n"
        f"Each player sends <b>ONE hint word</b> ending with <b>!</b> when it's their turn.\n"
        f"Example: if your word is related to fire, type <code>hot!</code>\n"
        f"3 rounds total, then discussion + vote!\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Round 1 — Turn 1</b>\n"
        f"👉 {mention(first_player)}, send your hint word ending with <b>!</b> <i>(e.g. cold!)</i> — 60 seconds",
        parse_mode=ParseMode.HTML,
    )

    await _start_turn_timer(context, chat_id)


# ── Turn timer ────────────────────────────────────────────────────────────────

async def _start_turn_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    schedule_task(context, "turn_timer", _turn_timer_coro(context, chat_id))


async def _turn_timer_coro(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    gs = G.get_game(chat_id)
    if not gs:
        return

    player = gs.current_turn_player()
    if not player:
        return

    # 60s countdown with pings at 40s and 20s left
    await asyncio.sleep(20)
    if gs.paused:
        await _wait_for_unpause(gs)
    gs2 = G.get_game(chat_id)
    if not gs2 or gs2.phase != Phase.HINT_ROUND:
        return
    if gs2.current_turn_player() and gs2.current_turn_player().user_id == player.user_id:
        await send_group(context, chat_id,
            f"⏳ {mention(player)}, 40 seconds left!",
            parse_mode=ParseMode.HTML,
        )

    await asyncio.sleep(20)
    if gs.paused:
        await _wait_for_unpause(gs)
    gs2 = G.get_game(chat_id)
    if not gs2 or gs2.phase != Phase.HINT_ROUND:
        return
    if gs2.current_turn_player() and gs2.current_turn_player().user_id == player.user_id:
        await send_group(context, chat_id,
            f"⏳ {mention(player)}, 20 seconds left!",
            parse_mode=ParseMode.HTML,
        )

    await asyncio.sleep(20)
    if gs.paused:
        await _wait_for_unpause(gs)

    # Time's up — check again
    gs2 = G.get_game(chat_id)
    if not gs2 or gs2.phase != Phase.HINT_ROUND:
        return
    cur = gs2.current_turn_player()
    if not cur or cur.user_id != player.user_id:
        return  # Already submitted

    # Auto-skip
    player2 = gs2.players.get(player.user_id)
    if not player2:
        return
    player2.consecutive_skips += 1
    round_idx = gs2.current_round - 1
    player2.hints[round_idx] = ""  # blank for skipped

    await send_group(context, chat_id,
        f"⏭️ {player2.display()} passed (no hint in time)."
    )

    if player2.consecutive_skips >= 2:
        player2.active = False
        await send_group(context, chat_id,
            f"🚫 {player2.display()} has been auto-removed (skipped 2 rounds in a row)."
        )
        # Host transfer if needed
        if player2.user_id == gs2.host_id:
            new_host_id = gs2.transfer_host()
            if new_host_id:
                new_host = gs2.players[new_host_id]
                await send_group(context, chat_id,
                    f"👑 Host transferred to {new_host.display()}."
                )

    gs2.save()
    await _advance_turn_or_round(context, chat_id)


async def _wait_for_unpause(gs: GameState) -> None:
    while gs.paused:
        await asyncio.sleep(1)


# ── Handle plain messages (hints) ─────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = update.message.text or ""

    logger.info(f"[MSG] chat={chat_id} user={user_id} phase={G.get_game(chat_id).phase if G.get_game(chat_id) else 'NO_GAME'} text={repr(text)}")

    gs = G.get_game(chat_id)
    if not gs:
        return

    # Custom category capture
    if context.chat_data.get("awaiting_customcategory") == user_id:
        await _handle_customcategory_input(update, context, gs, text)
        return

    # Guess-back phase (private guess)
    if gs.phase == Phase.GUESS_BACK:
        await _handle_guessback(update, context, gs, text)
        return

    if gs.phase != Phase.HINT_ROUND:
        return

    # Only treat messages ending with "!" as hint submissions
    # This lets people chat freely without triggering the bot
    if not text.rstrip().endswith("!"):
        return

    # Strip the trailing ! before processing
    text = text.rstrip().rstrip("!").strip()
    if not text:
        return

    # Rate limit
    if not gs.check_rate_limit(user_id):
        return

    # Must be active player — if not their turn, silently ignore (they're just chatting)
    cur = gs.current_turn_player()
    if not cur:
        return
    if cur.user_id != user_id:
        if user_id in gs.players and gs.players[user_id].active:
            await update.message.reply_text(
                f"⛔ Not your turn! We're waiting for {cur.display()} to send their hint."
            )
        return

    player = gs.players[user_id]
    valid, cleaned, err = G.validate_hint(text, player.secret_word)
    if not valid:
        if "secret word" in err:
            # DM them privately
            await send_dm(context, user_id, f"⚠️ {err}")
        else:
            await update.message.reply_text(err)
        return

    # Record hint
    round_idx = gs.current_round - 1
    player.hints[round_idx] = cleaned
    player.consecutive_skips = 0

    # Check for duplicate hint in this round
    round_hints = [
        (p.display(), p.hints[round_idx])
        for p in gs.players.values()
        if p.user_id != user_id and p.hints[round_idx] == cleaned
    ]
    dup_msg = ""
    if round_hints:
        names = " and ".join(n for n, _ in round_hints) + f" and {player.display()}"
        dup_msg = f"\n👀 {names} both said '{cleaned}' this round!"

    await send_group(context, chat_id,
        f"💬 {player.display()}: <b>{cleaned}</b>{dup_msg}"
    )

    cancel_task(context, "turn_timer")
    gs.save()
    await _advance_turn_or_round(context, chat_id)


async def _advance_turn_or_round(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    gs = G.get_game(chat_id)
    if not gs:
        return

    round_done = gs.advance_turn()
    gs.save()

    if not round_done:
        # Next player's turn
        cur = gs.current_turn_player()
        if cur:
            active = gs.active_players()
            turn_num = gs.turn_index + 1
            await send_group(context, chat_id,
                f"➡️ <b>Turn {turn_num}/{len(active)}</b>\n"
                f"👉 {mention(cur)}, send your hint word ending with <b>!</b> <i>(e.g. cold!)</i> — 60 seconds",
                parse_mode=ParseMode.HTML,
            )
            await _start_turn_timer(context, chat_id)
    else:
        # Round complete
        await send_group(context, chat_id,
            f"✅ Round {gs.current_round} complete!"
        )

        if gs.current_round < TOTAL_ROUNDS:
            gs.current_round += 1
            gs.turn_index = 0
            gs.save()

            # Recap of this round's hints
            round_idx = gs.current_round - 2
            recap_lines = []
            for uid in gs.turn_order:
                p = gs.players.get(uid)
                if p and p.active:
                    h = p.hints[round_idx] or "—"
                    recap_lines.append(f"  {p.display()}: {h}")
            recap = "\n".join(recap_lines)

            cur = gs.current_turn_player()
            cur_mention = mention(cur) if cur else "?"
            await send_group(context, chat_id,
                f"📋 <b>Round {gs.current_round - 1} hints:</b>\n{recap}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔄 <b>Round {gs.current_round} begins!</b>\n"
                f"👉 {cur_mention}, you're first! Send your hint ending with <b>!</b> — 60 seconds",
                parse_mode=ParseMode.HTML,
            )
            await _start_turn_timer(context, chat_id)
        else:
            # All rounds done → Discussion
            await _start_discussion(context, chat_id)


# ── Discussion ────────────────────────────────────────────────────────────────

async def _start_discussion(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    gs = G.get_game(chat_id)
    if not gs:
        return
    gs.phase = Phase.DISCUSSION
    gs.save()

    # Build hint recap for all 3 rounds
    lines = []
    for uid in gs.turn_order:
        p = gs.players.get(uid)
        if not p:
            continue
        hints_str = " | ".join(
            f"R{i+1}:{h}" if h else f"R{i+1}:—" for i, h in enumerate(p.hints)
        )
        lines.append(f"  {p.display()}: {hints_str}")
    hint_recap = "\n".join(lines)

    disc_time = gs.settings.discussion_time
    kbd = [[InlineKeyboardButton("✋ Ready to vote", callback_data="ready_to_vote")]]

    await send_group(context, chat_id,
        f"🗣️ <b>Discussion phase!</b> ({disc_time}s)\n\n"
        f"All hints:\n{hint_recap}\n\n"
        f"Discuss who the Imposter is! Press <b>Ready to vote</b> when your group is ready.",
        reply_markup=InlineKeyboardMarkup(kbd),
    )

    schedule_task(context, "discussion_timer", _discussion_timer(context, chat_id, disc_time))


async def _discussion_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int, disc_time: int) -> None:
    await asyncio.sleep(disc_time - 30)
    gs = G.get_game(chat_id)
    if not gs or gs.phase != Phase.DISCUSSION:
        return
    await send_group(context, chat_id, "⏳ 30 seconds left for discussion!")

    await asyncio.sleep(20)
    gs = G.get_game(chat_id)
    if not gs or gs.phase != Phase.DISCUSSION:
        return
    await send_group(context, chat_id, "⏳ 10 seconds left!")

    await asyncio.sleep(10)
    gs = G.get_game(chat_id)
    if not gs or gs.phase != Phase.DISCUSSION:
        return
    await _start_voting(context, chat_id)


async def _ready_to_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    await query.answer("Marked as ready!")

    gs = G.get_game(chat_id)
    if not gs or gs.phase != Phase.DISCUSSION:
        return
    if user_id not in gs.players or not gs.players[user_id].active:
        return

    gs.players[user_id].ready_to_vote = True
    gs.save()

    active = gs.active_players()
    ready_count = sum(1 for p in active if p.ready_to_vote)
    if ready_count >= len(active):
        cancel_task(context, "discussion_timer")
        await send_group(context, chat_id, "👍 Everyone's ready — moving to voting!")
        await _start_voting(context, chat_id)
    else:
        await send_group(context, chat_id,
            f"✋ {gs.players[user_id].display()} is ready ({ready_count}/{len(active)})"
        )


# ── Voting ────────────────────────────────────────────────────────────────────

async def _start_voting(context: ContextTypes.DEFAULT_TYPE, chat_id: int, re_vote_candidates: Optional[list[int]] = None) -> None:
    gs = G.get_game(chat_id)
    if not gs:
        return

    if re_vote_candidates:
        gs.phase = Phase.RE_VOTE
        gs.vote_candidates = re_vote_candidates
        gs.vote_round = 1
    else:
        gs.phase = Phase.VOTING
        gs.vote_candidates = gs.active_player_ids()
        gs.vote_round = 0

    # Reset votes
    for p in gs.players.values():
        p.voted_for = 0
        p.ready_to_vote = False

    gs.save()

    num_imp = gs.settings.num_imposters
    if num_imp > 1:
        instructions = f"Select <b>{num_imp} suspects</b> (tap each name to toggle):"
    else:
        instructions = "Tap a name to vote:"

    kbd = _build_vote_keyboard(gs)
    phase_label = "RE-VOTE" if gs.vote_round else "VOTING"
    await send_group(context, chat_id,
        f"🗳️ <b>{phase_label} OPEN!</b> (45 seconds)\n{instructions}\n"
        f"<i>Tap a name to vote. Tap again to change your vote. ✅ shows your pick.</i>",
        reply_markup=InlineKeyboardMarkup(kbd),
    )

    schedule_task(context, "vote_timer", _vote_timer(context, chat_id))


def _build_vote_keyboard(gs: GameState) -> list[list[InlineKeyboardButton]]:
    buttons = []
    for uid in gs.vote_candidates:
        p = gs.players.get(uid)
        if p:
            buttons.append([InlineKeyboardButton(p.display(), callback_data=f"vote_{uid}")])
    return buttons


async def _vote_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    await asyncio.sleep(VOTE_TIMEOUT)
    gs = G.get_game(chat_id)
    if not gs or gs.phase not in (Phase.VOTING, Phase.RE_VOTE):
        return
    await send_group(context, chat_id, "⏰ Voting closed!")
    await _tally_votes(context, chat_id)


async def _vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    await query.answer()

    gs = G.get_game(chat_id)
    if not gs or gs.phase not in (Phase.VOTING, Phase.RE_VOTE):
        return
    if user_id not in gs.players or not gs.players[user_id].active:
        return

    data = query.data
    target_id = int(data.split("_", 1)[1])
    if target_id not in gs.vote_candidates:
        return

    gs.players[user_id].voted_for = target_id
    gs.save()

    voted_name = gs.players[target_id].display()

    # Update the keyboard to show a ✅ next to the selected player
    # and show live vote counts so everyone can see progress
    active = gs.active_players()
    voted_count = sum(1 for p in active if p.voted_for)

    new_kbd = []
    for uid in gs.vote_candidates:
        p = gs.players.get(uid)
        if not p:
            continue
        # Show checkmark for this voter's current choice
        label = f"✅ {p.display()}" if uid == target_id else p.display()
        new_kbd.append([InlineKeyboardButton(label, callback_data=f"vote_{uid}")])

    try:
        await query.edit_message_reply_markup(InlineKeyboardMarkup(new_kbd))
    except Exception:
        pass  # Ignore if message can't be edited (e.g. already closed)

    await query.answer(f"✅ Voted for {voted_name}! ({voted_count}/{len(active)} voted)", show_alert=False)

    # Check if all voted
    if voted_count >= len(active):
        cancel_task(context, "vote_timer")
        await _tally_votes(context, chat_id)


async def _tally_votes(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    gs = G.get_game(chat_id)
    if not gs:
        return

    tally = G.tally_votes(gs)
    tally_str = "\n".join(
        f"  {gs.players[uid].display()}: {v} vote(s)"
        for uid, v in sorted(tally.items(), key=lambda x: -x[1])
        if uid in gs.players
    )
    await send_group(context, chat_id, f"📊 <b>Vote tally:</b>\n{tally_str}")

    winners, is_tie = G.find_vote_winner(tally)

    if is_tie:
        if gs.vote_round == 0:
            # First tie → re-vote among tied
            tied_names = ", ".join(gs.players[uid].display() for uid in winners if uid in gs.players)
            await send_group(context, chat_id,
                f"🤝 <b>Tie!</b> Re-vote between: {tied_names}"
            )
            await _start_voting(context, chat_id, re_vote_candidates=winners)
            return
        else:
            # Second tie → Civilians lose by default
            await send_group(context, chat_id,
                "🤝 *Still tied after re-vote!* Civilians lose by default — Imposters win!"
            )
            await _finish_game(context, chat_id, civilians_won=False)
            return

    # Clear winner
    elected_ids = winners  # list of 1 (single imp mode) or num_imposters (multi-imp mode)
    num_imp = gs.settings.num_imposters

    if num_imp > 1:
        # Multi-imposter: must exactly match
        elected_set = set(elected_ids)
        real_imp_set = set(gs.imposter_ids)
        if elected_set == real_imp_set:
            await send_group(context, chat_id, "🎯 *Civilians found all Imposters!*")
            await _finish_game(context, chat_id, civilians_won=True)
        else:
            await send_group(context, chat_id, "❌ *Wrong suspects!* Imposters win!")
            await _finish_game(context, chat_id, civilians_won=False)
        return

    # Single imposter mode
    elected_id = elected_ids[0]
    elected_player = gs.players[elected_id]

    if elected_id not in gs.imposter_ids:
        await send_group(context, chat_id,
            f"❌ {elected_player.display()} was <b>NOT</b> the Imposter! Imposters win!"
        )
        await _finish_game(context, chat_id, civilians_won=False)
    else:
        # Correct! Move to guess-back
        await send_group(context, chat_id,
            f"🎯 {elected_player.display()} was the <b>Imposter!</b>\n"
            f"But wait — the Imposter gets one chance to guess the civilians' word..."
        )
        gs.phase = Phase.GUESS_BACK
        gs.pending_guessback_ids = [elected_id]
        gs.save()
        await send_dm(context, elected_id,
            f"🔴 You've been caught! <b>Guess the civilians' secret word</b> to steal the win.\n"
            f"Reply to this message with your guess (one word):"
        )
        # Store that we're waiting for DM reply — handled in handle_message via private chat
        context.bot_data.setdefault("guessback_pending", {})[elected_id] = chat_id
        # Timeout for guess-back
        schedule_task(context, "guessback_timer", _guessback_timeout(context, chat_id, elected_id))


async def _guessback_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> None:
    await asyncio.sleep(30)
    gs = G.get_game(chat_id)
    if not gs or gs.phase != Phase.GUESS_BACK:
        return
    if user_id in gs.pending_guessback_ids:
        await send_group(context, chat_id,
            f"⏰ Imposter didn't guess in time — Civilians win!"
        )
        await _finish_game(context, chat_id, civilians_won=True)


# ── Guess-back handler (private message) ─────────────────────────────────────

async def _handle_guessback(update: Update, context: ContextTypes.DEFAULT_TYPE, gs: GameState, text: str) -> None:
    """Handle the imposter's guess-back (can come from private chat or group)."""
    user_id = update.effective_user.id
    chat_id = gs.chat_id

    # Find the right game via bot_data mapping
    guessback_map = context.bot_data.get("guessback_pending", {})
    target_chat_id = guessback_map.get(user_id)

    if not target_chat_id:
        return

    gs = G.get_game(target_chat_id)
    if not gs or gs.phase != Phase.GUESS_BACK:
        return
    if user_id not in gs.pending_guessback_ids:
        return

    cancel_task(context, "guessback_timer")
    guessback_map.pop(user_id, None)
    gs.pending_guessback_ids.remove(user_id)

    guess = text.strip().split()[0] if text.strip() else ""
    correct = guess.lower() == gs.majority_word.lower()

    if correct:
        gs.guessed_back_correctly.append(user_id)
        gs.save()
        await send_group(context, target_chat_id,
            f"😱 The Imposter guessed <b>{guess}</b> — <b>CORRECT!</b>\n"
            f"Imposters steal the win despite being caught!"
        )
        await _finish_game(context, target_chat_id, civilians_won=False)
    else:
        gs.save()
        await send_group(context, target_chat_id,
            f"😌 The Imposter guessed <b>{guess}</b> — Wrong! The word was <b>{gs.majority_word}</b>.\n"
            f"Civilians win! 🎉"
        )
        await _finish_game(context, target_chat_id, civilians_won=True)


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle messages sent directly to the bot (for guess-back)."""
    user_id = update.effective_user.id
    text = update.message.text or ""

    guessback_map = context.bot_data.get("guessback_pending", {})
    if user_id in guessback_map:
        chat_id = guessback_map[user_id]
        gs = G.get_game(chat_id)
        if gs:
            await _handle_guessback(update, context, gs, text)


# ── Game finish ───────────────────────────────────────────────────────────────

async def _finish_game(context: ContextTypes.DEFAULT_TYPE, chat_id: int, civilians_won: bool) -> None:
    gs = G.get_game(chat_id)
    if not gs:
        return

    gs.phase = Phase.REVEAL
    gs.save()

    # Reveal
    imp_names = ", ".join(
        gs.players[uid].display() for uid in gs.imposter_ids if uid in gs.players
    )
    outcome_emoji = "🎉" if civilians_won else "😈"
    outcome_text = "Civilians win!" if civilians_won else "Imposters win!"
    await send_group(context, chat_id,
        f"{outcome_emoji} <b>{outcome_text}</b>\n\n"
        f"🔴 The Imposter(s) were: {imp_names}\n"
        f"🔤 Civilian word: <b>{gs.majority_word}</b>\n"
        f"🎭 Imposter decoy: <b>{gs.decoy_word}</b>\n"
        f"Category: {gs.category_used}"
    )

    # Post recap
    recap = G.build_recap(gs, civilians_won)
    await send_group(context, chat_id, recap)

    # Update leaderboard
    civilians = [
        {"user_id": p.user_id, "username": p.display()}
        for p in gs.players.values() if p.role == "civilian"
    ]
    imposters = [
        {"user_id": p.user_id, "username": p.display()}
        for p in gs.players.values() if p.role == "imposter"
    ]
    caught_ids = set(gs.imposter_ids)  # simplification: all were caught if civilians won
    guessed_ids = set(gs.guessed_back_correctly)

    db.record_game_result(
        chat_id=chat_id,
        civilians=civilians,
        imposters=imposters,
        civilians_won=civilians_won,
        caught_imposter_ids=caught_ids if civilians_won else set(),
        guessed_back_ids=guessed_ids,
    )

    # Offer rematch
    kbd = [[InlineKeyboardButton("🔄 Rematch!", callback_data="rematch_ready")]]
    await send_group(context, chat_id,
        "Use /rematch to play again with the same group, or /newgame for a fresh lobby.",
        reply_markup=InlineKeyboardMarkup(kbd),
    )

    G.remove_game(chat_id)


# ── /rematch ──────────────────────────────────────────────────────────────────

async def cmd_rematch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    prev = context.chat_data.get("last_game_players")
    if not prev:
        await update.message.reply_text("No recent game to rematch. Use /newgame.")
        return

    # Create new lobby with same players
    gs = GameState(chat_id=chat_id, host_id=user_id)
    gs.phase = Phase.REMATCH_LOBBY
    for p_data in prev:
        p = Player(**p_data)
        p.rematch_ready = False
        gs.players[p.user_id] = p
        gs.join_order.append(p.user_id)
    G.set_game(gs)
    gs.save()

    kbd = [[InlineKeyboardButton("✅ Ready!", callback_data="rematch_ready")]]
    names = ", ".join(p.display() for p in gs.active_players())
    await send_group(context, chat_id,
        f"🔄 <b>Rematch!</b> Same players: {names}\n\n"
        f"Press <b>Ready</b> to confirm you're in! (5 min timeout)",
        reply_markup=InlineKeyboardMarkup(kbd),
    )
    schedule_task(context, "lobby_timer", _lobby_timeout(context, chat_id))


async def _rematch_ready_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    await query.answer("You're ready!")

    gs = G.get_game(chat_id)
    if not gs or gs.phase != Phase.REMATCH_LOBBY:
        return
    if user_id not in gs.players:
        return

    gs.players[user_id].rematch_ready = True
    gs.save()

    active = gs.active_players()
    ready_count = sum(1 for p in active if p.rematch_ready)
    if ready_count >= len(active):
        # All ready — auto-begin
        cancel_task(context, "lobby_timer")
        gs.phase = Phase.LOBBY
        await send_group(context, chat_id, "Everyone's ready — starting game!")
        # Fake a /begin call
        G.assign_roles(gs)
        gs.phase = Phase.HINT_ROUND
        gs.current_round = 1
        gs.turn_index = 0
        gs.save()

        for player in gs.players.values():
            dm_text = G.get_role_dm_text(gs, player)
            await send_dm(context, player.user_id, dm_text)

        order_names = " → ".join(gs.players[uid].display() for uid in gs.turn_order)
        cur = gs.current_turn_player()
        cur_mention = mention(cur) if cur else "?"
        await send_group(context, chat_id,
            f"🎮 <b>Rematch started!</b>\nTurn order: {order_names}\n\n"
            f"<b>Round 1!</b> {cur_mention}, you're first! <i>(60s)</i>",
            parse_mode=ParseMode.HTML,
        )
        await _start_turn_timer(context, chat_id)
    else:
        player = gs.players[user_id]
        await send_group(context, chat_id,
            f"✅ {player.display()} is ready! ({ready_count}/{len(active)})"
        )


# ── /kick ─────────────────────────────────────────────────────────────────────

async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    gs = G.get_game(chat_id)

    if not gs:
        await update.message.reply_text("No active game.")
        return
    if not await is_host_or_admin(context.bot, chat_id, user_id, gs):
        await update.message.reply_text("Only the host or a group admin can kick players.")
        return

    mentions = update.message.entities or []
    target_id = None
    for ent in mentions:
        if ent.type == "mention":
            username = update.message.text[ent.offset + 1: ent.offset + ent.length]
            for p in gs.players.values():
                if p.username and p.username.lower() == username.lower():
                    target_id = p.user_id
                    break

    if not target_id:
        await update.message.reply_text("Usage: /kick @username")
        return

    if target_id == gs.host_id:
        await update.message.reply_text("You can't kick yourself (the host).")
        return

    player = gs.players.get(target_id)
    if not player:
        await update.message.reply_text("Player not found.")
        return

    player.active = False
    gs.save()
    await send_group(context, chat_id, f"🚫 {player.display()} has been kicked.")

    if gs.phase == Phase.HINT_ROUND:
        cur = gs.current_turn_player()
        if cur and cur.user_id == target_id:
            cancel_task(context, "turn_timer")
            await _advance_turn_or_round(context, chat_id)


# ── /pause & /resume ──────────────────────────────────────────────────────────

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    gs = G.get_game(chat_id)
    if not gs or not await is_host_or_admin(context.bot, chat_id, user_id, gs):
        return
    gs.paused = True
    gs.save()
    await send_group(context, chat_id, "⏸️ Game paused. Use /resume to continue.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    gs = G.get_game(chat_id)
    if not gs or not await is_host_or_admin(context.bot, chat_id, user_id, gs):
        return
    gs.paused = False
    gs.save()
    await send_group(context, chat_id, "▶️ Game resumed!")


# ── /endgame ──────────────────────────────────────────────────────────────────

async def cmd_endgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    gs = G.get_game(chat_id)
    if not gs:
        await update.message.reply_text("No active game.")
        return
    if not await is_host_or_admin(context.bot, chat_id, user_id, gs):
        await update.message.reply_text("Only the host or a group admin can end the game.")
        return

    for key in ("turn_timer", "lobby_timer", "discussion_timer", "vote_timer", "guessback_timer"):
        cancel_task(context, key)

    G.remove_game(chat_id)
    await send_group(context, chat_id, "🛑 Game ended by host.")


# ── /score ────────────────────────────────────────────────────────────────────

async def cmd_score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    rows = db.get_leaderboard(chat_id)

    if not rows:
        await update.message.reply_text("No scores yet! Play a game first.")
        return

    lines = ["🏆 *Leaderboard*\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i+1}."
        lines.append(
            f"{medal} <b>{r['username']}</b> — {r['total_wins']}W / {r['games_played']}G\n"
            f"   Civ: {r['civ_wins']}W {r['civ_losses']}L | Imp: {r['imp_wins']}W {r['imp_losses']}L"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── Callback router ───────────────────────────────────────────────────────────

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data

    if data.startswith("set_") or data == "settings_done":
        await _settings_callback(update, context)
    elif data == "ready_to_vote":
        await _ready_to_vote_callback(update, context)
    elif data.startswith("vote_"):
        await _vote_callback(update, context)
    elif data == "rematch_ready":
        await _rematch_ready_callback(update, context)
    else:
        await query.answer()


# ── Startup crash recovery ────────────────────────────────────────────────────

async def recover_games(application: Application) -> None:
    """On startup, reload any saved game states from DB."""
    saved = db.load_all_game_states()
    for chat_id, state_dict in saved:
        try:
            gs = GameState.from_dict(state_dict)
            G.set_game(gs)
            logger.info(f"Recovered game state for chat {chat_id} (phase={gs.phase})")
            # Notify the chat that the bot restarted
            if gs.phase not in (Phase.IDLE, Phase.REVEAL):
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "♻️ *Bot restarted!* Game state recovered from phase: "
                        f"<b>{gs.phase}</b>\n"
                        "Note: active timers have reset. Play continues from current state.\n"
                        f"It is currently round {gs.current_round}. "
                        "The host can /endgame and /newgame if needed."
                    ),
                    parse_mode=ParseMode.MARKDOWN,
                )
        except Exception as e:
            logger.error(f"Failed to recover game for chat {chat_id}: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def setup_commands(application: Application) -> None:
    """Register bot commands so they appear in the / menu in Telegram."""
    await recover_games(application)
    commands = [
        ("newgame",        "Open a new game lobby"),
        ("join",           "Join the current lobby"),
        ("begin",          "Start the game (host/admin)"),
        ("settings",       "Configure game options (host/admin)"),
        ("customcategory", "Set a custom word list (host/admin)"),
        ("rematch",        "Replay with the same group"),
        ("score",          "Show the leaderboard"),
        ("kick",           "Remove a player (host/admin)"),
        ("pause",          "Pause timers (host/admin)"),
        ("resume",         "Resume timers (host/admin)"),
        ("endgame",        "Force-end the game (host/admin)"),
        ("help",           "Show rules and commands"),
    ]
    await application.bot.set_my_commands(commands)


def main() -> None:
    db.init_db()

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(setup_commands)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("customcategory", cmd_customcategory))
    app.add_handler(CommandHandler("begin", cmd_begin))
    app.add_handler(CommandHandler("kick", cmd_kick))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("endgame", cmd_endgame))
    app.add_handler(CommandHandler("rematch", cmd_rematch))
    app.add_handler(CommandHandler("score", cmd_score))
    app.add_handler(CommandHandler("leaderboard", cmd_score))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(callback_router))

    # Messages — group hints
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, handle_message)
    )
    # Messages — private (guess-back)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_private_message)
    )

    logger.info("Word Wolf bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()