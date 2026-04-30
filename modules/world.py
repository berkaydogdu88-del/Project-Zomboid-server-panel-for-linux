"""Dünya yönetimi - yedekle, sil, geri yükle"""
import os
import shutil
import datetime
import tarfile
from flask import Blueprint, render_template, request, jsonify
from config import SAVES_DIR, BACKUP_DIR, ensure_dirs, is_server_running

world_bp = Blueprint("world", __name__, url_prefix="/world")


def list_worlds():
    """Tüm dünyaları listele"""
    worlds = []
    if not os.path.exists(SAVES_DIR):
        return worlds
    # Saves/Multiplayer/, Saves/Sandbox/, Saves/Apocalypse/ vs.
    for mode_dir in sorted(os.listdir(SAVES_DIR)):
        mode_path = os.path.join(SAVES_DIR, mode_dir)
        if not os.path.isdir(mode_path):
            continue
        for world_name in sorted(os.listdir(mode_path)):
            world_path = os.path.join(mode_path, world_name)
            if not os.path.isdir(world_path):
                continue
            try:
                stat = os.stat(world_path)
                size = get_dir_size(world_path)
                worlds.append({
                    "mode": mode_dir,
                    "name": world_name,
                    "path": world_path,
                    "size": size,
                    "sizeHuman": human_size(size),
                    "modified": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
            except OSError:
                pass
    return worlds


def list_backups():
    """Tüm yedekleri listele"""
    ensure_dirs()
    backups = []
    if not os.path.exists(BACKUP_DIR):
        return backups
    for fn in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if fn.endswith(".tar.gz"):
            fp = os.path.join(BACKUP_DIR, fn)
            try:
                stat = os.stat(fp)
                backups.append({
                    "name": fn,
                    "size": stat.st_size,
                    "sizeHuman": human_size(stat.st_size),
                    "modified": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
            except OSError:
                pass
    return backups


def get_dir_size(path):
    total = 0
    try:
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    except OSError:
        pass
    return total


def human_size(b):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


# === Routes ===

@world_bp.route("/")
def index():
    return render_template("world.html")


@world_bp.route("/api/worlds")
def api_worlds():
    return jsonify({"worlds": list_worlds()})


@world_bp.route("/api/backups")
def api_backups():
    return jsonify({"backups": list_backups()})


@world_bp.route("/api/backup", methods=["POST"])
def api_backup():
    """Bir dünyayı yedekle"""
    data = request.json
    mode = data.get("mode")
    name = data.get("name")
    if not mode or not name:
        return jsonify({"success": False, "error": "mode/name eksik"})

    world_path = os.path.join(SAVES_DIR, mode, name)
    if not os.path.isdir(world_path):
        return jsonify({"success": False, "error": "Dünya bulunamadı"})

    ensure_dirs()
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = name.replace(" ", "_").replace("/", "_")
    backup_name = f"{mode}_{safe_name}_{timestamp}.tar.gz"
    backup_path = os.path.join(BACKUP_DIR, backup_name)

    try:
        with tarfile.open(backup_path, "w:gz") as tar:
            tar.add(world_path, arcname=os.path.join(mode, name))
        return jsonify({"success": True, "backup": backup_name})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@world_bp.route("/api/delete", methods=["POST"])
def api_delete():
    """Bir dünyayı sil (sunucu kapalıyken)"""
    if is_server_running():
        return jsonify({"success": False, "error": "Önce sunucuyu durdur!"})

    data = request.json
    mode = data.get("mode")
    name = data.get("name")
    create_backup = data.get("createBackup", True)

    if not mode or not name:
        return jsonify({"success": False, "error": "mode/name eksik"})

    world_path = os.path.join(SAVES_DIR, mode, name)
    if not os.path.isdir(world_path):
        return jsonify({"success": False, "error": "Dünya bulunamadı"})

    # Önce yedekle (istenirse)
    backup_name = None
    if create_backup:
        ensure_dirs()
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = name.replace(" ", "_").replace("/", "_")
        backup_name = f"deleted_{mode}_{safe_name}_{timestamp}.tar.gz"
        backup_path = os.path.join(BACKUP_DIR, backup_name)
        try:
            with tarfile.open(backup_path, "w:gz") as tar:
                tar.add(world_path, arcname=os.path.join(mode, name))
        except Exception as e:
            return jsonify({"success": False, "error": f"Yedekleme başarısız: {e}"})

    # Sil
    try:
        shutil.rmtree(world_path)
        return jsonify({"success": True, "backup": backup_name})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


def _valid_backup_name(name):
    """Reject names that contain path separators or don't look like our .tar.gz files."""
    return bool(name) and os.path.basename(name) == name and name.endswith(".tar.gz")


@world_bp.route("/api/restore", methods=["POST"])
def api_restore():
    """Yedeği geri yükle"""
    if is_server_running():
        return jsonify({"success": False, "error": "Önce sunucuyu durdur!"})

    data = request.json
    backup_name = data.get("backup")
    if not _valid_backup_name(backup_name):
        return jsonify({"success": False, "error": "Geçersiz yedek adı"})

    backup_path = os.path.join(BACKUP_DIR, backup_name)
    if not os.path.isfile(backup_path):
        return jsonify({"success": False, "error": "Yedek bulunamadı"})

    try:
        with tarfile.open(backup_path, "r:gz") as tar:
            tar.extractall(SAVES_DIR, filter="data")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@world_bp.route("/api/delete-backup", methods=["POST"])
def api_delete_backup():
    """Yedek dosyasını sil"""
    data = request.json
    backup_name = data.get("backup")
    if not _valid_backup_name(backup_name):
        return jsonify({"success": False, "error": "Geçersiz yedek adı"})

    backup_path = os.path.join(BACKUP_DIR, backup_name)
    if not os.path.isfile(backup_path):
        return jsonify({"success": False, "error": "Yedek bulunamadı"})

    try:
        os.remove(backup_path)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
