"""Cheat item browser — search and give items / spawn vehicles for online players"""
import os
import re
import glob
import time
from flask import Blueprint, render_template, request, jsonify
from config import SERVER_DIR, WORKSHOP_CONTENT_DIR, INI_PATH
from modules.server_manager import manager

cheats_bp = Blueprint("cheats", __name__, url_prefix="/cheats")

_item_cache = None
_item_cache_mtime = 0.0


# ── Mod filtering ────────────────────────────────────────────────────────────

def _active_mod_ids():
    """Return the set of mod IDs currently listed in servertest.ini."""
    from modules.mods import read_ini
    mods, _ = read_ini()
    return set(mods)


def _read_mod_id(mod_dir):
    """Return the id= value from a mod.info file, or None."""
    info_path = os.path.join(mod_dir, "mod.info")
    try:
        with open(info_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("id="):
                    return line.strip().split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def _active_mod_dirs(active_ids):
    """Yield directories for mods whose id appears in active_ids."""
    seen = set()
    for mod_dir in glob.glob(os.path.join(WORKSHOP_CONTENT_DIR, "*/mods/*")):
        if not os.path.isdir(mod_dir) or mod_dir in seen:
            continue
        mod_id = _read_mod_id(mod_dir)
        if mod_id is not None and mod_id not in active_ids:
            continue
        seen.add(mod_dir)
        yield mod_dir, (mod_id or os.path.basename(mod_dir))


# ── Parsing ──────────────────────────────────────────────────────────────────

def _parse_scripts():
    items = []
    seen = set()

    def add(it):
        key = (it["module"], it["item"], it.get("kind", "item"))
        if key not in seen:
            seen.add(key)
            items.append(it)

    vanilla_base = os.path.join(SERVER_DIR, "media/scripts")

    # Vanilla items
    for path in glob.glob(os.path.join(vanilla_base, "*.txt")):
        for it in _parse_items(path, "Vanilla"):
            add(it)

    # Vanilla vehicle items (Jack, Lug Wrench, etc.) + vehicle definitions
    for path in glob.glob(os.path.join(vanilla_base, "vehicles/*.txt")):
        for it in _parse_items(path, "Vanilla"):
            add(it)
        for it in _parse_vehicles(path, "Vanilla"):
            add(it)

    # Active mods only
    active_ids = _active_mod_ids()
    for mod_dir, source in _active_mod_dirs(active_ids):
        for mod_scripts in _script_dirs(mod_dir):
            for path in glob.glob(os.path.join(mod_scripts, "*.txt")):
                for it in _parse_items(path, source):
                    add(it)
            for path in glob.glob(os.path.join(mod_scripts, "vehicles/*.txt")):
                for it in _parse_items(path, source):
                    add(it)
                for it in _parse_vehicles(path, source):
                    add(it)

    return items


def _script_dirs(mod_dir):
    """Return normal and versioned script dirs for a mod, preserving order."""
    dirs = [os.path.join(mod_dir, "media/scripts")]
    dirs.extend(sorted(glob.glob(os.path.join(mod_dir, "*", "media/scripts"))))

    seen = set()
    for path in dirs:
        if path not in seen and os.path.isdir(path):
            seen.add(path)
            yield path


def _module_blocks(text):
    """Yield (module_name, module_body) for each module block in a script file."""
    for m in re.finditer(r'\bmodule\s+([^\s{]+)\s*\{', text):
        name = m.group(1)
        start = m.end()
        depth, pos = 1, start
        while pos < len(text) and depth > 0:
            if text[pos] == '{':
                depth += 1
            elif text[pos] == '}':
                depth -= 1
            pos += 1
        yield name, text[start:pos - 1]


def _block_body(body, keyword, name):
    """Return the inner body of `keyword name { ... }` within body."""
    m = re.search(rf'\b{keyword}\s+{re.escape(name)}\s*\{{', body)
    if not m:
        return ""
    start = m.end()
    depth, pos = 1, start
    while pos < len(body) and depth > 0:
        if body[pos] == '{':
            depth += 1
        elif body[pos] == '}':
            depth -= 1
        pos += 1
    return body[start:pos - 1]


def _read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _prop(body, key):
    m = re.search(
        rf'^\s*{re.escape(key)}\s*=\s*(.+?)[\s,]*$',
        body, re.MULTILINE | re.IGNORECASE
    )
    return m.group(1).strip() if m else None


def _parse_items(path, source):
    items = []
    text = _read_text(path)
    for module_name, module_body in _module_blocks(text):
        for item_m in re.finditer(r'\bitem\s+([^\s{]+)\s*\{', module_body):
            item_name = item_m.group(1)
            body = _block_body(module_body, "item", item_name)
            if _is_truthy(_prop(body, "Obsolete")):
                continue
            items.append({
                "module": module_name,
                "item": item_name,
                "display": _prop(body, "DisplayName") or item_name,
                "category": _prop(body, "DisplayCategory") or "",
                "type": _prop(body, "Type") or "Normal",
                "source": source,
                "kind": "item",
            })
    return items


def _parse_vehicles(path, source):
    vehicles = []
    text = _read_text(path)
    for module_name, module_body in _module_blocks(text):
        for v_m in re.finditer(r'(?<!template\s)\bvehicle\s+([^\s{]+)\s*\{', module_body):
            vehicle_name = v_m.group(1)
            vehicles.append({
                "module": module_name,
                "item": vehicle_name,
                "display": vehicle_name,
                "category": "Vehicle",
                "type": "Vehicle",
                "source": source,
                "kind": "vehicle",
            })
    return vehicles


def _is_truthy(value):
    return str(value or "").strip().lower() in {"true", "yes", "1"}


# ── Cache (auto-invalidated when INI changes) ─────────────────────────────────

def _get_items():
    global _item_cache, _item_cache_mtime
    try:
        mtime = os.path.getmtime(INI_PATH)
    except OSError:
        mtime = 0.0
    if _item_cache is None or mtime > _item_cache_mtime:
        _item_cache = _parse_scripts()
        _item_cache_mtime = mtime
    return _item_cache


# ── Player list ───────────────────────────────────────────────────────────────

def _get_players():
    if not manager.is_alive():
        return []
    manager.send_command("players")
    time.sleep(0.4)
    recent = manager.get_buffer(last_n=60)

    # Find the LAST "Players connected" line, then collect "-Name" lines after it
    last_idx = -1
    for i, line in enumerate(recent):
        if "Players connected" in line:
            last_idx = i

    if last_idx == -1:
        return []

    players = []
    for line in recent[last_idx + 1:]:
        stripped = line.strip()
        if stripped.startswith("-"):
            players.append(stripped[1:].strip())
        elif stripped:
            break
    return players


# ── Routes ────────────────────────────────────────────────────────────────────

@cheats_bp.route("/")
def cheats_page():
    return render_template("cheats.html")


@cheats_bp.route("/api/items")
def api_items():
    return jsonify(_get_items())


@cheats_bp.route("/api/players")
def api_players():
    return jsonify(_get_players())


@cheats_bp.route("/api/give", methods=["POST"])
def api_give():
    data = request.json or {}
    module = data.get("module", "").strip()
    item = data.get("item", "").strip()
    try:
        count = max(1, int(data.get("count", 1) or 1))
    except (TypeError, ValueError):
        count = 1
    target = data.get("target", "").strip()
    kind = data.get("kind", "item")

    if not module or not item:
        return jsonify({"success": False, "message": "Missing module or item"})

    full_id = f"{module}.{item}"

    def send(player):
        if kind == "vehicle":
            return manager.send_command(f"addvehicle {_cmd_arg(full_id)} {_cmd_arg(player)}")
        return manager.send_command(f"additem {_cmd_arg(player)} {_cmd_arg(full_id)} {count}")

    if target == "__all__":
        players = _get_players()
        if not players:
            return jsonify({"success": False, "message": "No players online"})
        for player in players:
            send(player)
        action = "Spawned" if kind == "vehicle" else f"Gave {count}x"
        return jsonify({"success": True, "message": f"{action} {full_id} for {len(players)} player(s)"})

    if not target:
        return jsonify({"success": False, "message": "No player selected"})

    ok, msg = send(target)
    action = "Spawned" if kind == "vehicle" else f"Gave {count}x"
    return jsonify({
        "success": ok,
        "message": f"{action} {full_id} for {target}" if ok else msg,
    })


def _cmd_arg(value):
    """Quote a single Project Zomboid console argument."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


_PERKS = [
    "Strength", "Fitness",
    "Lightfoot", "Nimble", "Sneak", "Sprinting",
    "Aiming", "Reloading",
    "Axe", "Blunt", "LongBlade", "SmallBlade", "SmallBlunt", "Spear",
    "Maintenance",
    "Woodwork", "Cooking", "Farming", "Doctor", "Electricity",
    "Fishing", "Mechanics", "Metalwelding", "Tailoring", "Trapping",
]

# Actions that target a specific player
_PLAYER_ACTIONS = {"godmode", "maxskills", "kick", "setrole", "tp_to_me"}

# Server-wide events (no player target)
_SERVER_ACTIONS = {"chopper", "gunshot", "startrain", "stoprain", "thunderstorm"}


@cheats_bp.route("/api/power", methods=["POST"])
def api_power():
    data = request.json or {}
    action = data.get("action", "").strip()
    target = data.get("target", "").strip()
    value = data.get("value", True)
    extra = data.get("extra", "").strip()  # broadcast message or tp destination

    if action == "servermsg":
        if not extra:
            return jsonify({"success": False, "message": "No message provided"})
        manager.send_command(f'servermsg "{extra}"')
        return jsonify({"success": True, "message": f"Broadcast sent"})

    if action in _SERVER_ACTIONS:
        manager.send_command(action)
        return jsonify({"success": True, "message": f"Event triggered: {action}"})

    if action in _PLAYER_ACTIONS:
        players = _get_players() if target == "__all__" else ([target] if target else [])
        if not players:
            return jsonify({"success": False, "message": "No players online" if target == "__all__" else "No player selected"})

        val_str = str(value).lower()
        for player in players:
            if action == "godmode":
                manager.send_command(f"godmode {player} -{val_str}")
            elif action == "maxskills":
                for perk in _PERKS:
                    manager.send_command(f"addxp {player} {perk}=999999")
            elif action == "kick":
                manager.send_command(f"kickuser {player}")
            elif action == "setrole":
                role = extra if extra in ("admin", "moderator", "overseer", "gm", "observer", "none") else "none"
                manager.send_command(f'setaccesslevel "{player}" "{role}"')
            elif action == "tp_to_me":
                # teleport target TO extra (another player's name)
                if extra:
                    manager.send_command(f"teleport {player} {extra}")

        return jsonify({"success": True, "message": f"Done for {len(players)} player(s)"})

    return jsonify({"success": False, "message": "Unknown action"})
