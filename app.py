#!/usr/bin/env python3
"""PZ Panel - Project Zomboid Sunucu Yönetim Paneli"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, render_template, redirect

from modules.mods import mods_bp
from modules.sandbox import sandbox_bp
from modules.world import world_bp
from modules.server import server_bp

app = Flask(__name__)
app.register_blueprint(mods_bp)
app.register_blueprint(sandbox_bp)
app.register_blueprint(world_bp)
app.register_blueprint(server_bp)


@app.route("/")
def index():
    return redirect("/mods/")


if __name__ == "__main__":
    print("\n" + "="*50)
    print("  🎮 PZ Panel başlatıldı")
    print("="*50)
    print("  👉 Tarayıcıdan aç: http://localhost:8621")
    print("="*50 + "\n")
    app.run(host="0.0.0.0", port=8621, debug=False, threaded=True)
