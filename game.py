"""
game.py — Word Wolf / Imposter game state machine.

State flow per chat:
  IDLE → LOBBY → (SETTINGS) → HINT_ROUND[1..3] → DISCUSSION → VOTING
       → [RE_VOTE] → [GUESS_BACK] → REVEAL → IDLE  (or REMATCH_LOBBY)

All mutable state lives in GameState dataclasses, keyed by chat_id.
State is snapshotted to SQLite after every significant change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional

import db

logger = logging.getLogger(__name__)

TOTAL_ROUNDS = 3
MIN_PLAYERS = 3
TURN_TIMEOUT = 60          # seconds per hint turn
LOBBY_TIMEOUT = 300        # 5 minutes
DISCUSSION_DEFAULT = 120   # 2 minutes
VOTE_TIMEOUT = 45          # seconds
RATE_LIMIT_WINDOW = 2.0    # seconds between commands per user

# ── Load word bank ────────────────────────────────────────────────────────────

_WORDS_PATH = Path(__file__).parent / "words.json"
with open(_WORDS_PATH) as _f:
    WORD_BANK: dict[str, list[dict]] = json.load(_f)

CATEGORIES = list(WORD_BANK.keys()) + ["Random"]


class Phase(str, Enum):
    IDLE = "IDLE"
    LOBBY = "LOBBY"
    HINT_ROUND = "HINT_ROUND"
    DISCUSSION = "DISCUSSION"
    VOTING = "VOTING"
    RE_VOTE = "RE_VOTE"
    GUESS_BACK = "GUESS_BACK"
    REVEAL = "REVEAL"
    REMATCH_LOBBY = "REMATCH_LOBBY"


class Difficulty(str, Enum):
    EASY = "EASY"
    HARD = "HARD"


@dataclass
class Player:
    user_id: int
    username: str
    first_name: str
    # Assigned during game start
    secret_word: str = ""
    role: str = ""          # "civilian" | "imposter"
    # Per-round hints list (index = round 0,1,2)
    hints: list[str] = field(default_factory=lambda: ["", "", ""])
    consecutive_skips: int = 0
    active: bool = True     # False if auto-removed
    voted_for: int = 0      # user_id voted for (during voting)
    ready_to_vote: bool = False
    rematch_ready: bool = False

    def display(self) -> str:
        return f"@{self.username}" if self.username else self.first_name


@dataclass
class GameSettings:
    difficulty: str = Difficulty.EASY
    category: str = "Random"
    num_imposters: int = 1
    discussion_time: int = DISCUSSION_DEFAULT
    custom_pairs: list[dict] = field(default_factory=list)   # host custom word pairs


@dataclass
class GameState:
    chat_id: int
    host_id: int
    phase: str = Phase.IDLE
    settings: GameSettings = field(default_factory=GameSettings)
    players: dict[int, Player] = field(default_factory=dict)   # user_id → Player
    join_order: list[int] = field(default_factory=list)        # insertion order
    current_round: int = 0       # 1-indexed; 0 = not started
    turn_index: int = 0          # index into active turn order
    turn_order: list[int] = field(default_factory=list)        # user_ids for turn order
    majority_word: str = ""
    decoy_word: str = ""
    category_used: str = ""
    imposter_ids: list[int] = field(default_factory=list)
    # Voting state
    vote_round: int = 0          # 0=first vote, 1=re-vote
    vote_candidates: list[int] = field(default_factory=list)   # user_ids to vote on
    # Guess-back state
    pending_guessback_ids: list[int] = field(default_factory=list)
    guessed_back_correctly: list[int] = field(default_factory=list)
    # Lobby timer start
    lobby_start: float = 0.0
    # Paused state
    paused: bool = False
    # Last command timestamps per user (rate limiting) — not persisted
    _rate_limits: dict = field(default_factory=dict, repr=False)

    # ── Serialization (for DB snapshot) ───────────────────────────────────────

    def to_dict(self) -> dict:
        d = {
            "chat_id": self.chat_id,
            "host_id": self.host_id,
            "phase": self.phase,
            "settings": asdict(self.settings),
            "players": {
                str(uid): asdict(p) for uid, p in self.players.items()
            },
            "join_order": self.join_order,
            "current_round": self.current_round,
            "turn_index": self.turn_index,
            "turn_order": self.turn_order,
            "majority_word": self.majority_word,
            "decoy_word": self.decoy_word,
            "category_used": self.category_used,
            "imposter_ids": self.imposter_ids,
            "vote_round": self.vote_round,
            "vote_candidates": self.vote_candidates,
            "pending_guessback_ids": self.pending_guessback_ids,
            "guessed_back_correctly": self.guessed_back_correctly,
            "lobby_start": self.lobby_start,
            "paused": self.paused,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "GameState":
        settings = GameSettings(**d["settings"])
        players = {
            int(uid): Player(**pdata)
            for uid, pdata in d["players"].items()
        }
        gs = cls(
            chat_id=d["chat_id"],
            host_id=d["host_id"],
            phase=d["phase"],
            settings=settings,
            players=players,
            join_order=d["join_order"],
            current_round=d["current_round"],
            turn_index=d["turn_index"],
            turn_order=d["turn_order"],
            majority_word=d["majority_word"],
            decoy_word=d["decoy_word"],
            category_used=d["category_used"],
            imposter_ids=d["imposter_ids"],
            vote_round=d["vote_round"],
            vote_candidates=d["vote_candidates"],
            pending_guessback_ids=d["pending_guessback_ids"],
            guessed_back_correctly=d.get("guessed_back_correctly", []),
            lobby_start=d.get("lobby_start", 0.0),
            paused=d.get("paused", False),
        )
        return gs

    def save(self) -> None:
        db.save_game_state(self.chat_id, self.to_dict())

    def active_players(self) -> list[Player]:
        return [self.players[uid] for uid in self.join_order
                if uid in self.players and self.players[uid].active]

    def active_player_ids(self) -> list[int]:
        return [p.user_id for p in self.active_players()]

    def current_turn_player(self) -> Optional[Player]:
        if not self.turn_order:
            return None
        active = [uid for uid in self.turn_order if self.players[uid].active]
        if self.turn_index >= len(active):
            return None
        return self.players[active[self.turn_index]]

    def advance_turn(self) -> bool:
        """Move to next active player's turn. Returns True if round is complete."""
        active = [uid for uid in self.turn_order if self.players[uid].active]
        self.turn_index += 1
        return self.turn_index >= len(active)

    def is_host(self, user_id: int) -> bool:
        return user_id == self.host_id

    def transfer_host(self) -> Optional[int]:
        """Transfer host to earliest-joined active player (excluding current host)."""
        for uid in self.join_order:
            if uid != self.host_id and uid in self.players and self.players[uid].active:
                self.host_id = uid
                self.save()
                return uid
        return None

    def check_rate_limit(self, user_id: int) -> bool:
        """Returns True if allowed, False if rate-limited."""
        now = time.monotonic()
        last = self._rate_limits.get(user_id, 0)
        if now - last < RATE_LIMIT_WINDOW:
            return False
        self._rate_limits[user_id] = now
        return True


# ── Global game registry ──────────────────────────────────────────────────────

_games: dict[int, GameState] = {}


def get_game(chat_id: int) -> Optional[GameState]:
    return _games.get(chat_id)


def get_or_create_idle(chat_id: int) -> GameState:
    if chat_id not in _games:
        _games[chat_id] = GameState(chat_id=chat_id, host_id=0)
    return _games[chat_id]


def set_game(gs: GameState) -> None:
    _games[gs.chat_id] = gs


def remove_game(chat_id: int) -> None:
    _games.pop(chat_id, None)
    db.delete_game_state(chat_id)


# ── Word selection ────────────────────────────────────────────────────────────

def pick_word_pair(settings: GameSettings) -> tuple[str, str, str]:
    """
    Returns (majority_word, decoy_word, category_name).
    Uses custom_pairs if set; otherwise picks from the word bank.
    """
    if settings.custom_pairs:
        pair = random.choice(settings.custom_pairs)
        return pair["majority_word"], pair["decoy_word"], "Custom"

    if settings.category == "Random":
        cats = [c for c in WORD_BANK.keys()]
        cat = random.choice(cats)
    else:
        cat = settings.category

    pair = random.choice(WORD_BANK[cat])
    return pair["majority_word"], pair["decoy_word"], cat


# ── Role assignment & word distribution ──────────────────────────────────────

def assign_roles(gs: GameState) -> None:
    """
    Picks imposters randomly, assigns secret words to all players.
    EASY MODE: Imposters know they are imposters + get a blend-in hint word for round 1.
    HARD MODE: Imposters don't know; they receive the decoy word as their "real" word.
    """
    majority, decoy, cat = pick_word_pair(gs.settings)
    gs.majority_word = majority
    gs.decoy_word = decoy
    gs.category_used = cat

    player_ids = list(gs.players.keys())
    random.shuffle(player_ids)
    num_imp = gs.settings.num_imposters
    imposters = player_ids[:num_imp]
    gs.imposter_ids = imposters

    for uid, player in gs.players.items():
        if uid in imposters:
            player.role = "imposter"
            if gs.settings.difficulty == Difficulty.HARD:
                # Hard mode: imposter gets decoy word and doesn't know
                player.secret_word = decoy
            else:
                # Easy mode: imposter gets majority word label for round-1 hint
                # We store majority word here and signal role separately
                player.secret_word = majority
        else:
            player.role = "civilian"
            player.secret_word = majority

    # Build turn order — always freshly shuffled so first player varies every game
    ids = list(gs.join_order)
    random.shuffle(ids)
    # Extra shuffle pass to reduce any pattern from previous games
    random.shuffle(ids)
    gs.turn_order = ids


def get_role_dm_text(gs: GameState, player: Player) -> str:
    """Craft the DM sent to a player at game start."""
    if player.role == "civilian":
        return (
            f"🔵 You are a *Civilian*!\n"
            f"Your secret word is: *{player.secret_word}*\n\n"
            f"Give one-word hints related to your word each round. "
            f"Work together to find the Imposter!"
        )
    # Imposter
    if gs.settings.difficulty == Difficulty.EASY:
        return (
            f"🔴 You are the *Imposter*!\n"
            f"Category: *{gs.category_used}*\n\n"
            f"You don't know the civilians' exact word — but here's a *blend-in hint* "
            f"you can use for Round 1 to seem like you belong:\n\n"
            f"💡 Suggested Round 1 hint: *{gs.decoy_word}*\n\n"
            f"For Rounds 2 & 3 you're on your own — listen to others' hints and bluff!"
        )
    else:
        # Hard mode — imposter thinks they have the same word as everyone
        return (
            f"🔵 You are a *Civilian*!\n"
            f"Your secret word is: *{player.secret_word}*\n\n"
            f"Give one-word hints related to your word each round. "
            f"Work together to find the Imposter!"
        )


# ── Hint validation ───────────────────────────────────────────────────────────

_PUNCT_RE = re.compile(r"[^\w\s\-']", re.UNICODE)


def validate_hint(raw: str, secret_word: str) -> tuple[bool, str, str]:
    """
    Returns (is_valid, cleaned_word, error_message).
    cleaned_word is the normalized hint if valid.
    """
    # Strip punctuation/emoji except internal hyphens and apostrophes
    cleaned = _PUNCT_RE.sub("", raw).strip()

    # Must be non-empty
    if not cleaned:
        return False, "", "Please send a non-empty hint word."

    # Must be exactly one token (no spaces)
    parts = cleaned.split()
    if len(parts) > 1:
        suggestion = parts[-1]  # suggest last word
        return False, "", (
            f"Only one word allowed — try just '{suggestion}' instead of '{raw}'."
        )

    word = parts[0]

    # Must not exactly match secret word (case-insensitive)
    if word.lower() == secret_word.lower():
        return False, "", "That's the secret word itself — give a related word instead."

    return True, word, ""


# ── Custom category parsing ───────────────────────────────────────────────────

def parse_custom_pairs(text: str) -> tuple[list[dict], str]:
    """
    Parse host-supplied word pairs. Format: "MajorityWord | DecoyWord" per line.
    Returns (pairs, error_message). error_message is empty on success.
    """
    pairs = []
    errors = []
    for i, line in enumerate(text.strip().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            errors.append(f"Line {i}: '{line}' — expected 'MajorityWord | DecoyWord'")
            continue
        if " " in parts[0] or " " in parts[1]:
            # Allow hyphenated compound words but not pure spaces
            pass
        pairs.append({"majority_word": parts[0], "decoy_word": parts[1]})

    if errors:
        return [], "Format errors:\n" + "\n".join(errors)
    if len(pairs) < 2:
        return [], "Please provide at least 2 word pairs."
    return pairs, ""


# ── Scoring helpers ───────────────────────────────────────────────────────────

def tally_votes(gs: GameState) -> dict[int, int]:
    """Returns {user_id: vote_count} for current vote_candidates."""
    tally: dict[int, int] = {uid: 0 for uid in gs.vote_candidates}
    for player in gs.active_players():
        if player.voted_for and player.voted_for in tally:
            tally[player.voted_for] += 1
    return tally


def find_vote_winner(tally: dict[int, int]) -> tuple[Optional[list[int]], bool]:
    """
    Returns (winner_ids, is_tie).
    winner_ids = [uid] if clear winner, list of tied uids if tie.
    """
    if not tally:
        return None, False
    max_votes = max(tally.values())
    winners = [uid for uid, v in tally.items() if v == max_votes]
    if len(winners) == 1:
        return winners, False
    return winners, True


def compute_awards(gs: GameState, civilians_won: bool) -> list[str]:
    """
    Compute fun flavor awards for the end-of-game recap.
    Returns a list of award strings.
    """
    awards = []
    imposter_ids_set = set(gs.imposter_ids)

    # Best Bluffer: Imposter who survived the vote (got fewest votes)
    if gs.imposter_ids:
        imp_votes = {}
        for player in gs.players.values():
            if player.voted_for in imposter_ids_set:
                imp_votes[player.voted_for] = imp_votes.get(player.voted_for, 0) + 1
        for uid in gs.imposter_ids:
            imp_votes.setdefault(uid, 0)
        min_votes = min(imp_votes.values())
        best_bluffer_ids = [uid for uid, v in imp_votes.items() if v == min_votes]
        for uid in best_bluffer_ids:
            p = gs.players.get(uid)
            if p:
                awards.append(f"🎭 *Best Bluffer*: {p.display()} (received only {min_votes} vote(s) as Imposter!)")

    # Most Suspicious: civilian with most votes
    civ_vote_counts = {}
    for player in gs.players.values():
        target = player.voted_for
        if target and target not in imposter_ids_set:
            civ_vote_counts[target] = civ_vote_counts.get(target, 0) + 1
    if civ_vote_counts:
        max_v = max(civ_vote_counts.values())
        most_sus_ids = [uid for uid, v in civ_vote_counts.items() if v == max_v]
        for uid in most_sus_ids:
            p = gs.players.get(uid)
            if p:
                awards.append(f"🕵️ *Most Suspicious Civilian*: {p.display()} ({max_v} vote(s)!)")

    # Sharpest Eye: player who voted for a real imposter
    sharp_ids = []
    for player in gs.players.values():
        if player.user_id not in imposter_ids_set and player.voted_for in imposter_ids_set:
            sharp_ids.append(player.user_id)
    if sharp_ids:
        names = ", ".join(gs.players[uid].display() for uid in sharp_ids if uid in gs.players)
        awards.append(f"👁️ *Sharpest Eye*: {names} (correctly suspected the Imposter!)")

    return awards


def build_recap(gs: GameState, civilians_won: bool) -> str:
    """Build the full post-game recap message."""
    lines = ["━━━━━━━━━━━━━━━━━━━━━━", "📋 *GAME RECAP*", "━━━━━━━━━━━━━━━━━━━━━━"]

    lines.append(f"\n🔤 *Words*: Civilians had *{gs.majority_word}* | Imposter decoy: *{gs.decoy_word}*")
    lines.append(f"📂 Category: {gs.category_used} | Mode: {gs.settings.difficulty}")

    imp_names = ", ".join(
        gs.players[uid].display() for uid in gs.imposter_ids if uid in gs.players
    )
    lines.append(f"🔴 Imposter(s): {imp_names}\n")

    lines.append("*Hints by round:*")
    for uid in gs.turn_order:
        if uid not in gs.players:
            continue
        p = gs.players[uid]
        role_tag = "🔴" if p.role == "imposter" else "🔵"
        hints_str = " | ".join(
            f"R{i+1}: {h}" if h else f"R{i+1}: —"
            for i, h in enumerate(p.hints)
        )
        lines.append(f"  {role_tag} {p.display()}: {hints_str}")

    lines.append("\n*Votes cast:*")
    for player in gs.active_players():
        voted_p = gs.players.get(player.voted_for)
        voted_name = voted_p.display() if voted_p else "—"
        lines.append(f"  {player.display()} → {voted_name}")

    outcome = "🎉 *Civilians WIN!*" if civilians_won else "😈 *Imposters WIN!*"
    lines.append(f"\n{outcome}")

    awards = compute_awards(gs, civilians_won)
    if awards:
        lines.append("\n🏆 *Awards*")
        lines.extend(awards)

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)