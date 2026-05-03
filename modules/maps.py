"""Map management — visualize cell coverage, add/remove maps, detect overlaps"""
import os
import re
import glob
import html
import time
from flask import Blueprint, render_template, request, jsonify
from config import SERVER_DIR, WORKSHOP_CONTENT_DIR, INI_PATH, SAVES_DIR
from modules.server_manager import manager

maps_bp = Blueprint("maps", __name__, url_prefix="/maps")

VANILLA_MAPS_DIR = os.path.join(SERVER_DIR, "media", "maps")
BASE_MAP_ID = "Muldraugh, KY"
TOP_PRIORITY_MAP_IDS = ("vehicle_interior",)
SERVER_NAME = os.path.splitext(os.path.basename(INI_PATH))[0]
SPAWN_REGIONS_PATH = os.path.join(os.path.dirname(INI_PATH), f"{SERVER_NAME}_spawnregions.lua")
SAVE_WORLD_DIR = os.path.join(SAVES_DIR, "Multiplayer", SERVER_NAME)

_CELL_RE = re.compile(r'^(\d+)_(\d+)\.lotheader$')
_MAP_FOLDER_RE = re.compile(r'Map\s*Folders?\s*[:=]\s*([^\r\n\[]+)', re.IGNORECASE)
_SPAWN_FILE_RE = re.compile(r'\bfile\s*=\s*["\']([^"\']+)["\']')
_last_player_positions = {}


# ── INI helpers ───────────────────────────────────────────────────────────────

def read_map_ini():
    """Return list of map folder names from Map= in servertest.ini."""
    try:
        with open(INI_PATH) as f:
            for line in f:
                if line.startswith("Map="):
                    val = line.strip().split("=", 1)[1]
                    return _normalize_map_order([m.strip() for m in val.split(";") if m.strip()])
    except FileNotFoundError:
        pass
    return [BASE_MAP_ID]


def _normalize_map_order(maps):
    """Keep special maps first, custom maps next, and the vanilla base map last."""
    seen = set()
    clean = []
    for m in maps:
        if m and m not in seen:
            clean.append(m)
            seen.add(m)

    top = [m for m in TOP_PRIORITY_MAP_IDS if m in seen]
    middle = [m for m in clean if m not in TOP_PRIORITY_MAP_IDS and m != BASE_MAP_ID]
    return top + middle + [BASE_MAP_ID]


def write_map_ini(maps):
    """Update Map= in servertest.ini."""
    maps = _normalize_map_order(maps)
    try:
        with open(INI_PATH) as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []

    found = False
    parent = os.path.dirname(INI_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(INI_PATH, "w") as f:
        for line in lines:
            if line.startswith("Map="):
                f.write("Map=" + ";".join(maps) + "\n")
                found = True
            else:
                f.write(line)
        if not found:
            f.write("Map=" + ";".join(maps) + "\n")


# ── Map scanning ──────────────────────────────────────────────────────────────

def _get_cells(map_dir):
    """Return sorted list of [x, y] pairs from X_Y.lotheader files."""
    cells = []
    try:
        for fname in os.listdir(map_dir):
            m = _CELL_RE.match(fname)
            if m:
                cells.append([int(m.group(1)), int(m.group(2))])
    except OSError:
        pass
    return sorted(cells)


def _has_worldmap(map_dir):
    """Return True when a map folder contributes in-game map data."""
    return (
        os.path.isfile(os.path.join(map_dir, "worldmap.xml")) or
        os.path.isfile(os.path.join(map_dir, "worldmap.xml.bin"))
    )


def _read_map_title(map_dir, root_dir=None):
    """Return title= from map.info, resolving 'See <path>' indirection if root_dir given."""
    try:
        with open(os.path.join(map_dir, "map.info"), encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.lower().startswith("title="):
                    title = line.strip().split("=", 1)[1].strip()
                    if title.startswith("See ") and root_dir:
                        ref_path = os.path.join(root_dir, title[4:])
                        try:
                            with open(ref_path, encoding="utf-8", errors="replace") as rf:
                                return rf.read().strip()
                        except OSError:
                            pass
                    return title
    except OSError:
        pass
    return None


def _read_map_lots(map_dir):
    """Return lots= folder references from map.info."""
    lots = []
    try:
        with open(os.path.join(map_dir, "map.info"), encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.lower().startswith("lots="):
                    value = line.strip().split("=", 1)[1].strip()
                    if value:
                        lots.extend([p.strip() for p in value.split(";") if p.strip()])
    except OSError:
        pass
    return lots


def _read_mod_name(mod_dir):
    """Return name= from mod.info, or None."""
    try:
        with open(os.path.join(mod_dir, "mod.info"), encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("name="):
                    return line.strip().split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def _read_mod_ids_from_mod_dir(mod_dir):
    """Return id= values from a mod.info file."""
    ids = []
    try:
        with open(os.path.join(mod_dir, "mod.info"), encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("id="):
                    mid = line.strip().split("=", 1)[1].strip()
                    if mid:
                        ids.append(mid)
    except OSError:
        pass
    return ids


def _scan_maps_dir(maps_dir, source, out, seen_ids, root_dir=None):
    """Scan a media/maps/ directory and append map dicts to out."""
    try:
        for map_name in sorted(os.listdir(maps_dir)):
            map_dir = os.path.join(maps_dir, map_name)
            if not os.path.isdir(map_dir):
                continue
            cells = _get_cells(map_dir)
            if not cells and not _has_worldmap(map_dir):
                continue
            if map_name in seen_ids:
                continue
            seen_ids.add(map_name)
            title = _read_map_title(map_dir, root_dir=root_dir) or map_name
            out.append({
                "id": map_name,
                "title": title,
                "source": source,
                "cells": cells,
                "hasCells": bool(cells),
                "hasWorldmap": _has_worldmap(map_dir),
                "lots": _read_map_lots(map_dir),
            })
    except OSError:
        pass


def scan_all_maps():
    """
    Return all available maps (vanilla + workshop).
    Each entry: { id, title, source, cells: [[x,y],...] }
    id = directory name, which matches the Map= INI value.
    """
    maps = []
    seen_ids = set()

    _scan_maps_dir(VANILLA_MAPS_DIR, "Vanilla", maps, seen_ids, root_dir=SERVER_DIR)

    try:
        for wid_dir in sorted(glob.glob(os.path.join(WORKSHOP_CONTENT_DIR, "*"))):
            if not os.path.isdir(wid_dir):
                continue
            for mod_dir in sorted(glob.glob(os.path.join(wid_dir, "mods", "*"))):
                if not os.path.isdir(mod_dir):
                    continue
                maps_dir = os.path.join(mod_dir, "media", "maps")
                if not os.path.isdir(maps_dir):
                    continue
                source = _read_mod_name(mod_dir) or os.path.basename(mod_dir)
                _scan_maps_dir(maps_dir, source, maps, seen_ids, root_dir=mod_dir)
    except OSError:
        pass

    return maps


def _scan_workshop_map_sources():
    """Return downloaded workshop map folders with file paths and mod metadata."""
    sources = []
    try:
        wid_dirs = sorted(glob.glob(os.path.join(WORKSHOP_CONTENT_DIR, "*")))
    except OSError:
        return sources

    for wid_dir in wid_dirs:
        if not os.path.isdir(wid_dir):
            continue
        workshop_id = os.path.basename(wid_dir)
        for mod_dir in sorted(glob.glob(os.path.join(wid_dir, "mods", "*"))):
            if not os.path.isdir(mod_dir):
                continue
            maps_dir = os.path.join(mod_dir, "media", "maps")
            if not os.path.isdir(maps_dir):
                continue
            mod_name = _read_mod_name(mod_dir) or os.path.basename(mod_dir)
            mod_ids = _read_mod_ids_from_mod_dir(mod_dir)
            for map_name in sorted(os.listdir(maps_dir)):
                map_dir = os.path.join(maps_dir, map_name)
                if not os.path.isdir(map_dir):
                    continue
                cells = _get_cells(map_dir)
                has_worldmap = _has_worldmap(map_dir)
                if not cells and not has_worldmap:
                    continue
                sources.append({
                    "id": map_name,
                    "title": _read_map_title(map_dir, root_dir=mod_dir) or map_name,
                    "source": mod_name,
                    "workshopId": workshop_id,
                    "modDir": mod_dir,
                    "modIds": mod_ids,
                    "path": map_dir,
                    "cells": cells,
                    "hasCells": bool(cells),
                    "hasWorldmap": has_worldmap,
                    "lots": _read_map_lots(map_dir),
                    "spawnpointsFile": os.path.join(map_dir, "spawnpoints.lua"),
                    "hasSpawnpoints": os.path.isfile(os.path.join(map_dir, "spawnpoints.lua")),
                    "hasSpawnregions": os.path.isfile(os.path.join(map_dir, "spawnregions.lua")),
                })
    return sources


def find_overlaps(active_ids, all_maps):
    """Return list of [x, y] cells covered by 2+ active maps."""
    from collections import Counter
    counter = Counter()
    active_set = set(active_ids)
    for m in all_maps:
        if m["id"] in active_set and m["id"] != BASE_MAP_ID:
            for cell in m["cells"]:
                counter[tuple(cell)] += 1
    return [[x, y] for (x, y), count in counter.items() if count > 1]


def _detect_map_names_for_wid(workshop_id):
    """
    If this workshop content is already downloaded, return the map folder names
    found inside its mods (used to auto-populate Map= after install).
    """
    names = []
    content_dir = os.path.join(WORKSHOP_CONTENT_DIR, str(workshop_id))
    if not os.path.isdir(content_dir):
        return names
    for mod_dir in glob.glob(os.path.join(content_dir, "mods", "*")):
        maps_base = os.path.join(mod_dir, "media", "maps")
        if not os.path.isdir(maps_base):
            continue
        for map_name in os.listdir(maps_base):
            map_dir = os.path.join(maps_base, map_name)
            if os.path.isdir(map_dir) and (_get_cells(map_dir) or _has_worldmap(map_dir)):
                names.append(map_name)
    return names


def _read_mod_ids_from_disk(workshop_id):
    """Read id= from mod.info files in already-downloaded workshop content."""
    ids = []
    content_dir = os.path.join(WORKSHOP_CONTENT_DIR, str(workshop_id))
    if not os.path.isdir(content_dir):
        return ids
    for mod_dir in glob.glob(os.path.join(content_dir, "mods", "*")):
        try:
            with open(os.path.join(mod_dir, "mod.info"), encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("id="):
                        mid = line.strip().split("=", 1)[1].strip()
                        if mid:
                            ids.append(mid)
                            break
        except OSError:
            pass
    return ids


def _related_map_names(map_names):
    """
    Expand selected map folders to sibling folders linked by map.info lots=.

    Some workshop maps split terrain and in-game worldmap data into separate
    folders. Bedford Falls is one example: North/South/West have no terrain
    cells, but their map.info files point back to BedfordFalls via lots=.
    """
    selected = {m for m in map_names if m}
    if not selected:
        return []

    sources = _scan_workshop_map_sources()
    by_mod_dir = {}
    for source in sources:
        by_mod_dir.setdefault(source.get("modDir"), []).append(source)

    expanded = set(selected)
    for group in by_mod_dir.values():
        ids = {s["id"] for s in group}
        if not ids.intersection(selected):
            continue

        graph = {mid: set() for mid in ids}
        for source in group:
            for lot in source.get("lots", []):
                if lot in ids:
                    graph[source["id"]].add(lot)
                    graph[lot].add(source["id"])

        stack = list(ids.intersection(selected))
        while stack:
            current = stack.pop()
            if current in expanded:
                pass
            expanded.add(current)
            for linked in graph.get(current, set()):
                if linked not in expanded:
                    stack.append(linked)

    ordered = []
    for name in list(map_names) + [s["id"] for s in sources]:
        if name in expanded and name not in ordered:
            ordered.append(name)
    return ordered


def _read_spawn_region_files():
    files = set()
    try:
        with open(SPAWN_REGIONS_PATH, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return files
    for match in _SPAWN_FILE_RE.finditer(text):
        files.add(match.group(1).replace("\\", "/"))
    return files


def _spawn_file_for_map(map_id):
    return f"media/maps/{map_id}/spawnpoints.lua"


def _append_spawn_regions(entries):
    """Append missing spawnpoint files to servertest_spawnregions.lua."""
    entries = [e for e in entries if e.get("id") and e.get("file")]
    if not entries:
        return []

    existing = _read_spawn_region_files()
    missing = [e for e in entries if e["file"] not in existing]
    if not missing:
        return []

    try:
        with open(SPAWN_REGIONS_PATH, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        text = "function SpawnRegions()\n\treturn {\n\t}\nend\n"

    lines = [f'\t\t{{ name = "{e["id"]}", file = "{e["file"]}" }},' for e in missing]
    insert = "\n".join(lines) + "\n"
    marker = "\t}\nend"
    if marker in text:
        text = text.replace(marker, insert + marker, 1)
    else:
        text = text.rstrip() + "\n" + insert

    os.makedirs(os.path.dirname(SPAWN_REGIONS_PATH), exist_ok=True)
    with open(SPAWN_REGIONS_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    return [e["id"] for e in missing]


def _generated_cells_for_map(map_entry):
    generated = []
    if not os.path.isdir(SAVE_WORLD_DIR):
        return generated
    for x, y in map_entry.get("cells", []):
        if os.path.exists(os.path.join(SAVE_WORLD_DIR, f"chunkdata_{x}_{y}.bin")):
            generated.append([x, y])
    return generated


def build_map_health():
    active = read_map_ini()
    active_set = set(active)
    all_maps = scan_all_maps()
    map_by_id = {m["id"]: m for m in all_maps}
    sources = _scan_workshop_map_sources()
    spawn_files = _read_spawn_region_files()

    issues = []
    actions = []

    inactive_reported = set()
    for source in sources:
        if source["id"] not in active_set:
            if source["id"] in inactive_reported:
                continue
            inactive_reported.add(source["id"])
            linked_to_active = bool(set(source.get("lots", [])).intersection(active_set))
            issues.append({
                "type": "map_not_active",
                "severity": "warn",
                "title": f"{source['id']} is downloaded but not active in Map=",
                "detail": (
                    "This looks like a companion in-game map folder for an active map. Add it to keep the in-game map overlay aligned."
                    if linked_to_active else
                    "The mod is installed, but this map folder will not load until it is added before Muldraugh, KY."
                ),
                "mapId": source["id"],
                "workshopId": source["workshopId"],
                "action": "activate_map",
            })

    for map_id in active:
        if map_id == BASE_MAP_ID or map_id in TOP_PRIORITY_MAP_IDS:
            continue
        if map_id not in map_by_id:
            issues.append({
                "type": "active_map_missing_files",
                "severity": "error",
                "title": f"{map_id} is active in Map= but no map files were found",
                "detail": "This usually means the workshop item is not downloaded on the dedicated server or the Map Folder name is wrong.",
                "mapId": map_id,
            })

    checked_active_maps = set()
    for source in sources:
        if source["id"] not in active_set:
            continue
        if source["id"] in checked_active_maps:
            continue
        checked_active_maps.add(source["id"])
        generated = _generated_cells_for_map(source)
        if generated:
            issues.append({
                "type": "save_cells_already_generated",
                "severity": "error",
                "title": f"{source['id']} has cells that already exist in this save",
                "detail": "If this map was added after the world was created, those cells may stay as old terrain/trees. A new world is the cleanest fix.",
                "mapId": source["id"],
                "cells": generated[:12],
                "cellCount": len(generated),
            })

        expected_spawn_file = _spawn_file_for_map(source["id"])
        if source["hasSpawnpoints"] and expected_spawn_file not in spawn_files:
            issues.append({
                "type": "spawn_region_missing",
                "severity": "warn",
                "title": f"{source['id']} has spawnpoints but is missing from {SERVER_NAME}_spawnregions.lua",
                "detail": "Players will not see this map as a spawn location until the spawn region entry is added.",
                "mapId": source["id"],
                "file": expected_spawn_file,
                "action": "add_spawn_region",
            })
            actions.append({"mapId": source["id"], "file": expected_spawn_file})

    return {
        "ok": len([i for i in issues if i["severity"] == "error"]) == 0,
        "issues": issues,
        "spawnRegionActions": actions,
        "spawnRegionsPath": SPAWN_REGIONS_PATH,
        "saveWorldDir": SAVE_WORLD_DIR,
    }


def _parse_map_folders(description):
    """Extract Map Folder: values from a Steam description."""
    names = []
    text = html.unescape(description or "")
    text = re.sub(r'<br\s*/?>', "\n", text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', "", text)
    text = re.sub(r'\[[^\]]+\]', "", text)
    for match in _MAP_FOLDER_RE.finditer(text):
        value = match.group(1).strip(" \t:-")
        # Descriptions sometimes use separators for multiple folder names, but
        # commas are valid inside names like "Muldraugh, KY".
        for part in re.split(r'\s*(?:;|\||/)\s*', value):
            part = part.strip(" \t`'\"")
            if part and part.lower() not in ("none", "n/a"):
                names.append(part)
    return list(dict.fromkeys(names))


def _activate_map_names(map_names):
    """Add map folder names to Map= and return the names that were added."""
    map_names = _related_map_names(map_names)
    if not map_names:
        return []
    active = read_map_ini()
    added = []
    for mname in map_names:
        if mname not in active:
            active.append(mname)
            added.append(mname)
    if added:
        write_map_ini(active)
    return added


def _remove_map_names(map_names):
    """Remove map folder names and linked companion folders from Map=."""
    map_names = [m for m in _related_map_names(map_names) if m not in TOP_PRIORITY_MAP_IDS and m != BASE_MAP_ID]
    if not map_names:
        return []
    active = read_map_ini()
    removed = [m for m in active if m in set(map_names)]
    if removed:
        write_map_ini([m for m in active if m not in set(map_names)])
    return removed


def _quote_cmd_arg(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _online_players():
    try:
        from modules.cheats import _get_players
        return _get_players()
    except Exception:
        return []


# ── Routes ────────────────────────────────────────────────────────────────────

@maps_bp.route("/")
def maps_page():
    return render_template("maps.html")


@maps_bp.route("/api/data")
def api_data():
    active = read_map_ini()
    all_maps = scan_all_maps()
    overlapping = find_overlaps(active, all_maps)

    from modules.mods import read_ini as read_mod_ini
    _, workshop_ids = read_mod_ini()

    # Maps that are downloaded (files on disk) but not yet added to Map=
    workshop_set = set(workshop_ids)
    pending_ids = set()
    for wid in workshop_set:
        for mname in _detect_map_names_for_wid(wid):
            if mname not in active:
                pending_ids.add(mname)
    pending_maps = [m["id"] for m in all_maps if m["id"] in pending_ids]

    return jsonify({
        "active": active,
        "maps": all_maps,
        "overlapping": overlapping,
        "installedWorkshopIds": workshop_ids,
        "pendingMaps": pending_maps,
        "baseMap": BASE_MAP_ID,
    })


@maps_bp.route("/api/health")
def api_health():
    return jsonify(build_map_health())


@maps_bp.route("/api/spawn-region", methods=["POST"])
def api_spawn_region():
    data = request.json or {}
    map_id = str(data.get("mapId") or "").strip()
    if not map_id:
        return jsonify({"success": False, "message": "Missing map ID"})

    sources = {s["id"]: s for s in _scan_workshop_map_sources()}
    source = sources.get(map_id)
    if not source or not source.get("hasSpawnpoints"):
        return jsonify({"success": False, "message": "No spawnpoints.lua found for this map"}), 404

    try:
        added = _append_spawn_regions([{"id": map_id, "file": _spawn_file_for_map(map_id)}])
    except OSError as e:
        return jsonify({"success": False, "message": f"Could not update spawn regions: {e}"}), 500

    return jsonify({
        "success": True,
        "added": added,
        "message": "Spawn region added. Restart server to apply." if added else "Spawn region already exists.",
    })


@maps_bp.route("/api/players")
def api_players():
    names = _online_players()
    now = time.time()
    players = []
    for name in names:
        pos = _last_player_positions.get(name)
        players.append({
            "name": name,
            "x": pos["x"] if pos else None,
            "y": pos["y"] if pos else None,
            "z": pos["z"] if pos else None,
            "cellX": pos["cellX"] if pos else None,
            "cellY": pos["cellY"] if pos else None,
            "updatedAgo": round(now - pos["updatedAt"], 1) if pos else None,
            "source": "panel-teleport" if pos else "online",
        })
    return jsonify({
        "players": players,
        "positionSource": "panel-teleport-cache",
        "liveCoordinates": False,
    })


@maps_bp.route("/api/teleport", methods=["POST"])
def api_teleport():
    data = request.json or {}
    players = data.get("players") or []
    if isinstance(players, str):
        players = [players]
    players = [str(p).strip() for p in players if str(p).strip()]
    if not players:
        return jsonify({"success": False, "message": "No players selected"})

    try:
        cell_x = int(data.get("cellX"))
        cell_y = int(data.get("cellY"))
        z = int(data.get("z", 0) or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "No map location selected"})

    tile_x = int(data.get("x") or (cell_x * 300 + 150))
    tile_y = int(data.get("y") or (cell_y * 300 + 150))

    sent = []
    failed = []
    for player in players:
        cmd = f"teleportto {_quote_cmd_arg(player)} {tile_x},{tile_y},{z}"
        ok, msg = manager.send_command(cmd)
        if ok:
            sent.append(player)
            _last_player_positions[player] = {
                "x": tile_x,
                "y": tile_y,
                "z": z,
                "cellX": cell_x,
                "cellY": cell_y,
                "updatedAt": time.time(),
            }
        else:
            failed.append({"player": player, "message": msg})

    return jsonify({
        "success": bool(sent) and not failed,
        "sent": sent,
        "failed": failed,
        "x": tile_x,
        "y": tile_y,
        "z": z,
        "cellX": cell_x,
        "cellY": cell_y,
        "message": f"Teleported {len(sent)} player(s) to {tile_x},{tile_y},{z}" if sent else "Teleport failed",
    })


@maps_bp.route("/api/add", methods=["POST"])
def api_add():
    data = request.json or {}
    map_id = str(data.get("id") or "").strip()
    if not map_id:
        return jsonify({"success": False, "message": "Missing map ID"})
    try:
        added = _activate_map_names([map_id])
        return jsonify({"success": True, "active": read_map_ini(), "added": added})
    except OSError as e:
        return jsonify({"success": False, "message": f"Could not update Map=: {e}"}), 500


@maps_bp.route("/api/remove", methods=["POST"])
def api_remove():
    data = request.json or {}
    map_id = str(data.get("id") or "").strip()
    if not map_id:
        return jsonify({"success": False, "message": "Missing map ID"})
    if map_id == BASE_MAP_ID:
        return jsonify({"success": False, "message": f"{BASE_MAP_ID} must stay first and cannot be removed"})
    try:
        removed = _remove_map_names([map_id])
        return jsonify({"success": True, "active": read_map_ini(), "removed": removed})
    except OSError as e:
        return jsonify({"success": False, "message": f"Could not update Map=: {e}"}), 500


@maps_bp.route("/api/workshop-search")
def api_workshop_search():
    """Search Steam Workshop for map mods (always filtered to Map tag)."""
    from modules.mods import steam_search_workshop
    q = request.args.get("q", "").strip()
    page = int(request.args.get("page", 1))
    sort = request.args.get("sort", "trend")
    result = steam_search_workshop(query=q, category="Map", page=page, sort=sort)
    return jsonify(result)


@maps_bp.route("/api/install", methods=["POST"])
def api_install():
    """
    Install a workshop map mod:
      1. Adds to WorkshopItems= + Mods= (downloads on server restart)
      2. If files are already present, auto-adds map folder(s) to Map=
    """
    from modules.mods import (
        steam_api_get_mod, resolve_transitive_dependencies,
        read_ini as read_mod_ini, write_ini as write_mod_ini,
    )

    data = request.json or {}
    wid = str(data.get("workshopId", "")).strip()
    custom_mod_id = str(data.get("modId") or "").strip()

    if not wid.isdigit():
        return jsonify({"success": False, "message": "Invalid Workshop ID"})

    info = steam_api_get_mod(wid)
    if not info:
        return jsonify({"success": False, "message": "Not found on Steam"})

    # Resolve mod IDs: custom input → already-downloaded mod.info → Steam description
    disk_mod_ids = _read_mod_ids_from_disk(wid)
    disk_map_names = _detect_map_names_for_wid(wid)
    desc_map_names = _parse_map_folders(info.get("description", ""))
    if custom_mod_id:
        all_mod_ids = [custom_mod_id]
    elif disk_mod_ids:
        all_mod_ids = disk_mod_ids
    else:
        all_mod_ids = info["modIds"]  # may be [] for map mods without "Mod ID:" in description

    mods, workshops = read_mod_ini()
    if wid in workshops:
        if all_mod_ids:
            pairs = [(m, w) for m, w in zip(mods, workshops) if w != wid]
            pairs.extend((mid, wid) for mid in all_mod_ids)
            mods, workshops = (list(zip(*pairs)) if pairs else ([], []))
            write_mod_ini(list(mods), list(workshops))
        auto_added_maps = _activate_map_names(disk_map_names or desc_map_names)
        return jsonify({
            "success": True,
            "name": info["name"],
            "alreadyInstalled": True,
            "modId": all_mod_ids[0] if all_mod_ids else None,
            "autoAddedMaps": auto_added_maps,
            "needsDownload": len(disk_map_names) == 0,
        })

    if not all_mod_ids:
        return jsonify({
            "success": False,
            "message": "Mod ID not found",
            "needManualModId": True,
            "name": info["name"],
            "mapFolders": desc_map_names,
        })

    for mid in all_mod_ids:
        mods.append(mid)
        workshops.append(wid)

    # Auto-add required workshop dependencies (transitive — same logic as mods tab)
    discovered = resolve_transitive_dependencies(wid, set(workshops) | {wid})
    for dep in discovered:
        if not dep["modIds"]:
            continue
        for mid in dep["modIds"]:
            mods.append(mid)
            workshops.append(dep["workshopId"])

    write_mod_ini(mods, workshops)

    # If mod files are already downloaded, auto-add map names to Map=
    map_names = disk_map_names or desc_map_names
    auto_added_maps = _activate_map_names(map_names)

    return jsonify({
        "success": True,
        "name": info["name"],
        "modId": all_mod_ids[0] if all_mod_ids else None,
        "autoAddedMaps": auto_added_maps,
        "needsDownload": len(disk_map_names) == 0,
    })
