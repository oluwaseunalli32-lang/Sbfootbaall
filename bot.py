"""SB24GZFootballFactsBot - compare two football players using manually stored stats.

All data lives in players.json. Edit that file to add or update players.
"""

import difflib
import html
import json
import logging
import math
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("footballfacts")

DATA_FILE = Path(__file__).with_name("players.json")
PICKER_PAGE_SIZE = 8
LIST_PAGE_SIZE = 15
ID_PATTERN = re.compile(r"^[a-z0-9-]{1,20}$")
STAT_FIELDS = ("goals", "assists", "appearances", "trophies")


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Player:
    id: str
    name: str
    position: str
    nationality: str
    goals: int
    assists: int
    appearances: int
    trophies: int

    @property
    def goals_per_game(self) -> float:
        return self.goals / self.appearances if self.appearances else 0.0

    @property
    def subtitle(self) -> str:
        return ", ".join(part for part in (self.position, self.nationality) if part)


def load_data(path: Path = DATA_FILE):
    """Read and validate players.json. Exits with a clear message if it is wrong."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit("players.json was not found next to bot.py.")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"players.json is not valid JSON: {exc}")

    players, seen = [], set()
    for number, item in enumerate(raw.get("players", []), start=1):
        try:
            pid = str(item["id"])
            if not ID_PATTERN.match(pid):
                raise ValueError(
                    "id must be 1-20 characters: lowercase letters, numbers, hyphens"
                )
            if pid in seen:
                raise ValueError(f"duplicate id '{pid}'")
            seen.add(pid)
            name = str(item["name"]).strip()
            if not name:
                raise ValueError("name is empty")
            stats = {}
            for field in STAT_FIELDS:
                value = item[field]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"{field} must be a whole number, 0 or higher")
                stats[field] = value
        except KeyError as exc:
            raise SystemExit(f"players.json entry #{number} is missing the field {exc}")
        except ValueError as exc:
            raise SystemExit(f"players.json entry #{number}: {exc}")
        players.append(
            Player(
                id=pid,
                name=name,
                position=str(item.get("position", "")).strip(),
                nationality=str(item.get("nationality", "")).strip(),
                **stats,
            )
        )

    if len(players) < 2:
        raise SystemExit("players.json needs at least 2 players.")

    players.sort(key=lambda p: normalize(p.name))
    meta = {
        "last_updated": str(raw.get("last_updated", "unknown")),
        "source_note": str(raw.get("source_note", "")).strip(),
    }
    return players, meta


def normalize(text: str) -> str:
    """Lowercase and remove accents so 'Mbappé' matches 'mbappe'."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())


PLAYERS, META = load_data()
BY_ID = {p.id: p for p in PLAYERS}


def find_player(query: str):
    """Return (player, suggestions). player is None if no single clear match."""
    q = normalize(query)
    if not q:
        return None, []
    for p in PLAYERS:
        if normalize(p.name) == q or p.id == q:
            return p, []
    contains = [p for p in PLAYERS if q in normalize(p.name)]
    if len(contains) == 1:
        return contains[0], []
    if len(contains) > 1:
        return None, contains[:5]
    # Typo help: compare against full names and against each single word
    # of a name (so "messy" can still find "Lionel Messi").
    candidates = {}
    for p in PLAYERS:
        full = normalize(p.name)
        candidates.setdefault(full, p)
        for word in full.split():
            candidates.setdefault(word, p)
    close = difflib.get_close_matches(q, list(candidates), n=6, cutoff=0.6)
    suggestions, seen = [], set()
    for c in close:
        player = candidates[c]
        if player.id not in seen:
            seen.add(player.id)
            suggestions.append(player)
    return None, suggestions[:3]


# --------------------------------------------------------------------------
# Text builders
# --------------------------------------------------------------------------
def esc(text: str) -> str:
    return html.escape(text, quote=False)


def welcome_text() -> str:
    return (
        "<b>Football Player Compare</b>\n\n"
        "Pick two players and see their goals, assists, appearances and "
        "trophies side by side.\n\n"
        f"{len(PLAYERS)} players available. Stats are entered manually and may "
        "not be up to date. This bot shows stats only and gives no "
        "predictions or tips.\n\n"
        "Tap <b>Compare players</b> to start."
    )


HELP_TEXT = (
    "<b>How to use this bot</b>\n\n"
    "1. Tap <b>Compare players</b> and choose two players from the list.\n"
    "2. Or type <code>/compare Player One vs Player Two</code>\n\n"
    "<b>Commands</b>\n"
    "/start - main menu\n"
    "/compare - compare two players\n"
    "/players - list all players\n"
    "/help - this message\n"
    "/about - about the stats"
)


def about_text() -> str:
    lines = [
        "<b>About the stats</b>\n",
        f"Players in the database: {len(PLAYERS)}",
        f"Data last updated: {esc(META['last_updated'])}",
    ]
    if META["source_note"]:
        lines.append(f"Source note: {esc(META['source_note'])}")
    lines.append(
        "\nAll numbers are entered manually, so they can be out of date or "
        "contain mistakes. The comparison shows stats only and is not a "
        "ranking of overall ability. This bot gives no predictions or tips, "
        "and it does not store your personal data."
    )
    return "\n".join(lines)


def build_comparison(a: Player, b: Player) -> str:
    rows = [
        ("Goals", a.goals, b.goals, f"{a.goals:,}", f"{b.goals:,}", True),
        ("Assists", a.assists, b.assists, f"{a.assists:,}", f"{b.assists:,}", True),
        ("Apps", a.appearances, b.appearances,
         f"{a.appearances:,}", f"{b.appearances:,}", False),
        ("Trophies", a.trophies, b.trophies,
         f"{a.trophies:,}", f"{b.trophies:,}", True),
        ("Goals/game", round(a.goals_per_game, 2), round(b.goals_per_game, 2),
         f"{a.goals_per_game:.2f}", f"{b.goals_per_game:.2f}", True),
    ]
    table = [f"{'Stat':<11}{'A':>6}   {'B':<6}"]
    a_lead = b_lead = 0
    for label, av, bv, a_str, b_str, counted in rows:
        sign = ">" if av > bv else "<" if av < bv else "="
        table.append(f"{label:<11}{a_str:>6} {sign} {b_str:<6}")
        if counted:
            if av > bv:
                a_lead += 1
            elif bv > av:
                b_lead += 1

    if a_lead == b_lead:
        summary = f"Level on the 4 counted stats ({a_lead}-{b_lead})."
    else:
        leader = a if a_lead > b_lead else b
        summary = (
            f"{esc(leader.name)} is ahead on more of the 4 counted stats "
            f"({max(a_lead, b_lead)}-{min(a_lead, b_lead)})."
        )

    def describe(tag: str, p: Player) -> str:
        extra = f" ({esc(p.subtitle)})" if p.subtitle else ""
        return f"<b>{tag}</b> = {esc(p.name)}{extra}"

    return (
        "<b>Player comparison</b>\n"
        f"{describe('A', a)}\n{describe('B', b)}\n\n"
        f"<pre>{esc(chr(10).join(table))}</pre>\n"
        f"{summary}\n"
        "Counted: goals, assists, trophies, goals per game. Apps are shown "
        "for context only.\n\n"
        f"<i>Manual stats, last updated {esc(META['last_updated'])}. "
        "Not a ranking of overall ability.</i>"
    )


# --------------------------------------------------------------------------
# Keyboards
# --------------------------------------------------------------------------
def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn("Compare players", "cmp")],
            [btn("Player list", "list:0")],
            [btn("Help", "help"), btn("About the stats", "about")],
        ]
    )


def picker_keyboard(page: int, first_id: str | None = None) -> InlineKeyboardMarkup:
    choices = [p for p in PLAYERS if p.id != first_id]
    pages = max(1, math.ceil(len(choices) / PICKER_PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    chunk = choices[page * PICKER_PAGE_SIZE:(page + 1) * PICKER_PAGE_SIZE]

    rows = []
    for p in chunk:
        data = f"c1:{p.id}" if first_id is None else f"c2:{first_id}:{p.id}"
        rows.append([btn(p.name, data)])

    nav = []
    if page > 0:
        prev = f"g1:{page - 1}" if first_id is None else f"g2:{first_id}:{page - 1}"
        nav.append(btn("◀ Prev", prev))
    nav.append(btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nxt = f"g1:{page + 1}" if first_id is None else f"g2:{first_id}:{page + 1}"
        nav.append(btn("Next ▶", nxt))
    rows.append(nav)

    bottom = [btn("Menu", "menu")]
    if first_id is not None:
        bottom.insert(0, btn("Change player A", "cmp"))
    rows.append(bottom)
    return InlineKeyboardMarkup(rows)


def result_keyboard(a: Player, b: Player) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn("Swap A and B", f"sw:{a.id}:{b.id}")],
            [btn("Change player B", f"c1:{a.id}"), btn("New comparison", "cmp")],
            [btn("Menu", "menu")],
        ]
    )


def list_view(page: int):
    pages = max(1, math.ceil(len(PLAYERS) / LIST_PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    chunk = PLAYERS[page * LIST_PAGE_SIZE:(page + 1) * LIST_PAGE_SIZE]
    lines = [f"<b>Players ({len(PLAYERS)})</b> - page {page + 1}/{pages}\n"]
    for p in chunk:
        extra = f" ({esc(p.subtitle)})" if p.subtitle else ""
        lines.append(f"• {esc(p.name)}{extra}")

    nav = []
    if page > 0:
        nav.append(btn("◀ Prev", f"list:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("Next ▶", f"list:{page + 1}"))
    rows = [nav] if nav else []
    rows.append([btn("Compare players", "cmp")])
    rows.append([btn("Menu", "menu")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


STEP1_TEXT = "<b>Step 1 of 2</b>\nChoose player A:"


def step2_text(first: Player) -> str:
    return f"<b>Step 2 of 2</b>\nPlayer A: <b>{esc(first.name)}</b>\nChoose player B:"


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
async def show(query, text: str, markup: InlineKeyboardMarkup) -> None:
    """Edit the button message; ignore Telegram's 'message is not modified'."""
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def reply(update: Update, text: str, markup: InlineKeyboardMarkup | None = None):
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=markup
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, welcome_text(), menu_keyboard())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, HELP_TEXT, menu_keyboard())


async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, about_text(), menu_keyboard())


async def players_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, markup = list_view(0)
    await reply(update, text, markup)


async def compare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args or []).strip()
    if not text:
        await reply(update, STEP1_TEXT, picker_keyboard(0))
        return

    parts = re.split(r"\s+(?:vs\.?|versus)\s+|\s*\|\s*", text, flags=re.IGNORECASE)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) != 2:
        await reply(
            update,
            "Please write two names like this:\n"
            "<code>/compare Player One vs Player Two</code>\n\n"
            "Or tap the button to choose from a list.",
            InlineKeyboardMarkup([[btn("Compare players", "cmp")]]),
        )
        return

    found = []
    for part in parts:
        player, suggestions = find_player(part)
        if player is None:
            msg = f"I could not find a single match for <b>{esc(part)}</b>."
            if suggestions:
                names = ", ".join(esc(s.name) for s in suggestions)
                msg += f"\nDid you mean: {names}?"
            msg += "\n\nUse /players to see the full list."
            await reply(
                update, msg, InlineKeyboardMarkup([[btn("Compare players", "cmp")]])
            )
            return
        found.append(player)

    a, b = found
    if a.id == b.id:
        await reply(
            update,
            "Please choose two different players.",
            InlineKeyboardMarkup([[btn("Compare players", "cmp")]]),
        )
        return
    await reply(update, build_comparison(a, b), result_keyboard(a, b))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    action = parts[0]

    try:
        if action == "noop":
            return
        if action == "menu":
            await show(query, welcome_text(), menu_keyboard())
        elif action == "help":
            await show(query, HELP_TEXT, menu_keyboard())
        elif action == "about":
            await show(query, about_text(), menu_keyboard())
        elif action == "cmp":
            await show(query, STEP1_TEXT, picker_keyboard(0))
        elif action == "list":
            text, markup = list_view(int(parts[1]))
            await show(query, text, markup)
        elif action == "g1":
            await show(query, STEP1_TEXT, picker_keyboard(int(parts[1])))
        elif action == "c1":
            first = BY_ID[parts[1]]
            await show(query, step2_text(first), picker_keyboard(0, first.id))
        elif action == "g2":
            first = BY_ID[parts[1]]
            await show(
                query, step2_text(first), picker_keyboard(int(parts[2]), first.id)
            )
        elif action in ("c2", "sw"):
            first, second = BY_ID[parts[1]], BY_ID[parts[2]]
            if action == "sw":
                first, second = second, first
            if first.id == second.id:
                raise KeyError("same player")
            await show(
                query, build_comparison(first, second), result_keyboard(first, second)
            )
        else:
            raise KeyError(action)
    except (KeyError, IndexError, ValueError):
        await show(
            query,
            "That button is out of date. Here is the main menu:\n\n" + welcome_text(),
            menu_keyboard(),
        )


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(
        update,
        "I can compare two players using stored stats. Tap a button below, "
        "or type <code>/compare Player One vs Player Two</code>.",
        menu_keyboard(),
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=context.error)


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Main menu"),
            BotCommand("compare", "Compare two players"),
            BotCommand("players", "List all players"),
            BotCommand("help", "How to use the bot"),
            BotCommand("about", "About the stats"),
        ]
    )


def main() -> None:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "The BOT_TOKEN environment variable is missing. "
            "Add it in the Render dashboard under Environment."
        )

    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("about", about_cmd))
    app.add_handler(CommandHandler("players", players_cmd))
    app.add_handler(CommandHandler("compare", compare_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT, fallback))  # text + unknown commands
    app.add_error_handler(on_error)

    log.info("Bot starting with %d players", len(PLAYERS))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
