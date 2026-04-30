"""Ortak yardımcı fonksiyonlar ve konfigürasyon"""
import os
import subprocess
from pathlib import Path

HOME = os.path.expanduser("~")
ZOMBOID_DIR = os.path.join(HOME, "Zomboid")
SERVER_DIR = os.path.join(HOME, ".local/share/Steam/steamapps/common/Project Zomboid Dedicated Server")
SERVER_SCRIPT = os.path.join(SERVER_DIR, "start-server.sh")
INI_PATH = os.path.join(ZOMBOID_DIR, "Server/servertest.ini")
SANDBOX_PATH = os.path.join(ZOMBOID_DIR, "Server/servertest_SandboxVars.lua")
SURVIVAL_LUA_PATH = os.path.join(SERVER_DIR, "media/lua/shared/Sandbox/Survival.lua")
WORKSHOP_CONTENT_DIR = os.path.join(SERVER_DIR, "steamapps/workshop/content/108600")
SAVES_DIR = os.path.join(ZOMBOID_DIR, "Saves")
BACKUP_DIR = os.path.join(ZOMBOID_DIR, "Backups")

# Sunucu işlem ismi
SERVER_PROCESS_NAME = "ProjectZomboid"


def is_server_running():
    """Sunucu çalışıyor mu kontrol et"""
    try:
        result = subprocess.run(["pgrep", "-f", SERVER_PROCESS_NAME], capture_output=True)
        return result.returncode == 0
    except Exception:
        return False


def start_server():
    """Sunucuyu başlat"""
    if is_server_running():
        return False, "Sunucu zaten çalışıyor"
    try:
        subprocess.Popen(
            ["bash", SERVER_SCRIPT],
            cwd=SERVER_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True, "Sunucu başlatılıyor..."
    except Exception as e:
        return False, f"Hata: {e}"


def stop_server():
    """Sunucuyu durdur"""
    try:
        subprocess.run(["pkill", "-f", SERVER_PROCESS_NAME])
        return True, "Sunucu durduruldu"
    except Exception as e:
        return False, f"Hata: {e}"


def restart_server():
    """Sunucuyu yeniden başlat"""
    import time
    stop_server()
    time.sleep(2)
    return start_server()


def ensure_dirs():
    """Gerekli klasörleri oluştur"""
    Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
