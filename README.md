# Project Zomboid Server Panel for Linux

A web-based control panel for managing a Project Zomboid Dedicated Server on Linux. Manage mods, sandbox settings, game worlds, and the server process — all from your browser.

---

## Features

- **Mod Management** — Search Steam Workshop, auto-resolve dependencies, drag-and-drop load order
- **Sandbox Configuration** — Edit `SandboxVars.lua` settings through a clean UI
- **World Management** — Backup, restore, and delete save files
- **Real-time Terminal** — Start/stop/restart the server, stream live logs, send server commands

## Requirements

- Linux
- Python 3
- Flask (`pip install flask`)
- Project Zomboid Dedicated Server installed via Steam

## Installation

```bash
git clone https://github.com/berkaydogdu88-del/Project-Zomboid-server-panel-for-linux.git
cd Project-Zomboid-server-panel-for-linux
pip install flask
python3 app.py
```

Then open your browser at: **http://localhost:8621**

## Configuration

Edit `config.py` to set the correct paths for your system:

| Variable | Description |
|---|---|
| `SERVER_DIR` | Path to the PZ Dedicated Server folder |
| `INI_PATH` | Path to your `servertest.ini` |
| `SANDBOX_PATH` | Path to `servertest_SandboxVars.lua` |
| `SAVES_DIR` | Path to `~/.Zomboid/Saves/` |
| `BACKUP_DIR` | Where world backups are stored |

Default paths assume a standard Steam installation. Adjust if yours differs.

## Project Structure

```
pz_panel/
├── app.py               # Entry point
├── config.py            # Path configuration
├── modules/
│   ├── server.py        # Server control routes & SSE streaming
│   ├── server_manager.py# PTY process manager, log buffer
│   ├── mods.py          # Mod management & Steam Workshop integration
│   ├── sandbox.py       # Lua config parser & sandbox settings
│   └── world.py         # Save/backup management
├── templates/           # Jinja2 HTML templates
└── static/              # CSS
```

---

# Türkçe

Project Zomboid Dedicated Server'ınızı tarayıcıdan yönetmenizi sağlayan bir web paneli.

## Özellikler

- **Mod Yönetimi** — Steam Workshop'ta arama, bağımlılıkları otomatik çözme, yükleme sırası düzenleme
- **Sandbox Ayarları** — `SandboxVars.lua` dosyasını arayüz üzerinden düzenleme
- **Dünya Yönetimi** — Kayıtları yedekleme, geri yükleme ve silme
- **Gerçek Zamanlı Terminal** — Sunucuyu başlatma/durdurma/yeniden başlatma, canlı log akışı, komut gönderme

## Gereksinimler

- Linux
- Python 3
- Flask (`pip install flask`)
- Steam üzerinden kurulu Project Zomboid Dedicated Server

## Kurulum

```bash
git clone https://github.com/berkaydogdu88-del/Project-Zomboid-server-panel-for-linux.git
cd Project-Zomboid-server-panel-for-linux
pip install flask
python3 app.py
```

Tarayıcıdan açın: **http://localhost:8621**

## Yapılandırma

`config.py` dosyasındaki yolları kendi sisteminize göre düzenleyin:

| Değişken | Açıklama |
|---|---|
| `SERVER_DIR` | PZ Dedicated Server klasörünün yolu |
| `INI_PATH` | `servertest.ini` dosyasının yolu |
| `SANDBOX_PATH` | `servertest_SandboxVars.lua` dosyasının yolu |
| `SAVES_DIR` | `~/.Zomboid/Saves/` klasörünün yolu |
| `BACKUP_DIR` | Dünya yedeklerinin kaydedileceği konum |

Varsayılan yollar standart bir Steam kurulumunu varsayar.
