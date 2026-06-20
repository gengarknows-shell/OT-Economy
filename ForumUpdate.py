from ossapi import Ossapi, Scope
import json
import string
import time
from pathlib import Path

# === osu! API Setup ===
from config import (
    CLIENT_ID, CLIENT_SECRET, REDIRECT_URI
)
scopes = [Scope.PUBLIC, Scope.FORUM_WRITE]
api    = Ossapi(CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, scopes = scopes)

# === Local DB ===
DB_PATH = Path("database.json")
COMMAND_HISTORY_PATH = Path("command_history.json")
INVESTMENTS_PATH = Path("investments.json")
SNAPSHOT_PATH = Path("balance_snapshot.json")
POST_ID = 10010331   # post you want to update

# ===  Build Leaderboard ===
def update_leaderboard(db):
    """Return a formatted leaderboard showing the top 10 richest users with colored ranks and economy statistics."""
    # Sort users by OT bucks (balance)
    sorted_users = sorted(
        db.items(),
        key=lambda x: x[1].get("balance", 0),
        reverse=True
    )[:10]  # only top 10

    # Colors for top ranks
    rank_colors = {
        1: "gold",     # 
        2: "silver",   # 
        3: "#cd7f32"   #  (bronze)
    }

    # --- Stat 1: Total + average OT bucks ---
    total_ot_bucks = sum(user.get("balance", 0) for user in db.values())
    user_count = len(db)
    avg_bucks = round(total_ot_bucks / user_count) if user_count > 0 else 0

    # --- Stat 2: Investment success rate (last 2 weeks) ---
    two_weeks_ago = time.time() - (14 * 24 * 3600)
    success_count = 0
    resolved_count = 0
    if INVESTMENTS_PATH.exists():
        with open(INVESTMENTS_PATH, "r") as f:
            try:
                investments = json.load(f)
            except json.JSONDecodeError:
                investments = []
        for inv in investments:
            if inv.get("status") in ("success", "failed"):
                if inv.get("resolved_at", 0) >= two_weeks_ago:
                    resolved_count += 1
                    if inv["status"] == "success":
                        success_count += 1

    if resolved_count > 0:
        success_pct = round(success_count / resolved_count * 100)
        success_str = f"{success_pct}% ({success_count}/{resolved_count} investments)"
    else:
        success_str = "No resolved investments in the last 2 weeks"

    # --- Stat 3: Richest gains this week (vs weekly balance snapshot) ---
    top_gainer_str = "No snapshot yet — gains will appear after the first weekly tick"
    if SNAPSHOT_PATH.exists():
        with open(SNAPSHOT_PATH, "r") as f:
            try:
                snapshot = json.load(f)
            except json.JSONDecodeError:
                snapshot = {}
        snapshot_balances = snapshot.get("balances", {})
        best_uid, best_gain = None, None
        for uid, user in db.items():
            prev = snapshot_balances.get(uid, 0)  # new users had 0 last week
            gain = user.get("balance", 0) - prev
            if best_gain is None or gain > best_gain:
                best_gain = gain
                best_uid = uid
        if best_uid is not None:
            name = db[best_uid]["username"]
            sign = f"+{best_gain}" if best_gain >= 0 else str(best_gain)
            top_gainer_str = f"{name} ({sign} OT Bucks)"

    # --- Stat 4: Item rarity distribution ---
    rarity_colors = {
        "common": "grey", "rare": "lime", "exotic": "cyan",
        "legendary": "red", "sacred": "gold",
    }
    rarity_counts = {r: 0 for r in rarity_colors}
    total_items = 0
    for user in db.values():
        for item in user.get("items", []):
            r = item.get("rarity", "common").lower()
            if r in rarity_counts:
                rarity_counts[r] += 1
            else:
                rarity_counts[r] = rarity_counts.get(r, 0) + 1
            total_items += 1

    if total_items > 0:
        parts = []
        for r, color in rarity_colors.items():
            count = rarity_counts.get(r, 0)
            if count:
                pct = round(count / total_items * 100)
                parts.append(f"[color={color}]{r}: {count} ({pct}%)[/color]")
        rarity_str = " | ".join(parts) if parts else "None"
    else:
        rarity_str = "No items in circulation"

    # --- Assemble output ---
    lines = ["[centre][b]OT!Economy Richest Users:[/b][/centre]"]
    for rank, (uid, user) in enumerate(sorted_users, start=1):
        color = rank_colors.get(rank)
        if color:
            lines.append(f"[color={color}]{rank}. {user['username']} — {user['balance']} OT bucks[/color]")
        else:
            lines.append(f"{rank}. {user['username']} — {user['balance']} OT bucks")

    lines.append("")
    lines.append(f"[i]Total OT Bucks in circulation: {total_ot_bucks}[/i]")
    lines.append(f"[box=[b]More Stats:[/b]][i]Average OT Bucks per user: {avg_bucks}[/i]")
    lines.append(f"[i]Investment success rate (last 2 weeks): {success_str}[/i]")
    lines.append(f"[i]Richest gains this week: {top_gainer_str}[/i]")
    lines.append(f"[i]Item rarity distribution: {rarity_str}[/i][/box]")

    return "\n".join(lines)



# ===  Build Ledger (A–Z boxes) ===
def create_ledger(db):
    """Return formatted ledger boxes for all users grouped by first letter, with colored item rarities."""

    # Define rarity → color mapping
    rarity_colors = {
        "common": "grey",
        "rare": "lime",
        "exotic": "cyan",
        "legendary": "red",
        "sacred": "gold"
    }

    import string
    alphabet = list(string.ascii_uppercase)
    ledger_lines = []
    grouped = {letter: [] for letter in alphabet}
    grouped["#"] = []

    # Group users by starting letter
    for user in db.values():
        name = user["username"]
        first = name[0].upper() if name else "#"
        if first not in grouped:
            first = "#"
        grouped[first].append(user)

    for letter in grouped:
        if not grouped[letter]:
            continue

        ledger_lines.append(f"[box={letter}]")
        for user in sorted(grouped[letter], key=lambda u: u["username"].lower()):
            item_boxes = []
            for item in user.get("items", []):
                rarity  = item.get("rarity", "common").lower()
                color   = rarity_colors.get(rarity, "grey")
                history = format_item_history(item, user["username"])
                box_title   = f"[color={color}]{item['name']}[/color] #{item['stack_id']} ×{item['quantity']}"
                box_content = (
                    f"{history}\n"
                    f"item rarity: [color={color}]{rarity}[/color]"
                )
                item_boxes.append(f"[box={box_title}]{box_content}[/box]")

            items_str = ", ".join(item_boxes) if item_boxes else "None"

            ledger_lines.append(f"[box={user['username']}]")
            ledger_lines.append(f"OT bucks : {user.get('balance', 0)}")
            ledger_lines.append(f"Items : {items_str}")
            ledger_lines.append("[/box]")
        ledger_lines.append("[/box]")

    return "\n".join(ledger_lines)


# === Item history formatter ===
def format_item_history(item, current_username):
    """
    Render the ownership chain for a single item stack as a single line.
    e.g.  PlayerA -> PlayerB (upgraded to rare) -> PlayerC
    Legacy items with no history field fall back to: Unknown origin -> current_username
    """
    history = item.get("history")
    if not history:
        return f"Unknown origin -> {current_username}"

    parts = []
    for entry in history:
        label   = entry.get("owner", "?")
        upgrades = entry.get("upgrades", [])
        if upgrades:
            label += f" (upgraded to {upgrades[-1]})"
        parts.append(label)

    return " -> ".join(parts)


# === 2. Build Recent Commands Log ===
def create_command_history():
    """Return a formatted list of the most recently executed commands (most recent first)."""
    if not COMMAND_HISTORY_PATH.exists():
        return "[i]No commands have been executed yet.[/i]"

    with open(COMMAND_HISTORY_PATH, "r", encoding="utf-8") as f:
        try:
            history = json.load(f)
        except json.JSONDecodeError:
            history = []

    if not history:
        return "[i]No commands have been executed yet.[/i]"

    lines = []
    for entry in reversed(history):  # most recent first
        timestamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.get("timestamp", 0)))
        username = entry.get("username", "unknown")
        command = entry.get("command", "")
        if entry.get("success"):
            status = "[color=lime]✅[/color]"
        else:
            status = "[color=red]❌[/color]"
        lines.append(f"{status} [{timestamp}] {username}: {command}")

    return "\n".join(lines)


# ===  Combine Everything ===
from pathlib import Path
import json

INTRO_PATH = Path("post_intro.txt")

def create_updated_post():
    """Combine static intro text from file, ledger, and leaderboard into final forum post."""
    if not DB_PATH.exists():
        return "No data available."

    with open(DB_PATH, "r", encoding="utf-8") as f:
        db = json.load(f)

    # Load your formatted intro text
    if INTRO_PATH.exists():
        with open(INTRO_PATH, "r", encoding="utf-8") as f:
            static_text = f.read().strip()
    else:
        static_text = "[b]OT!Economy[/b] missing intro text file!"

    # Build ledger + leaderboard + command history
    ledger = create_ledger(db)
    leaderboard = update_leaderboard(db)
    command_history = create_command_history()

    # Combine final post
    return (
        f"{static_text}\n"
        f"[notice]{leaderboard}\n[/notice]"
        f"[notice][centre][b]OT! Economy Ledger[/b][/centre]\n{ledger}\n[/notice]"
        f"[notice][centre][b]Recent Commands[/b][/centre]\n{command_history}\n[/notice]"
    )



# ===  Upload the post ===
def update_post():
    text = create_updated_post()
    api.forum_edit_post(post_id=POST_ID, body=text)
    print("✅ Forum post updated successfully.")
