"""Sandbox settings - parses Lua file with its embedded comments"""
import re
import os
from flask import Blueprint, render_template, request, jsonify
from config import SANDBOX_PATH, INI_PATH, SURVIVAL_LUA_PATH

sandbox_bp = Blueprint("sandbox", __name__, url_prefix="/sandbox")


def parse_survival_defaults():
    """Parse Survival.lua into a flat dict keyed by 'Group.Key' (or just 'Key' for top-level)."""
    if not os.path.exists(SURVIVAL_LUA_PATH):
        return {}
    with open(SURVIVAL_LUA_PATH) as f:
        content = f.read()
    defaults = {}
    group_stack = []
    for raw in content.split('\n'):
        line = raw.strip()
        if not line or line.startswith('--'):
            continue
        if line.startswith('return {') or line == '{':
            group_stack.append(None)
            continue
        m_group = re.match(r'^([A-Za-z_][\w]*)\s*=\s*\{\s*$', line)
        if m_group:
            group_stack.append(m_group.group(1))
            continue
        if re.match(r'^\}\s*,?\s*$', line):
            if group_stack:
                group_stack.pop()
            continue
        m_kv = re.match(r'^([A-Za-z_][\w]*)\s*=\s*(.+?),?\s*$', line)
        if m_kv:
            key = m_kv.group(1)
            if key == 'VERSION':
                continue
            value_str = m_kv.group(2).rstrip(',').strip()
            value = parse_lua_value(value_str)
            if value is None:
                continue
            named = [g for g in group_stack if g is not None]
            full_key = '.'.join(named + [key]) if named else key
            defaults[full_key] = value
    return defaults


def parse_sandbox_lua():
    """
    Parse SandboxVars.lua. For each setting, capture:
      - group (parent block, or "General")
      - description (from preceding -- comments)
      - min/max/default (extracted from description, with Survival.lua as fallback)
      - current value
    """
    if not os.path.exists(SANDBOX_PATH):
        return None

    survival = parse_survival_defaults()

    with open(SANDBOX_PATH) as f:
        lines = f.readlines()

    groups = {}
    seen_per_group = {}
    group_stack = ["General"]
    pending_comments = []

    for raw in lines:
        line = raw.rstrip("\n").strip()
        if not line:
            pending_comments = []
            continue

        if line.startswith("--"):
            pending_comments.append(line.lstrip("- ").strip())
            continue

        # "GroupName = {"
        m_group = re.match(r'^([A-Za-z_][\w]*)\s*=\s*\{\s*$', line)
        if m_group:
            group_stack.append(m_group.group(1))
            pending_comments = []
            continue

        # "}" or "},"
        if re.match(r'^\}\s*,?\s*$', line):
            if len(group_stack) > 1:
                group_stack.pop()
            pending_comments = []
            continue

        # "Key = value"
        m_kv = re.match(r'^([A-Za-z_][\w]*)\s*=\s*(.+?),?\s*$', line)
        if m_kv:
            key = m_kv.group(1)
            value_str = m_kv.group(2).rstrip(",").strip()

            # Skip wrapper key
            if key in ("SandboxVars",) and group_stack[-1] == "General":
                pending_comments = []
                continue
            if key.upper() == "VERSION":
                pending_comments = []
                continue

            current_group = group_stack[-1]
            if current_group == "SandboxVars":
                current_group = "General"

            # Skip duplicates (same key appears twice in file due to multi-build mod.info)
            group_seen = seen_per_group.setdefault(current_group, set())
            if key in group_seen:
                pending_comments = []
                continue
            group_seen.add(key)

            value = parse_lua_value(value_str)
            if value is None:
                pending_comments = []
                continue

            description = " ".join(pending_comments).strip()
            min_val, max_val, default_val = extract_meta(description)
            clean_desc = clean_description(description)

            if default_val is None:
                survival_key = key if current_group == "General" else f"{current_group}.{key}"
                default_val = survival.get(survival_key)

            groups.setdefault(current_group, []).append({
                "key": key,
                "value": value,
                "description": clean_desc,
                "min": min_val,
                "max": max_val,
                "default": default_val,
                "type": type(value).__name__,
            })
            pending_comments = []
            continue

        pending_comments = []

    return groups


def parse_lua_value(s):
    s = s.strip().rstrip(",").strip()
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return None


def extract_meta(desc):
    min_val = max_val = default_val = None
    m = re.search(r'Minimum\s*=\s*(-?[\d.]+)', desc, re.IGNORECASE)
    if m:
        min_val = float(m.group(1)) if "." in m.group(1) else int(m.group(1))
    m = re.search(r'Maximum\s*=\s*(-?[\d.]+)', desc, re.IGNORECASE)
    if m:
        max_val = float(m.group(1)) if "." in m.group(1) else int(m.group(1))

    # Capture everything after "Default=" up to the first "N = " option entry or end
    dm = re.search(r'Default\s*=\s*(.+?)(?=\s+\d+\s*=\s|\s*(?:Minimum|Maximum)\s*=|$)',
                   desc, re.IGNORECASE)
    if not dm:
        return min_val, max_val, default_val

    default_text = dm.group(1).strip()

    if default_text.lower() == "true":
        return min_val, max_val, True
    if default_text.lower() == "false":
        return min_val, max_val, False

    # Pure number (no trailing label text)
    if re.match(r'^-?[\d]+\.?[\d]*$', default_text):
        try:
            default_val = float(default_text) if "." in default_text else int(default_text)
            return min_val, max_val, default_val
        except ValueError:
            pass

    # Label-based default: find all "N = Label" options in the description,
    # then look up the default label text to get its number.
    # e.g. "Default=Normal ... 4 = Normal" → 4
    options = {}
    for num, label in re.findall(r'(\d+)\s*=\s*(.+?)(?=\s+\d+\s*=\s|$)', desc):
        options[label.strip().lower()] = int(num)

    if default_text.lower() in options:
        default_val = options[default_text.lower()]

    return min_val, max_val, default_val


def clean_description(desc):
    cleaned = re.sub(r'Minimum\s*=\s*-?[\d.]+\s*', '', desc, flags=re.IGNORECASE)
    cleaned = re.sub(r'Maximum\s*=\s*-?[\d.]+\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'Default\s*=\s*(-?[\d.]+|true|false)\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def write_sandbox_updates(updates):
    """updates: { "Zombies": 3, "ZombieLore.Speed": 2, ... }"""
    with open(SANDBOX_PATH) as f:
        content = f.read()

    for full_key, new_value in updates.items():
        if "." in full_key:
            group, key = full_key.split(".", 1)
            content = replace_nested_value(content, group, key, new_value)
        else:
            content = replace_top_value(content, full_key, new_value)

    with open(SANDBOX_PATH, "w") as f:
        f.write(content)


def replace_top_value(content, key, new_value):
    pattern = re.compile(
        rf'(\b{re.escape(key)}\s*=\s*)("[^"]*"|true|false|-?\d+\.?\d*)',
    )
    return pattern.sub(rf'\g<1>{format_lua_value(new_value)}', content, count=1)


def replace_nested_value(content, group, key, new_value):
    group_pattern = re.compile(rf'\b{re.escape(group)}\s*=\s*\{{')
    m = group_pattern.search(content)
    if not m:
        return content

    start = m.end()
    depth = 1
    i = start
    while i < len(content) and depth > 0:
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1

    block = content[start:i]
    new_block = replace_top_value(block, key, new_value)
    return content[:start] + new_block + content[i:]


def format_lua_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return f'"{v}"'
    return str(v)


def _get_survival_vanilla_keys():
    """Return frozenset of top-level key names defined in Survival.lua (canonical vanilla General keys)."""
    return frozenset(k for k in parse_survival_defaults() if '.' not in k)


GROUP_ORDER = [
    "General", "Population", "ZombieLore", "ZombieConfig",
    "Loot", "Survival", "Character", "World", "Map",
    "WaterShut", "ElecShut", "Erosion", "Vehicle", "Farming",
    "Fire", "Sleep", "Food", "Animals", "Plants", "Statistics",
    "Storage", "Weather", "Hordes", "AntiCheat",
]

VANILLA_GROUPS = frozenset(GROUP_ORDER) | {"SandboxVars"}


def _read_installed_mod_ids():
    """Read Mods= from servertest.ini without importing from mods.py (avoids circular import)."""
    try:
        with open(INI_PATH) as f:
            for line in f:
                if line.startswith("Mods="):
                    val = line.strip().split("=", 1)[1]
                    return [m.strip() for m in val.split(";") if m.strip()]
    except FileNotFoundError:
        pass
    return []


def _find_orphan_groups_in(groups_dict, installed_mod_ids):
    """
    Return non-vanilla group names from groups_dict that don't match
    any installed mod ID. When no mods are installed every non-vanilla
    group is an orphan.
    """
    mod_ids_lower = [m.lower() for m in installed_mod_ids]
    orphans = []
    for group_name in groups_dict:
        if group_name in VANILLA_GROUPS:
            continue
        g = group_name.lower()
        if not mod_ids_lower or not any(g == m or g in m or m in g for m in mod_ids_lower):
            orphans.append(group_name)
    return orphans


def find_orphan_sandbox_groups(mod_ids):
    """
    Return non-vanilla sandbox groups whose name exactly matches one of
    mod_ids (case-insensitive, ignoring non-alphanumerics). Used at mod
    removal time, so matching is strict: false negatives leave inert
    leftover settings, but false positives would delete unrelated mods'
    settings. Substring matching is intentionally avoided — short mod IDs
    like "LS" or "BCGTools" would otherwise nuke any group containing
    those letters as a substring.
    """
    if not mod_ids or not os.path.exists(SANDBOX_PATH):
        return []
    groups = parse_sandbox_lua()
    if not groups:
        return []
    norm = lambda s: re.sub(r'[^a-z0-9]', '', s.lower())
    mod_norms = {norm(m) for m in mod_ids if m}
    return [
        group_name for group_name in groups
        if group_name not in VANILLA_GROUPS and norm(group_name) in mod_norms
    ]


def remove_sandbox_groups(group_names):
    """
    Remove named top-level groups from SandboxVars.lua.
    Discards the group block and any comment lines immediately before it.
    Returns the list of group names actually removed.
    """
    if not group_names or not os.path.exists(SANDBOX_PATH):
        return []

    to_remove = {g.lower() for g in group_names}

    with open(SANDBOX_PATH) as f:
        lines = f.readlines()

    removed = []
    new_lines = []
    skip_depth = 0
    pending_comments = []

    for line in lines:
        stripped = line.rstrip("\n").strip()

        if skip_depth > 0:
            for ch in stripped:
                if ch == '{':
                    skip_depth += 1
                elif ch == '}':
                    skip_depth -= 1
                    if skip_depth == 0:
                        break
            continue  # skip every line inside the removed block

        # Detect group opening: "Name = {"
        m = re.match(r'^([A-Za-z_]\w*)\s*=\s*\{', stripped)
        if m and m.group(1).lower() in to_remove:
            group_name = m.group(1)
            pending_comments = []  # discard comments that preceded this block
            skip_depth = 1
            # Account for extra braces on the same line
            rest = stripped[stripped.index('{') + 1:]
            for ch in rest:
                if ch == '{':
                    skip_depth += 1
                elif ch == '}':
                    skip_depth -= 1
            if group_name not in removed:
                removed.append(group_name)
            continue

        if stripped.startswith("--"):
            pending_comments.append(line)
            continue

        new_lines.extend(pending_comments)
        pending_comments = []
        new_lines.append(line)

    new_lines.extend(pending_comments)

    if removed:
        with open(SANDBOX_PATH, "w") as f:
            f.writelines(new_lines)

    return removed


# === Routes ===

@sandbox_bp.route("/")
def index():
    return render_template("sandbox.html")


@sandbox_bp.route("/api/get")
def api_get():
    try:
        groups_dict = parse_sandbox_lua()
        if groups_dict is None:
            return jsonify({
                "success": False,
                "error": "SandboxVars.lua not found. Start the server at least once."
            })

        # Page-load auto-cleanup intentionally removed. The previous
        # implementation used a substring-match heuristic to guess which
        # groups belonged to uninstalled mods, and silently deleted any
        # group that didn't substring-match an installed mod ID. That
        # destroyed legit groups like LSHygiene / LSAmbt (their owning
        # mod ID 'Lifestyle' shares no substring with the group names).
        # Removal-time cleanup in find_orphan_sandbox_groups still runs
        # and uses strict matching, so genuinely orphan groups are still
        # cleaned when the user explicitly removes a mod.

        # Split General: vanilla keys stay; mod flat-keys (prefix_rest) get synthetic tabs
        survival_vanilla_keys = _get_survival_vanilla_keys()
        vanilla_items = []
        synth_by_prefix = {}
        for item in groups_dict.get('General', []):
            key = item['key']
            if '_' not in key or key in survival_vanilla_keys:
                vanilla_items.append(item)
            else:
                prefix = key.split('_')[0]
                synth_by_prefix.setdefault(prefix, []).append(item)
        groups_dict['General'] = vanilla_items

        flat_group_ids = []
        synth_groups = []
        for prefix, items in synth_by_prefix.items():
            synth_id = f'_mod_{prefix}'
            flat_group_ids.append(synth_id)
            synth_groups.append({"id": synth_id, "label": f"{prefix} (mod)", "items": items})

        ordered_groups = []
        seen = set()
        for g in GROUP_ORDER:
            if g in groups_dict and groups_dict[g]:
                ordered_groups.append({
                    "id": g,
                    "label": g,
                    "items": groups_dict[g],
                })
                seen.add(g)
        for g, items in groups_dict.items():
            if g not in seen and items:
                ordered_groups.append({
                    "id": g,
                    "label": g,
                    "items": items,
                })
        ordered_groups.extend(synth_groups)

        return jsonify({"success": True, "groups": ordered_groups, "flatGroups": flat_group_ids})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)})


@sandbox_bp.route("/api/save", methods=["POST"])
def api_save():
    try:
        data = request.json
        updates = data.get("updates", {})

        clean = {}
        for k, v in updates.items():
            if isinstance(v, str):
                if v.lower() == "true":
                    clean[k] = True
                elif v.lower() == "false":
                    clean[k] = False
                else:
                    try:
                        clean[k] = float(v) if "." in v else int(v)
                    except ValueError:
                        clean[k] = v
            else:
                clean[k] = v

        write_sandbox_updates(clean)
        return jsonify({"success": True, "count": len(clean)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)})
