import re
import json
import time
import copy
from pathlib import Path
import API
from API import get_username
from investments import (
    resolve_tier,
    get_reward_multiplier,
    create_investment,
    TIER_PCT,
    TIER_BASE_HOURS,
)

"""
This file handles commands and game logic.

Commands supported:
- !register
- !give [amount] {target}
- !item create {item_name} [quantity]
- !item give {item_name} {target} [stack_id]
- !item delete {item_name} {stack_id} [quantity]
- !item upgrade rarity {item_name} {stack_id} [quantity]
"""

# === CONFIG ===
DB_PATH = Path("database.json")
COMMAND_HISTORY_PATH = Path("command_history.json")
MAX_COMMAND_HISTORY = 20
BLACKLISTED_ITEMS = ["admin_sword", "banhammer"]
RARITY_ORDER = [
    ("common", 100),
    ("rare", 350),
    ("exotic", 1500),
    ("legendary", 10000),
    ("sacred", None),  # no upgrades beyond sacred
]

# Total OT Bucks invested per item unit at each rarity (creation + all upgrades to reach it)
RARITY_TOTAL_COST = {
    "common":    1,        # 1 (creation)
    "rare":      101,      # 1 + 100
    "exotic":    451,      # 1 + 100 + 350
    "legendary": 1951,     # 1 + 100 + 350 + 1500
    "sacred":    11951,    # 1 + 100 + 350 + 1500 + 10000
}
command_queue = []


# === DATABASE UTILITIES ===
def load_db():
    if DB_PATH.exists():
        with open(DB_PATH, "r") as f:
            return json.load(f)
    return {}

def save_db(db):
    with open(DB_PATH, "w") as f:
        json.dump(db, f, indent=4)


# === COMMAND HISTORY UTILITIES ===
def load_command_history():
    if COMMAND_HISTORY_PATH.exists():
        with open(COMMAND_HISTORY_PATH, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []
    return []

def save_command_history(history):
    with open(COMMAND_HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=4)

def record_command(username, raw_command, success):
    """Append an executed command to the history log, keeping only the most recent MAX_COMMAND_HISTORY entries."""
    history = load_command_history()
    history.append({
        "timestamp": time.time(),
        "username": username,
        "command": raw_command,
        "success": bool(success)
    })
    history = history[-MAX_COMMAND_HISTORY:]
    save_command_history(history)

def register_user(db, user_id, username):
    username = "".join(username.split())  # ✅ Removes *all* whitespace
    if str(user_id) not in db:
        db[str(user_id)] = {
            "username": username,
            "balance": 100,
            "items": [],
            "time_since_last_post": time.time(),
            "recent_post_times": []
        }
        save_db(db)
        print(f"✅ Registered {username} ({user_id})")
        return True
    print(f"⚠️ {username} already registered.")
    return False

def find_user_by_name(db, username):
    username = username.strip().lower()
    for uid, user in db.items():
        if user["username"].lower() == username:
            return uid
    return None

def next_stack_id(items, item_name):
    same_items = [it for it in items if it["name"].lower() == item_name.lower()]
    if not same_items:
        return "0001"
    max_id = max(int(it["stack_id"]) for it in same_items)
    return f"{max_id+1:04d}"

def next_rarity(current):
    for i, (r, cost) in enumerate(RARITY_ORDER):
        if r == current:
            if i + 1 < len(RARITY_ORDER):
                # Return next rarity, but use the current rarity's upgrade cost
                next_rarity_name = RARITY_ORDER[i + 1][0]
                return next_rarity_name, cost
            else:
                return None
    return None



# === QUOTE STRIPPING ===
def strip_quotes_stack(text):
    result = []
    stack = []
    i = 0
    while i < len(text):
        if text[i:i+7].lower() == "[quote=" or text[i:i+7].lower() == "[quote]":
            stack.append(i)
            i = text.find("]", i) + 1
        elif text[i:i+8].lower() == "[/quote]":
            if stack:
                stack.pop()
            i += 8
        else:
            if not stack:
                result.append(text[i])
            i += 1
    return ''.join(result).strip()


# === COMMAND DETECTION ===
def check_post_for_commands(postList):
    if not postList:
        return None

    for post in postList:
        raw_text = post.get("raw", "")
        dequoted_text = strip_quotes_stack(raw_text)

        for pattern, handler in COMMANDS:
            for line in dequoted_text.split("\n"):
                for match in re.finditer(pattern, line, flags=re.IGNORECASE):
                    args = [arg for arg in match.groups() if arg is not None]
                    # Strip surrounding ' or " from quoted string arguments
                    args = [arg[1:-1] if arg and arg[0] in "'\"" else arg for arg in args]
                    command_queue.append({
                        "post_id": post.get("id"),
                        "topic_id": post.get("topic_id"),
                        "user_id": post.get("user_id"),
                        "command": handler.__name__,
                        "username": "".join(API.get_username(post.get("user_id")).split()),
                        "args": args,
                        "raw": match.group(0).strip(),
                        "handler": handler
                    })
    return command_queue


# === COMMAND EXECUTION ===
def cmd_register(args, user_id, username, topic_id=None):
    db = load_db()
    return register_user(db, user_id, username)

def cmd_give(args, user_id, username, topic_id=None):
    """
    !give [amount] {target}
    Transfers OT Bucks from sender to target if valid.
    Returns True if transfer succeeded, False otherwise.
    """
    if len(args) < 2:
        print("⚠️ Usage: !give [amount] {target}")
        return False

    amount_str, target_name = args

    try:
        amount = int(amount_str)
    except ValueError:
        print("⚠️ Invalid amount format.")
        return False

    if amount <= 0:
        print("⚠️ Invalid transfer amount.")
        return False

    db = load_db()
    sender = db.get(str(user_id))
    if not sender:
        print("⚠️ Sender not registered.")
        return False

    # Find target by username or ID
    target = None
    target_id = None
    for uid, data in db.items():
        if data["username"].lower() == target_name.lower() or uid == str(target_name):
            target = data
            target_id = uid
            break

    if not target:
        print("⚠️ Target not found.")
        return False

    if sender["balance"] < amount:
        print("⚠️ Not enough balance.")
        return False

    # Perform transfer safely
    sender["balance"] -= amount
    target["balance"] += amount

    save_db(db)
    print(f"✅ {sender['username']} gave {amount} OT Bucks to {target['username']}")
    return True



def cmd_item_create(args, user_id, username, topic_id=None):
    item_name = args[0].strip()
    qty = int(args[1]) if len(args) > 1 else 1
    db = load_db()

    if item_name.lower() in [b.lower() for b in BLACKLISTED_ITEMS]:
        print("⚠️ This item name is blacklisted.")
        return False
    user = db.get(str(user_id))
    if not user:
        print("⚠️ You are not registered.")
        return False
    cost = qty
    if user["balance"] < cost:
        print("⚠️ Not enough OT bucks.")
        return False

    user["balance"] -= cost
    stack_id = next_stack_id(user["items"], item_name)
    user["items"].append({
        "name": item_name,
        "quantity": qty,
        "rarity": "common",
        "stack_id": stack_id,
        "history": [{"owner": username, "upgrades": []}]
    })
    save_db(db)
    print(f"✅ Created {qty}x {item_name} (stack {stack_id}) for {username}")
    return True


def cmd_item_give(args, user_id, username, topic_id=None):
    """
    !item give {item_name} {target} [stack_id [quantity]]

    Transfers an item to another user, updating ownership history.
    If quantity is less than the stack size the stack is split; the transferred
    portion keeps the same stack_id and inherits the full history up to this point.
    If the recipient already holds a stack with the same name and stack_id the
    quantities are simply merged.
    """
    if len(args) < 2:
        print("⚠️ Usage: !item give {item_name} {target} [stack_id [quantity]]")
        return False

    item_name = args[0]
    target    = args[1]
    stack_id  = args[2] if len(args) > 2 else None

    try:
        quantity = int(args[3]) if len(args) > 3 else None
    except ValueError:
        print("⚠️ Quantity must be a whole number.")
        return False

    if quantity is not None and quantity <= 0:
        print("⚠️ Quantity must be at least 1.")
        return False

    db = load_db()
    sender = db.get(str(user_id))
    if not sender:
        print("⚠️ Sender not registered.")
        return False

    target_id = target if target.isdigit() else find_user_by_name(db, target)
    if not target_id or str(target_id) not in db:
        print("⚠️ Target not found.")
        return False

    match = None
    for item in sender["items"]:
        if item["name"].lower() == item_name.lower() and (stack_id is None or item["stack_id"] == stack_id):
            match = item
            break
    if not match:
        print("⚠️ Item not found in your inventory.")
        return False

    give_qty = quantity if quantity is not None else match["quantity"]
    if give_qty > match["quantity"]:
        print(f"⚠️ You only have {match['quantity']}x {match['name']} in that stack.")
        return False

    recipient          = db[str(target_id)]
    recipient_username = recipient["username"]

    # Ensure sender's item has a history (legacy support)
    if not match.get("history"):
        match["history"] = [{"owner": username, "upgrades": []}]

    # Helper: find an existing stack in recipient's inventory with the same lineage
    def find_existing(items, name, sid):
        return next(
            (it for it in items
             if it["name"].lower() == name.lower() and it["stack_id"] == sid),
            None
        )

    if give_qty == match["quantity"]:
        # ── Full stack transfer ──────────────────────────────────────────────
        match["history"].append({"owner": recipient_username, "upgrades": []})
        sender["items"].remove(match)

        existing = find_existing(recipient["items"], match["name"], match["stack_id"])
        if existing:
            # Recipient already holds part of this lineage — merge quantity only
            existing["quantity"] += give_qty
        else:
            recipient["items"].append(match)

    else:
        # ── Partial transfer — split the stack ───────────────────────────────
        match["quantity"] -= give_qty

        transferred_history = copy.deepcopy(match["history"])
        transferred_history.append({"owner": recipient_username, "upgrades": []})

        existing = find_existing(recipient["items"], match["name"], match["stack_id"])
        if existing:
            existing["quantity"] += give_qty
        else:
            recipient["items"].append({
                "name":     match["name"],
                "quantity": give_qty,
                "rarity":   match["rarity"],
                "stack_id": match["stack_id"],
                "history":  transferred_history,
            })

    save_db(db)
    print(f"✅ Gave {give_qty}x {match['name']} (#{match['stack_id']}) to {recipient_username}")
    return True

def cmd_item_delete(args, user_id, username, topic_id=None):
    item_name, stack_id, *qty_arg = args
    qty = int(qty_arg[0]) if qty_arg else None
    db = load_db()
    user = db.get(str(user_id))
    if not user:
        print("⚠️ Not registered.")
        return False

    for item in user["items"]:
        if item["name"].lower() == item_name.lower() and item["stack_id"] == stack_id:
            qty_deleted = item["quantity"] if (qty is None or qty >= item["quantity"]) else qty
            value_per_unit = RARITY_TOTAL_COST.get(item["rarity"].lower(), 1)
            refund = round(qty_deleted * value_per_unit * 0.5)
            user["balance"] += refund

            if qty is None or qty >= item["quantity"]:
                user["items"].remove(item)
                print(f"✅ Deleted stack {stack_id} of {item_name} — refunded {refund} OT Bucks")
                save_db(db)
                return True
            else:
                item["quantity"] -= qty
                print(f"✅ Deleted {qty_deleted}x {item_name} from stack {stack_id} — refunded {refund} OT Bucks")
                save_db(db)
                return True

    else:
        print("⚠️ No matching item found.")
        return False



def cmd_item_upgrade(args, user_id, username, topic_id=None):
    item_name, stack_id, *qty_arg = args
    qty = int(qty_arg[0]) if qty_arg else None
    db = load_db()
    user = db.get(str(user_id))
    if not user:
        print("⚠️ Not registered.")
        return False

    for item in user["items"]:
        if item["name"].lower() == item_name.lower() and item["stack_id"] == stack_id:
            rarity_next = next_rarity(item["rarity"])
            if not rarity_next:
                print("⚠️ Already at highest rarity.")
                return False
            new_rarity, cost_per_item = rarity_next
            qty = qty or item["quantity"]
            cost = int(cost_per_item) * int(qty)
            if user["balance"] < cost:
                print("⚠️ Not enough OT bucks.")
                return False
            user["balance"] -= cost
            item["rarity"] = new_rarity

            # Record upgrade in ownership history
            if not item.get("history"):
                item["history"] = [{"owner": username, "upgrades": [new_rarity]}]
            else:
                last = item["history"][-1]
                if last["owner"].lower() == username.lower():
                    last.setdefault("upgrades", []).append(new_rarity)
                else:
                    item["history"].append({"owner": username, "upgrades": [new_rarity]})

            save_db(db)
            print(f"✅ Upgraded {item_name} (stack {stack_id}) to {new_rarity} rarity.")
            return True
    print("⚠️ Item not found.")
    return False


def cmd_invest(args, user_id, username, topic_id=None):
    """
    !invest [amount] [tier] [hours]

    Invests OT Bucks on a thread reaching a target number of new unique
    posters within the given time limit.

    Tiers: 10 / 20 / 30 / 40 / 50  (raw value is rounded DOWN to nearest tier)
    Payouts on success: 3% / 7% / 12% / 25% / 50% of stake (before time scaling)
    Time scaling per tier — full reward window / half / quarter / max:
        Tier 10:  ≤12h / ≤24h / ≤36h  — fail if >36h
        Tier 20:  ≤24h / ≤48h / ≤72h  — fail if >72h
        Tier 30:  ≤48h / ≤96h / ≤144h — fail if >144h
        Tier 40:  ≤72h / ≤144h / ≤216h — fail if >216h
        Tier 50: ≤168h / ≤336h / ≤504h — fail if >504h
    On failure: refund = round(stake × posters_reached / tier_target)
    """
    if topic_id is None:
        print("⚠️ !invest could not determine the topic — post may be missing topic context.")
        return False

    if len(args) < 3:
        print("⚠️ Usage: !invest [amount] [tier] [hours]")
        return False

    try:
        invest_amount    = int(args[0])
        raw_tier         = int(args[1])
        time_limit_hours = int(args[2])
    except ValueError:
        print("⚠️ !invest — all arguments must be whole numbers.")
        return False

    if invest_amount <= 0:
        print("⚠️ Investment amount must be a positive number.")
        return False

    if time_limit_hours < 1:
        print("⚠️ Time limit must be at least 1 hour.")
        return False

    # Resolve and validate tier
    tier = resolve_tier(raw_tier)
    if tier is None:
        print(f"⚠️ Invalid tier {raw_tier}. Must be between 10 and 50 inclusive.")
        return False

    if tier != raw_tier:
        print(f"ℹ️ Tier {raw_tier} rounded down to tier {tier}.")

    # Validate time limit and determine reward scaling
    reward_multiplier = get_reward_multiplier(tier, time_limit_hours)
    if reward_multiplier is None:
        base = TIER_BASE_HOURS[tier]
        max_hours = base * 3
        print(
            f"⚠️ Time limit {time_limit_hours}h is too long for tier {tier}. "
            f"Maximum is {max_hours}h ({base}h = full, {base*2}h = half, {base*3}h = quarter)."
        )
        return False

    # Create the investment (validates balance, snapshots baseline, deducts stake)
    success, result = create_investment(
        user_id, username, topic_id,
        invest_amount, tier, time_limit_hours, reward_multiplier
    )

    if not success:
        print(f"⚠️ Investment failed: {result}")
        return False

    potential_reward = round(invest_amount * TIER_PCT[tier] * reward_multiplier)
    print(
        f"✅ {username} invested {invest_amount} OT Bucks on tier {tier} "
        f"({tier} new unique posters in {time_limit_hours}h | "
        f"×{reward_multiplier} multiplier | "
        f"potential reward: +{potential_reward} OT Bucks)"
    )
    return True


# === COMMAND REGEX ===
# UINT_REGEX  — captures a non-negative integer
# STR_REGEX   — captures either a quoted string ('...' or "...") allowing spaces,
#               or a plain single word (min 1 char, must not start with a quote).
#               The [^\s\"'] first-char class prevents whitespace and mismatched quotes.
UINT_REGEX = r"(\d+)"
STR_REGEX  = r"((?:\")[^\"\v\f\r]+(?:\")|(?:')[^\'\v\f\r]+(?:')|[^\s\"']\S*)"

COMMANDS = [
    (r"^\s*!register\s*$",                                                                                          cmd_register),
    (r"^\s*!give\s+"          + UINT_REGEX + r"\s+" + STR_REGEX + r"\s*$",                                         cmd_give),
    (r"^\s*!item\s+create\s+" + STR_REGEX  + r"(?:\s+" + UINT_REGEX + r")?\s*$",                                   cmd_item_create),
    (r"^\s*!item\s+give\s+"   + STR_REGEX  + r"\s+" + STR_REGEX + r"(?:\s+" + UINT_REGEX + r"(?:\s+" + UINT_REGEX + r")?)?\s*$",              cmd_item_give),
    (r"^\s*!item\s+delete\s+" + STR_REGEX  + r"\s+" + UINT_REGEX + r"(?:\s+" + UINT_REGEX + r")?\s*$",             cmd_item_delete),
    (r"^\s*!item\s+upgrade\s+rarity\s+" + STR_REGEX + r"\s+" + UINT_REGEX + r"(?:\s+" + UINT_REGEX + r")?\s*$",   cmd_item_upgrade),
    (r"^\s*!invest\s+"        + UINT_REGEX + r"\s+" + UINT_REGEX + r"\s+" + UINT_REGEX + r"\s*$",                  cmd_invest),
]


# === QUEUE PROCESSING ===
def process_command_queue(queue):
    """
    Executes all commands in the queue.
    Returns True if any command changed the database (e.g. balance, items), otherwise False.
    """
    changed = False  # Track if anything modified the economy

    while queue:
        cmd = queue.pop(0)
        handler = cmd["handler"]
        args = cmd["args"]
        user_id = cmd["user_id"]
        username = cmd.get("username", "unknown")
        raw_command = cmd.get("raw", handler.__name__)
        topic_id = cmd.get("topic_id")

        print(f"⚙️ Executing {handler.__name__} from {username} ({user_id}) with args {args}")

        try:
            # Execute command handler
            result = handler(args, user_id, username, topic_id)

            # If handler reports a change, mark as changed
            if result:
                changed = True

            record_command(username, raw_command, result)

        except Exception as e:
            print(f"❌ Error executing {handler.__name__}: {e}")
            record_command(username, raw_command, False)

    return changed
