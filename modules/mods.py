"""Mod yönetimi - ekle/kaldır, arama, bağımlılık kontrolü"""
import re
import json
import os
import urllib.request
import urllib.parse
import concurrent.futures
from collections import deque
from flask import Blueprint, render_template, request, jsonify, g
from config import INI_PATH, WORKSHOP_CONTENT_DIR
from modules.sandbox import find_orphan_sandbox_groups, remove_sandbox_groups

mods_bp = Blueprint("mods", __name__, url_prefix="/mods")


CATEGORY_TAGS = [
    "Weapons", "Vehicles", "Items", "Clothing/Armor", "Building",
    "Map", "Balance", "Models", "Hardmode", "Misc", "Food", "Animations", "Translation",
]


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
        # [^...[] - köşeli parantezde durak, label kısmı (ör. "[MAIN MOD]") hariç tutulur
        mod_ids = re.findall(r'Mod\s*ID:\s*([^\s\n\r\[]+)', desc, re.IGNORECASE)
        mod_ids = list(dict.fromkeys(m.strip() for m in mod_ids if m.strip()))

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
            return []

        return re.findall(
            r'href="https://steamcommunity\.com/workshop/filedetails/\?id=(\d+)"',
            container_match.group(1)
        )
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
                try:
                    with open(os.path.join(root, 'mod.info')) as f:
                        for line in f:
                            k, sep, v = line.strip().partition('=')
                            if not sep:
                                continue
                            k, v = k.strip().lower(), v.strip()
                            if k == 'id':
                                local_mod_ids.append(v)
                            elif k == 'require':
                                # Strip leading backslash (Build 42 namespaced mod ID format)
                                requires += [r.strip().lstrip('\\') for r in re.split(r'[,;]', v) if r.strip()]
                except Exception:
                    pass
        for mid in local_mod_ids:
            mod_id_to_wid[mid] = wid
        dep_map[wid] = list(dict.fromkeys(requires))  # deduplicate, preserve order
    return dep_map, mod_id_to_wid


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


# === Routes ===

@mods_bp.route("/")
def index():
    return render_template("mods.html")


@mods_bp.route("/api/list")
def api_list():
    mods, workshops = read_ini()
    items = []
    for i, wid in enumerate(workshops):
        items.append({
            "workshopId": wid,
            "modId": mods[i] if i < len(mods) else "?",
        })
    return jsonify({"mods": items})


@mods_bp.route("/api/add", methods=["POST"])
def api_add():
    data = request.json
    wid = str(data.get("workshopId", "")).strip()
    custom_mod_id = data.get("modId", "").strip()  # Opsiyonel manuel mod ID

    if not wid.isdigit():
        return jsonify({"success": False, "error": "Geçersiz Workshop ID"})

    info = steam_api_get_mod(wid)
    if not info:
        return jsonify({"success": False, "error": "Mod bulunamadı (Steam API)"})

    # Mod ID belirleme
    mod_id = custom_mod_id
    if not mod_id and info["modIds"]:
        mod_id = info["modIds"][0]

    if not mod_id:
        return jsonify({
            "success": False,
            "error": "Mod ID bulunamadı",
            "needManualModId": True,
            "name": info["name"],
        })

    mods, workshops = read_ini()
    if wid in workshops:
        return jsonify({"success": False, "error": "Bu mod zaten ekli"})

    # Ana modu ekle (tüm mod ID'leri)
    all_mod_ids = info["modIds"] if info["modIds"] else [mod_id]
    if custom_mod_id:
        all_mod_ids = [custom_mod_id] + [m for m in info["modIds"] if m != custom_mod_id]
    for mid in all_mod_ids:
        mods.append(mid)
        workshops.append(wid)

    # Workshop sayfasını scrape ederek gerçek bağımlılıkları bul
    dep_workshop_ids = scrape_required_workshop_ids(wid)
    auto_added = []
    skipped_deps = []
    for dep_wid in dep_workshop_ids:
        if dep_wid in workshops or dep_wid == wid:
            continue
        dep_info = steam_api_get_mod(dep_wid)
        if dep_info and dep_info["modIds"]:
            # Bağımlılığın tüm mod ID'lerini ekle
            for mid in dep_info["modIds"]:
                mods.append(mid)
                workshops.append(dep_wid)
            auto_added.append({"workshopId": dep_wid, "name": dep_info["name"],
                                "modIds": dep_info["modIds"]})
        elif dep_info:
            skipped_deps.append(f"{dep_info['name']} (mod ID bulunamadı)")
        else:
            skipped_deps.append(f"Workshop {dep_wid}")

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
    if not wid:
        return jsonify({"success": False, "error": "workshopId gerekli"})

    mods, workshops = read_ini()

    # Collect all mod IDs belonging to this workshop item (for sandbox cleanup)
    mod_ids_removed = [mods[i] for i, w in enumerate(workshops) if w == wid and i < len(mods)]
    if not mod_ids_removed:
        return jsonify({"success": False, "error": "Mod bulunamadı"})

    # Remove every entry that belongs to this workshop ID
    pairs = [(m, w) for m, w in zip(mods, workshops) if w != wid]
    new_mods, new_workshops = (list(zip(*pairs)) if pairs else ([], []))
    write_ini(list(new_mods), list(new_workshops))

    # Clean orphan sandbox groups
    orphans = find_orphan_sandbox_groups(mod_ids_removed)
    cleaned = remove_sandbox_groups(orphans)

    return jsonify({"success": True, "cleanedGroups": cleaned})


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


@mods_bp.route("/api/auto-order")
def api_auto_order():
    """
    Returns the suggested load order for installed mods based on dependency data.
    Sources: local mod.info 'require=' fields + Steam Workshop 'Required Items' (parallel scrape).
    Does NOT write to INI — the caller decides whether to save.
    """
    mods_list, workshops = read_ini()
    if not workshops:
        return jsonify({"success": True, "order": [], "noChange": True, "depsFound": 0})

    unique_wids = list(dict.fromkeys(workshops))

    # Phase 1: local mod.info (instant)
    local_dep_map, mod_id_to_wid = _read_local_deps(unique_wids)

    # Phase 2: parallel Workshop page scraping
    scraped = {}

    def _scrape(wid):
        return wid, scrape_required_workshop_ids(wid)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futs = {pool.submit(_scrape, wid): wid for wid in unique_wids}
        try:
            for fut in concurrent.futures.as_completed(futs, timeout=25):
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

    # Phase 4: topological sort
    sorted_wids = _topo_sort(unique_wids, dep_map)

    # Expand back to (modId, workshopId) pairs
    wid_to_mods = {}
    for mod_id, wid in zip(mods_list, workshops):
        wid_to_mods.setdefault(wid, []).append(mod_id)

    result = [{"workshopId": wid, "modId": mid}
              for wid in sorted_wids
              for mid in wid_to_mods.get(wid, [])]

    original = [{"workshopId": w, "modId": m} for m, w in zip(mods_list, workshops)]
    deps_found = sum(1 for deps in dep_map.values() if deps)

    return jsonify({
        "success": True,
        "order": result,
        "noChange": result == original,
        "depsFound": deps_found,
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


