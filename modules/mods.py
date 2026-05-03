"""Mod yönetimi - ekle/kaldır, arama, bağımlılık kontrolü"""
import re
import json
import os
import urllib.request
import urllib.parse
import urllib.error
import concurrent.futures
from collections import deque
from flask import Blueprint, render_template, request, jsonify, g
from config import INI_PATH, WORKSHOP_CONTENT_DIR, ZOMBOID_DIR
from modules.sandbox import find_orphan_sandbox_groups, remove_sandbox_groups

mods_bp = Blueprint("mods", __name__, url_prefix="/mods")

_required_workshop_cache = {}

KNOWN_DEPENDENCY_WORKSHOP_IDS = {
    "TchernoLib": "2986578314",
}

_server_major_cache = None


CATEGORY_TAGS = [
    "Weapons", "Vehicles", "Items", "Clothing/Armor", "Building",
    "Map", "Balance", "Models", "Hardmode", "Misc", "Food", "Animations", "Translation",
]


def _clean_mod_id(value):
    value = str(value or "").strip().lstrip("\\")
    if value.lower().startswith("require="):
        value = value.split("=", 1)[1].strip()
    return value


def _mod_id_aliases(value):
    value = _clean_mod_id(value)
    aliases = {value}
    if "/" in value:
        aliases.add(value.rsplit("/", 1)[1])
    return {alias for alias in aliases if alias}


def _canonical_local_mod_ids(workshop_id):
    """Return exact local mod.info IDs for a downloaded Workshop item."""
    local_by_wid, _ = _read_mod_infos([str(workshop_id)])
    return list(dict.fromkeys(
        info["modId"] for info in local_by_wid.get(str(workshop_id), []) if info.get("modId")
    ))


def _canonicalize_mod_id_for_workshop(mod_id, workshop_id):
    """
    Convert Steam-page namespaced IDs to exact local mod.info IDs when possible.

    Some Workshop pages list values like 1299328280/ToadTraits, while the
    dedicated server expects the local mod.info id value, ToadTraits.
    """
    mod_id = _clean_mod_id(mod_id)
    if not mod_id:
        return mod_id
    local_ids = _canonical_local_mod_ids(workshop_id)
    if mod_id in local_ids:
        return mod_id
    if "/" in mod_id:
        suffix = mod_id.rsplit("/", 1)[1]
        if _server_major_version() < 42 and re.match(r"^\d+/", mod_id):
            return suffix
        if suffix in local_ids:
            return suffix
    return mod_id


def _server_major_version():
    """Best-effort detection of the running/installed PZ major version."""
    global _server_major_cache
    if _server_major_cache is not None:
        return _server_major_cache

    logs_dir = os.path.join(ZOMBOID_DIR, "Logs")
    candidates = []
    try:
        for root, _, files in os.walk(logs_dir):
            for fname in files:
                if fname.endswith("_DebugLog-server.txt"):
                    path = os.path.join(root, fname)
                    candidates.append((os.path.getmtime(path), path))
    except OSError:
        candidates = []

    for _, path in sorted(candidates, reverse=True)[:8]:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read(4096)
        except OSError:
            continue
        match = re.search(r"\bversion=(\d+)\.", text)
        if match:
            _server_major_cache = int(match.group(1))
            return _server_major_cache

    _server_major_cache = 41
    return _server_major_cache


def _mod_info_applies_to_server(root, content_path):
    """Filter version-specific mod.info folders to the server major version."""
    try:
        parts = os.path.relpath(root, content_path).split(os.sep)
    except ValueError:
        return True
    server_major = _server_major_version()
    for part in parts:
        if re.fullmatch(r"\d+(?:\.\d+)?", part):
            return int(part.split(".", 1)[0]) == server_major
    return True


def _mod_id_applies_to_server(mod_id):
    """Build 41 dedicated servers do not accept numeric Workshop-prefixed IDs."""
    if _server_major_version() < 42 and re.match(r"^\d+/", _clean_mod_id(mod_id)):
        return False
    return True


def _canonicalize_mod_ids_for_workshop(mod_ids, workshop_id):
    """Canonicalize and deduplicate Mod IDs for this server/workshop."""
    local_ids = _canonical_local_mod_ids(workshop_id)
    raw_ids = local_ids or mod_ids
    return list(dict.fromkeys(
        mid for mid in (
            _canonicalize_mod_id_for_workshop(raw_id, workshop_id)
            for raw_id in raw_ids
        )
        if mid and _mod_id_applies_to_server(mid)
    ))


def _norm_mod_id(value):
    return re.sub(r"[^a-z0-9]+", "", _clean_mod_id(value).lower())


def _mod_id_similarity(a, b):
    a_norm, b_norm = _norm_mod_id(a), _norm_mod_id(b)
    if not a_norm or not b_norm:
        return 0.0
    if a_norm == b_norm:
        return 1.0
    if a_norm in b_norm or b_norm in a_norm:
        return min(len(a_norm), len(b_norm)) / max(len(a_norm), len(b_norm))

    a_tokens = {t for t in re.split(r"[^a-z0-9]+", _clean_mod_id(a).lower()) if t}
    b_tokens = {t for t in re.split(r"[^a-z0-9]+", _clean_mod_id(b).lower()) if t}
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / len(a_tokens | b_tokens)


def parse_workshop_mod_ids(description):
    """Extract Mod ID values from Steam descriptions, including IDs with spaces."""
    text = html_unescape(description or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(?:p|div|li|h\d)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\[[^\]]+\]", "", text)
    ids = []
    for line in text.splitlines():
        match = re.search(r"\bMod\s*ID\s*[:=]\s*(.+)$", line, re.IGNORECASE)
        if not match:
            continue
        value = match.group(1).strip(" \t`'\"")
        value = re.split(r"\s+(?:Workshop|Map)\s+ID\s*[:=]", value, maxsplit=1, flags=re.IGNORECASE)[0]
        value = value.strip(" \t`'\"")
        if value and value.lower() not in {"none", "n/a"}:
            ids.append(value)
    return list(dict.fromkeys(ids))


def html_unescape(value):
    return (
        str(value or "")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )


def read_ini():
    """servertest.ini'den mod listesini oku"""
    mods, workshops = [], []
    try:
        with open(INI_PATH) as f:
            for line in f:
                if line.startswith("Mods="):
                    val = line.strip().split("=", 1)[1]
                    mods = [m for m in val.split(";") if m.strip()]
                elif line.startswith("WorkshopItems="):
                    val = line.strip().split("=", 1)[1]
                    workshops = [w for w in val.split(";") if w.strip()]
    except FileNotFoundError:
        pass
    return mods, workshops


def write_ini(mods, workshops):
    """servertest.ini'yi güncelle"""
    with open(INI_PATH) as f:
        lines = f.readlines()
    found_mods = found_ws = False
    with open(INI_PATH, "w") as f:
        for line in lines:
            if line.startswith("Mods="):
                f.write("Mods=" + ";".join(mods) + "\n")
                found_mods = True
            elif line.startswith("WorkshopItems="):
                f.write("WorkshopItems=" + ";".join(workshops) + "\n")
                found_ws = True
            else:
                f.write(line)
        if not found_mods:
            f.write("Mods=" + ";".join(mods) + "\n")
        if not found_ws:
            f.write("WorkshopItems=" + ";".join(workshops) + "\n")


def steam_api_get_mod(workshop_id):
    """Steam API'den mod detaylarını çek"""
    try:
        data = urllib.parse.urlencode({
            "itemcount": 1,
            "publishedfileids[0]": workshop_id
        }).encode()
        req = urllib.request.Request(
            "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/",
            data=data, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
        details = result["response"]["publishedfiledetails"][0]
        if details.get("result") != 1:
            return None

        title = details.get("title", workshop_id)
        desc = details.get("description", "")
        preview = details.get("preview_url", "")
        tags = [t.get("tag", "") for t in details.get("tags", [])]
        subscriptions = details.get("subscriptions", 0)

        # Mod ID'leri parse et (tek modda birden fazla olabilir)
        mod_ids = parse_workshop_mod_ids(desc)

        # Workshop ID'leri parse et (bağımlılık olabilir)
        ws_ids_in_desc = re.findall(r'(?:steamcommunity\.com/sharedfiles/filedetails/\?id=|Workshop\s*ID:\s*)(\d+)', desc, re.IGNORECASE)

        return {
            "workshopId": workshop_id,
            "name": title,
            "description": desc[:500],
            "preview": preview,
            "tags": tags,
            "modIds": mod_ids,
            "subscriptions": subscriptions,
            "linkedWorkshopIds": list(set(ws_ids_in_desc)),
        }
    except Exception as e:
        print(f"Steam API hatası: {e}")
        return None


def scrape_required_workshop_ids(workshop_id):
    """
    Scrape the Steam Workshop page to find 'Required Items'.
    The API children field is None for most PZ mods — authors set
    required items via the Steam UI, which only appears in the HTML page.
    Returns list of workshop IDs that are listed as required.
    """
    if workshop_id in _required_workshop_cache:
        return _required_workshop_cache[workshop_id]
    try:
        url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="ignore")

        # Steam renders required items inside id="RequiredItems" div
        container_match = re.search(
            r'<div[^>]*id="RequiredItems"[^>]*>(.*?)</div>\s*</div>',
            html, re.DOTALL
        )
        if not container_match:
            _required_workshop_cache[workshop_id] = []
            return []

        deps = re.findall(
            r'href="https://steamcommunity\.com/workshop/filedetails/\?id=(\d+)"',
            container_match.group(1)
        )
        _required_workshop_cache[workshop_id] = deps
        return deps
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print(f"scrape_required_workshop_ids({workshop_id}) skipped: Steam rate limit (429)")
        else:
            print(f"scrape_required_workshop_ids({workshop_id}) HTTP error: {e}")
        return []
    except Exception as e:
        print(f"scrape_required_workshop_ids({workshop_id}) hatası: {e}")
        return []


def steam_search_workshop(query="", category=None, page=1, sort="trend", build_filter=None):
    """
    Steam Workshop arama - tek sayfa, 20 sonuç
    sort: 'trend' (popüler/trending), 'mostrecent', 'totaluniquesubscribers' (en çok abone),
          'lastupdated', 'subscriptions' (en çok aboneliği bozulmamış)
    build_filter: 'Build 41' veya 'Build 42' (Steam tag filtresi)
    """
    return _fetch_workshop_page(query, category, page, sort, build_filter)


def _fetch_workshop_page(query, category, page, sort="trend", build_filter=None):
    """Tek bir Workshop sayfasını çek ve parse et"""
    try:
        params_list = [
            ("appid", "108600"),
            ("browsesort", sort),
            ("section", "readytouseitems"),
            ("actualsort", sort),
            ("p", str(page)),
            ("numperpage", "20"),
        ]
        if query:
            params_list.append(("searchtext", query))
        if category and category in CATEGORY_TAGS:
            params_list.append(("requiredtags[]", category))
        if build_filter in ("Build 41", "Build 42"):
            params_list.append(("requiredtags[]", build_filter))

        url = "https://steamcommunity.com/workshop/browse/?" + urllib.parse.urlencode(params_list)

        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="ignore")

        items = []
        seen_ids = set()

        # Steam, mod listesinde her item için "data-publishedfileid" attribute'u kullanıyor.
        # Bu attribute SADECE gerçek workshop modlarında var.
        # Featured guide / promoted items gibi şeyler bu attribute'u kullanmıyor.
        #
        # HTML yapısı (Nisan 2026):
        # <div class="workshopItem">
        #   <a href="...?id=ID..." data-appid="108600" data-publishedfileid="ID">
        #     <div class="workshopItemPreviewHolder">
        #       <img class="workshopItemPreviewImage" src="..."/>
        #     ...
        #     <div class="workshopItemTitle ellipsis">Başlık</div>
        #   </a>
        #   <div class="workshopItemAuthorName...">by <a>Yazar</a></div>
        # </div>

        # Her bir gerçek mod entry'sinin başlangıç pozisyonunu bul
        item_starts = list(re.finditer(
            r'<a[^>]*data-publishedfileid="(\d+)"[^>]*>',
            html
        ))

        for i, start_match in enumerate(item_starts):
            ws_id = start_match.group(1)
            if ws_id in seen_ids:
                continue

            # Bu item'ın block'u: bu match'ten sonraki ~3000 char veya bir sonraki item'a kadar
            block_start = start_match.end()
            block_end = item_starts[i + 1].start() if i + 1 < len(item_starts) else min(block_start + 5000, len(html))
            block = html[block_start:block_end]

            # Title
            title_match = re.search(
                r'<div\s+class="workshopItemTitle\s+ellipsis"[^>]*>([^<]+)</div>',
                block
            )
            if not title_match:
                # Bazı modlarda farklı class olabilir
                title_match = re.search(
                    r'<div\s+class="workshopItemTitle[^"]*"[^>]*>([^<]+)</div>',
                    block
                )
            if not title_match:
                continue
            title = title_match.group(1).strip()

            # Preview - workshopItemPreviewImage class'ı (sonunda boşluk olabilir)
            preview = ""
            preview_match = re.search(
                r'<img[^>]*class="workshopItemPreviewImage\s*"[^>]*src="([^"]+)"',
                block
            )
            if not preview_match:
                # Sıralama farklı olabilir: src önce, class sonra
                preview_match = re.search(
                    r'<img[^>]*src="([^"]+)"[^>]*class="workshopItemPreviewImage',
                    block
                )
            if preview_match:
                preview = preview_match.group(1)
                # HTML entity decode (özellikle &amp; → &)
                preview = preview.replace("&amp;", "&").replace("&#x2F;", "/").replace("&#x3A;", ":")
                if "data:" in preview or preview.endswith("blank.gif"):
                    preview = ""

            # Yazar - workshopItemAuthorName div'inde
            author = ""
            author_match = re.search(
                r'<div\s+class="workshopItemAuthorName[^"]*"[^>]*>.*?<a[^>]*>([^<]+)</a>',
                block, re.DOTALL
            )
            if author_match:
                author = author_match.group(1).strip()

            seen_ids.add(ws_id)
            items.append({
                "workshopId": ws_id,
                "name": title,
                "preview": preview,
                "author": author,
            })

        # Toplam sayfa sayısı
        total_pages = 1
        # Pattern: <a class="pagebtn" ...>Next</a> veya pagination link'leri
        # Steam'de "Showing X-Y of Z" şeklinde olabilir
        # Önce "of N" şeklinde toplam sayıyı dene
        of_match = re.search(r'of\s+([\d,]+)\s*</', html)
        if of_match:
            try:
                total_results = int(of_match.group(1).replace(",", ""))
                total_pages = max(1, (total_results + 19) // 20)
            except ValueError:
                pass

        # Alternatif: GoToPage('N') linklerinin maksimumu
        page_links = re.findall(r"GoToPage\(['\"]?(\d+)", html)
        if page_links:
            try:
                page_max = max(int(p) for p in page_links)
                if page_max > total_pages:
                    total_pages = page_max
            except ValueError:
                pass

        # Alternatif 2: "?p=N" linklerinin maksimumu
        p_links = re.findall(r'[?&]p=(\d+)', html)
        if p_links:
            try:
                p_max = max(int(p) for p in p_links)
                if p_max > total_pages:
                    total_pages = p_max
            except ValueError:
                pass

        return {"items": items, "totalPages": total_pages}
    except Exception as e:
        print(f"Workshop search error: {e}")
        import traceback
        traceback.print_exc()
        return {"items": [], "totalPages": 1}


def _read_local_deps(unique_wids):
    """
    Parse mod.info files for each workshop ID.
    Returns (dep_map, mod_id_to_wid) where:
      dep_map[wid]      = list of required mod-IDs declared in mod.info
      mod_id_to_wid[id] = workshop ID that provides that mod ID
    """
    dep_map = {}
    mod_id_to_wid = {}
    for wid in unique_wids:
        content_path = os.path.join(WORKSHOP_CONTENT_DIR, str(wid))
        requires, local_mod_ids = [], []
        if os.path.isdir(content_path):
            for root, _, files in os.walk(content_path):
                if 'mod.info' not in files:
                    continue
                if not _mod_info_applies_to_server(root, content_path):
                    continue
                try:
                    with open(os.path.join(root, 'mod.info')) as f:
                        for line in f:
                            k, sep, v = line.strip().partition('=')
                            if not sep:
                                continue
                            k, v = k.strip().lower(), v.strip()
                            if k == 'id':
                                if _mod_id_applies_to_server(v):
                                    local_mod_ids.append(v)
                            elif k == 'require':
                                # Strip leading backslash (Build 42 namespaced mod ID format)
                                requires += [_clean_mod_id(r) for r in re.split(r'[,;]', v) if r.strip()]
                except Exception:
                    pass
        for mid in local_mod_ids:
            mod_id_to_wid[mid] = wid
            for alias in _mod_id_aliases(mid):
                mod_id_to_wid.setdefault(alias, wid)
        dep_map[wid] = list(dict.fromkeys(requires))  # deduplicate, preserve order
    return dep_map, mod_id_to_wid


def _read_mod_infos(workshop_ids=None):
    """
    Parse local mod.info files.
    Returns:
      by_wid[wid] = [info, ...]
      by_mod_id[mid] = info
    """
    by_wid = {}
    by_mod_id = {}
    roots = []
    if workshop_ids is None:
        try:
            roots = [
                name for name in os.listdir(WORKSHOP_CONTENT_DIR)
                if name.isdigit()
            ]
        except OSError:
            roots = []
    else:
        roots = [str(wid) for wid in workshop_ids]

    for wid in roots:
        content_path = os.path.join(WORKSHOP_CONTENT_DIR, wid)
        infos = []
        if os.path.isdir(content_path):
            for root, _, files in os.walk(content_path):
                if "mod.info" not in files:
                    continue
                if not _mod_info_applies_to_server(root, content_path):
                    continue
                info = {
                    "workshopId": wid,
                    "path": os.path.join(root, "mod.info"),
                    "name": os.path.basename(root),
                    "modId": "",
                    "requires": [],
                }
                try:
                    with open(info["path"], encoding="utf-8", errors="replace") as f:
                        for line in f:
                            k, sep, v = line.strip().partition("=")
                            if not sep:
                                continue
                            k, v = k.strip().lower(), v.strip()
                            if k == "name":
                                info["name"] = v
                            elif k == "id":
                                info["modId"] = _clean_mod_id(v)
                            elif k == "require":
                                info["requires"] = [
                                    _clean_mod_id(r)
                                    for r in re.split(r"[,;]", v)
                                    if r.strip()
                                ]
                except OSError:
                    continue
                if info["modId"] and _mod_id_applies_to_server(info["modId"]):
                    infos.append(info)
                    for alias in _mod_id_aliases(info["modId"]):
                        by_mod_id.setdefault(alias, info)
        by_wid[wid] = infos
    return by_wid, by_mod_id


def analyze_mod_health():
    mods_list, workshops = read_ini()
    active_mods = set()
    for mid in mods_list:
        active_mods.update(_mod_id_aliases(mid))
    unique_wids = list(dict.fromkeys(workshops))
    local_by_wid, _ = _read_mod_infos(unique_wids)
    _, global_by_mod = _read_mod_infos()

    issues = []
    notes = []
    missing_count = 0
    bad_id_count = 0
    missing_file_count = 0

    for mod_id, wid in zip(mods_list, workshops):
        content_path = os.path.join(WORKSHOP_CONTENT_DIR, str(wid))
        local_infos = local_by_wid.get(str(wid), [])
        mod_aliases = _mod_id_aliases(mod_id)
        local_ids = {info["modId"] for info in local_infos}

        if not os.path.isdir(content_path):
            missing_file_count += 1
            issues.append({
                "type": "missing_files",
                "severity": "warn",
                "confidence": "high",
                "modId": mod_id,
                "workshopId": wid,
                "message": f"{mod_id} is enabled, but Workshop {wid} is not downloaded on disk yet.",
                "evidence": f"Missing directory: {content_path}",
            })
            continue

        if local_infos and mod_id not in local_ids and (mod_aliases & local_ids):
            canonical = next(local_id for local_id in sorted(local_ids) if local_id in mod_aliases)
            issues.append({
                "type": "mod_id_alias_not_accepted",
                "severity": "error",
                "confidence": "high",
                "modId": mod_id,
                "workshopId": wid,
                "canonicalModId": canonical,
                "availableModIds": sorted(local_ids),
                "message": f"{mod_id} looks like a Steam-page Mod ID, but this server expects {canonical}.",
                "evidence": "Use the exact id= value from local mod.info, not the Workshop-prefixed form.",
            })
            bad_id_count += 1
            continue

        if local_infos and not (mod_aliases & local_ids):
            candidates = sorted(
                (
                    {
                        "modId": local_id,
                        "score": round(_mod_id_similarity(mod_id, local_id), 2),
                    }
                    for local_id in local_ids
                ),
                key=lambda item: item["score"],
                reverse=True,
            )
            best = candidates[0] if candidates else {"score": 0}
            target = notes if best["score"] >= 0.34 else issues
            issue = {
                "type": "mod_id_not_found",
                "severity": "warn" if target is notes else "error",
                "confidence": "low" if target is notes else "high",
                "modId": mod_id,
                "workshopId": wid,
                "availableModIds": sorted(local_ids),
                "candidates": candidates,
                "message": f"Workshop {wid} is installed, but enabled Mod ID {mod_id} does not match its local mod.info ID.",
                "evidence": "Local mod.info IDs: " + ", ".join(sorted(local_ids)),
            }
            target.append(issue)
            if target is issues:
                bad_id_count += 1
            continue

        info = next((it for it in local_infos if it["modId"] in mod_aliases), None)
        if not info:
            continue

        for req in info["requires"]:
            if req in active_mods:
                continue
            provider = global_by_mod.get(req)
            provider_wid = (
                provider["workshopId"] if provider else KNOWN_DEPENDENCY_WORKSHOP_IDS.get(req)
            )
            if provider_wid and provider_wid in unique_wids:
                notes.append({
                    "type": "dependency_workshop_present",
                    "severity": "info",
                    "confidence": "medium",
                    "modId": mod_id,
                    "workshopId": wid,
                    "dependency": req,
                    "providerWorkshopId": provider_wid,
                    "providerName": provider.get("name") if provider else "",
                    "message": f"{mod_id} declares dependency Mod ID {req}. Its Workshop {provider_wid} is installed, so this is not treated as a missing dependency.",
                    "evidence": f"Dependency Workshop {provider_wid} exists in WorkshopItems.",
                })
                continue
            missing_count += 1
            issues.append({
                "type": "missing_dependency",
                "severity": "error",
                "confidence": "high" if provider_wid else "medium",
                "modId": mod_id,
                "workshopId": wid,
                "dependency": req,
                "providerWorkshopId": provider_wid,
                "providerModId": req,
                "providerName": provider.get("name") if provider else "",
                "message": f"{mod_id} requires Mod ID {req}, but no installed Workshop item provides it.",
                "evidence": f"Found in {info['path']}",
            })

    pair_counts = {}
    for mod_id, wid in zip(mods_list, workshops):
        pair = (mod_id, wid)
        pair_counts[pair] = pair_counts.get(pair, 0) + 1
    duplicate_workshops = [
        {"workshopId": wid, "modId": mod_id, "count": count}
        for (mod_id, wid), count in pair_counts.items()
        if count > 1
    ]

    return {
        "success": True,
        "summary": {
            "enabledMods": len(mods_list),
            "uniqueWorkshops": len(unique_wids),
            "issues": len(issues),
            "notes": len(notes),
            "missingDependencies": missing_count,
            "badModIds": bad_id_count,
            "missingFiles": missing_file_count,
            "duplicateWorkshops": len(duplicate_workshops),
        },
        "issues": issues,
        "notes": notes,
        "duplicateWorkshops": duplicate_workshops,
    }


def repair_mod_id(workshop_id, old_mod_id, new_mod_id):
    """Replace one enabled Mod ID row for a Workshop item."""
    workshop_id = str(workshop_id or "").strip()
    old_mod_id = str(old_mod_id or "").strip()
    new_mod_id = _canonicalize_mod_id_for_workshop(new_mod_id, workshop_id)
    if not workshop_id or not old_mod_id or not new_mod_id:
        return False, "Missing repair data"

    mods, workshops = read_ini()
    changed = False
    new_pairs = []
    seen = set()
    for mod_id, wid in zip(mods, workshops):
        if wid == workshop_id and mod_id == old_mod_id:
            mod_id = new_mod_id
            changed = True
        pair = (mod_id, wid)
        if pair in seen:
            continue
        seen.add(pair)
        new_pairs.append(pair)

    if not changed:
        return False, "Mod ID row not found"

    new_mods = [m for m, _ in new_pairs]
    new_workshops = [w for _, w in new_pairs]
    write_ini(new_mods, new_workshops)
    return True, f"Replaced {old_mod_id} with {new_mod_id}"


def resolve_transitive_dependencies(seed_workshop_id, already_present,
                                    max_depth=4, max_nodes=25):
    """
    BFS through the Steam Workshop 'Required Items' graph starting at
    seed_workshop_id, returning new dependencies in load order
    (parents before children). Each entry:
        {workshopId, name, modIds: [...], depth, via}

    `already_present` is a set/iterable of Workshop IDs already in the INI
    plus the seed itself — used to skip and avoid loops.
    Capped at max_depth (chain length) and max_nodes (total discovered)
    to protect against runaway chains and Steam HTTP 429.
    """
    seen = set(str(w) for w in already_present)
    seen.add(str(seed_workshop_id))
    queue = deque([(str(seed_workshop_id), 0)])
    discovered = []

    while queue and len(discovered) < max_nodes:
        wid, depth = queue.popleft()
        if depth >= max_depth:
            continue
        try:
            child_wids = scrape_required_workshop_ids(wid)
        except Exception:
            continue
        for child_wid in child_wids:
            child_wid = str(child_wid)
            if child_wid in seen:
                continue
            seen.add(child_wid)
            info = steam_api_get_mod(child_wid)
            mod_ids = (
                _canonicalize_mod_ids_for_workshop(info["modIds"], child_wid)
                if info else []
            )
            discovered.append({
                "workshopId": child_wid,
                "name": info["name"] if info else child_wid,
                "modIds": mod_ids,
                "depth": depth + 1,
                "via": wid,
                "error": None if info else "Steam API failed",
            })
            if len(discovered) >= max_nodes:
                break
            queue.append((child_wid, depth + 1))

    return discovered


def _topo_sort(wids, dep_map):
    """
    Kahn's algorithm. dep_map[wid] = [wids that wid depends on (must load first)].
    Returns sorted list. Cycles are resolved by appending remaining nodes in original order.
    """
    ids = list(dict.fromkeys(wids))
    id_set = set(ids)
    in_degree = {w: 0 for w in ids}
    adj = {w: [] for w in ids}
    for wid in ids:
        for dep in dep_map.get(wid, []):
            if dep not in id_set or dep == wid:
                continue
            adj[dep].append(wid)
            in_degree[wid] += 1
    queue = deque(w for w in ids if in_degree[w] == 0)
    result = []
    while queue:
        node = queue.popleft()
        result.append(node)
        for nxt in adj[node]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)
    result_set = set(result)
    result += [w for w in ids if w not in result_set]
    return result


def _topo_sort_pairs(pairs, pair_dep_map):
    """
    Stable topological sort for individual (modId, workshopId) rows.
    pair_dep_map[index] = indexes that must come before index.
    """
    ids = list(range(len(pairs)))
    in_degree = {i: 0 for i in ids}
    adj = {i: [] for i in ids}
    for idx in ids:
        for dep_idx in pair_dep_map.get(idx, []):
            if dep_idx == idx or dep_idx not in in_degree:
                continue
            adj[dep_idx].append(idx)
            in_degree[idx] += 1

    queue = deque(i for i in ids if in_degree[i] == 0)
    result = []
    while queue:
        node = queue.popleft()
        result.append(node)
        for nxt in adj[node]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)

    result_set = set(result)
    result += [i for i in ids if i not in result_set]
    return [pairs[i] for i in result]


# === Routes ===

@mods_bp.route("/")
def index():
    return render_template("mods.html")


@mods_bp.route("/api/list")
def api_list():
    mods, workshops = read_ini()
    counts = {}
    for wid in workshops:
        counts[wid] = counts.get(wid, 0) + 1
    items = []
    for i, wid in enumerate(workshops):
        items.append({
            "workshopId": wid,
            "modId": mods[i] if i < len(mods) else "?",
            "groupSize": counts.get(wid, 1),
        })
    return jsonify({"mods": items})


@mods_bp.route("/api/add", methods=["POST"])
def api_add():
    data = request.json
    wid = str(data.get("workshopId", "")).strip()
    custom_mod_id = str(data.get("modId") or "").strip()  # Opsiyonel manuel mod ID

    if not wid.isdigit():
        return jsonify({"success": False, "error": "Geçersiz Workshop ID"})

    info = steam_api_get_mod(wid)
    if not info:
        return jsonify({"success": False, "error": "Mod bulunamadı (Steam API)"})

    # Mod ID belirleme. Prefer local mod.info IDs because Steam descriptions
    # sometimes list Workshop-prefixed IDs that PZ dedicated servers reject.
    mod_id = _canonicalize_mod_id_for_workshop(custom_mod_id, wid) if custom_mod_id else ""
    all_mod_ids = _canonicalize_mod_ids_for_workshop(info["modIds"], wid)
    if not mod_id and all_mod_ids:
        mod_id = all_mod_ids[0]

    if not mod_id:
        return jsonify({
            "success": False,
            "error": "Mod ID bulunamadı",
            "needManualModId": True,
            "name": info["name"],
        })

    mods, workshops = read_ini()
    if wid in workshops:
        if custom_mod_id and not any(m == mod_id and w == wid for m, w in zip(mods, workshops)):
            mods.append(mod_id)
            workshops.append(wid)
            write_ini(mods, workshops)
            return jsonify({
                "success": True,
                "name": info["name"],
                "modId": mod_id,
                "allModIds": [mod_id],
                "autoDeps": [],
                "skippedDeps": [],
            })
        return jsonify({"success": False, "error": "Bu mod zaten ekli"})

    # Ana modu ekle (tüm mod ID'leri)
    if custom_mod_id:
        all_mod_ids = [mod_id] + [m for m in all_mod_ids if m != mod_id]
    for mid in all_mod_ids:
        mods.append(mid)
        workshops.append(wid)

    # BFS through Steam Workshop required-items graph (transitive deps).
    discovered = resolve_transitive_dependencies(wid, set(workshops) | {wid})
    auto_added = []
    skipped_deps = []
    for dep in discovered:
        dep_wid = dep["workshopId"]
        if dep["modIds"]:
            for mid in dep["modIds"]:
                mods.append(mid)
                workshops.append(dep_wid)
            auto_added.append({
                "workshopId": dep_wid,
                "name": dep["name"],
                "modIds": dep["modIds"],
                "depth": dep["depth"],
                "via": dep["via"],
            })
        elif dep.get("error"):
            skipped_deps.append(f"Workshop {dep_wid}")
        else:
            skipped_deps.append(f"{dep['name']} (mod ID bulunamadı)")

    write_ini(mods, workshops)

    return jsonify({
        "success": True,
        "name": info["name"],
        "modId": mod_id,
        "allModIds": all_mod_ids,
        "autoDeps": auto_added,
        "skippedDeps": skipped_deps,
    })


@mods_bp.route("/api/remove", methods=["POST"])
def api_remove():
    data = request.json
    wid = str(data.get("workshopId", "")).strip()
    mod_id = str(data.get("modId") or "").strip()
    if not wid:
        return jsonify({"success": False, "error": "workshopId gerekli"})

    mods, workshops = read_ini()
    before_count = sum(1 for w in workshops if w == wid)

    if mod_id:
        remove_indexes = [
            i for i, (m, w) in enumerate(zip(mods, workshops))
            if w == wid and m == mod_id
        ]
    else:
        remove_indexes = [
            i for i, w in enumerate(workshops)
            if w == wid and i < len(mods)
        ]

    # Collect mod IDs being removed (for sandbox cleanup)
    mod_ids_removed = [mods[i] for i in remove_indexes if i < len(mods)]
    if not mod_ids_removed:
        return jsonify({"success": False, "error": "Mod bulunamadı"})

    remove_set = set(remove_indexes)
    pairs = [(m, w) for i, (m, w) in enumerate(zip(mods, workshops)) if i not in remove_set]
    new_mods, new_workshops = (list(zip(*pairs)) if pairs else ([], []))
    write_ini(list(new_mods), list(new_workshops))
    remaining_count = sum(1 for w in new_workshops if w == wid)

    # Clean orphan sandbox groups. This is best-effort; mod removal should not
    # report failure after servertest.ini was already updated.
    cleaned = []
    cleanup_error = None
    try:
        orphans = find_orphan_sandbox_groups(mod_ids_removed)
        cleaned = remove_sandbox_groups(orphans)
    except OSError as e:
        cleanup_error = str(e)

    return jsonify({
        "success": True,
        "cleanedGroups": cleaned,
        "cleanupError": cleanup_error,
        "removedModIds": mod_ids_removed,
        "removedWorkshopItem": remaining_count == 0,
        "remainingModIdsForWorkshop": remaining_count,
        "wasMultiIdWorkshop": before_count > len(remove_indexes),
    })


@mods_bp.route("/api/info/<workshop_id>")
def api_info(workshop_id):
    info = steam_api_get_mod(workshop_id)
    if info:
        return jsonify({"success": True, "info": info})
    return jsonify({"success": False, "error": "Bulunamadı"})


@mods_bp.route("/api/reorder", methods=["POST"])
def api_reorder():
    data = request.json
    order = data.get("order", [])  # [{"workshopId": "...", "modId": "..."}, ...]
    new_mods = [item["modId"] for item in order]
    new_workshops = [item["workshopId"] for item in order]
    write_ini(new_mods, new_workshops)
    return jsonify({"success": True})


@mods_bp.route("/api/health")
def api_health():
    return jsonify(analyze_mod_health())


@mods_bp.route("/api/repair-id", methods=["POST"])
def api_repair_id():
    data = request.json or {}
    ok, message = repair_mod_id(
        data.get("workshopId"),
        data.get("modId"),
        data.get("canonicalModId"),
    )
    return jsonify({"success": ok, "message": message})


@mods_bp.route("/api/auto-order")
def api_auto_order():
    """
    Returns the suggested load order for installed mods based on dependency data.
    Sources: local mod.info 'require=' fields by default.
    Steam Workshop 'Required Items' scraping is optional via ?steam=1 because
    large mod lists can trigger Steam HTTP 429 rate limits.
    Does NOT write to INI — the caller decides whether to save.
    """
    mods_list, workshops = read_ini()
    if not workshops:
        return jsonify({"success": True, "order": [], "noChange": True, "depsFound": 0})

    unique_wids = list(dict.fromkeys(workshops))

    # Phase 1: local mod.info (instant)
    local_dep_map, mod_id_to_wid = _read_local_deps(unique_wids)

    # Phase 2: optional Workshop page scraping. Keep this conservative: autosort
    # can be run often, and Steam rate-limits bursts from large mod lists.
    scraped = {}
    used_steam = request.args.get("steam") == "1"

    if used_steam:
        def _scrape(wid):
            return wid, scrape_required_workshop_ids(wid)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            futs = {pool.submit(_scrape, wid): wid for wid in unique_wids}
            try:
                for fut in concurrent.futures.as_completed(futs, timeout=60):
                    try:
                        wid, dep_wids = fut.result(timeout=1)
                        scraped[wid] = dep_wids
                    except Exception:
                        scraped[futs[fut]] = []
            except concurrent.futures.TimeoutError:
                pass

    for wid in unique_wids:
        scraped.setdefault(wid, [])

    # Phase 3: merge into a single dep_map (wid -> installed wids it depends on)
    installed = set(unique_wids)
    dep_map = {}
    for wid in unique_wids:
        deps = set()
        for dep_wid in scraped.get(wid, []):
            if dep_wid in installed and dep_wid != wid:
                deps.add(dep_wid)
        for req_mid in local_dep_map.get(wid, []):
            dep_wid = mod_id_to_wid.get(req_mid)
            if dep_wid and dep_wid in installed and dep_wid != wid:
                deps.add(dep_wid)
        dep_map[wid] = list(deps)

    # Phase 4: topological sort at the individual Mod ID row level. This
    # handles both cross-workshop dependencies and multi-ID workshop items
    # whose sub-mods depend on another sub-mod from the same Workshop item.
    original = [{"workshopId": w, "modId": m} for m, w in zip(mods_list, workshops)]
    _, local_by_mod = _read_mod_infos(unique_wids)
    wid_to_indexes = {}
    alias_to_index = {}
    for i, row in enumerate(original):
        wid_to_indexes.setdefault(row["workshopId"], []).append(i)
        for alias in _mod_id_aliases(row["modId"]):
            alias_to_index.setdefault(alias, i)

    pair_dep_map = {}
    deps_found = 0
    for i, row in enumerate(original):
        deps = set()
        for dep_wid in dep_map.get(row["workshopId"], []):
            deps.update(wid_to_indexes.get(dep_wid, []))

        info = None
        for alias in _mod_id_aliases(row["modId"]):
            candidate = local_by_mod.get(alias)
            if candidate and candidate["workshopId"] == row["workshopId"]:
                info = candidate
                break
        if info:
            for req_mid in info["requires"]:
                dep_idx = alias_to_index.get(req_mid)
                if dep_idx is not None:
                    deps.add(dep_idx)

        deps.discard(i)
        if deps:
            deps_found += 1
        pair_dep_map[i] = list(deps)

    result = _topo_sort_pairs(original, pair_dep_map)

    return jsonify({
        "success": True,
        "order": result,
        "noChange": result == original,
        "depsFound": deps_found,
        "usedSteam": used_steam,
    })


@mods_bp.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    page = int(request.args.get("page", "1"))
    sort = request.args.get("sort", "trend")
    build_filter = request.args.get("build", "").strip() or None
    # Geçerli sort değerleri
    if sort not in ("trend", "totaluniquesubscribers", "mostrecent", "lastupdated"):
        sort = "trend"
    result = steam_search_workshop(query=query, category=category or None, page=page, sort=sort, build_filter=build_filter)
    return jsonify({
        "items": result["items"],
        "totalPages": result["totalPages"],
        "currentPage": page,
    })


@mods_bp.route("/api/categories")
def api_categories():
    cats = {k: g.t.get("cat_" + k.replace("/", "_"), k) for k in CATEGORY_TAGS}
    return jsonify({"categories": cats})
