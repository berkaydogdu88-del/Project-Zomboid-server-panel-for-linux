#!/usr/bin/env python3
"""PZ Panel - Project Zomboid Sunucu Yönetim Paneli"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, render_template, redirect, request, g

from modules.mods import mods_bp
from modules.sandbox import sandbox_bp
from modules.world import world_bp
from modules.server import server_bp
from modules.cheats import cheats_bp
from modules.maps import maps_bp

app = Flask(__name__)

# Load translations at startup
_translations = {}
_trans_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "translations")
for _lang in ("en", "tr"):
    with open(os.path.join(_trans_dir, f"{_lang}.json"), encoding="utf-8") as _f:
        _translations[_lang] = json.load(_f)

app.register_blueprint(mods_bp)
app.register_blueprint(sandbox_bp)
app.register_blueprint(world_bp)
app.register_blueprint(server_bp)
app.register_blueprint(cheats_bp)
app.register_blueprint(maps_bp)


@app.before_request
def set_language():
    lang = request.cookies.get("lang", "tr")
    if lang not in _translations:
        lang = "tr"
    g.lang = lang
    g.t = _translations[lang]


@app.context_processor
def inject_i18n():
    return {"t": g.t, "lang": g.lang}


@app.route("/set-language/<lang>")
def set_language_route(lang):
    if lang not in _translations:
        lang = "tr"
    resp = redirect(request.referrer or "/")
    resp.set_cookie("lang", lang, max_age=365 * 24 * 3600)
    return resp


@app.route("/")
def index():
    return redirect("/mods/")


if __name__ == "__main__":
    print("\n" + "=" * 50)
    print("  🎮 PZ Panel başlatıldı")
    print("=" * 50)
    print("  👉 Tarayıcıdan aç: http://localhost:8621")
    print("=" * 50 + "\n")
    app.run(host="0.0.0.0", port=8621, debug=False, threaded=True)
