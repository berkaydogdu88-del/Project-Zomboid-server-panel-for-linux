"""Sunucu kontrol modülü + canlı terminal"""
import json
import time
import queue as queue_mod
from flask import Blueprint, jsonify, request, render_template, Response, stream_with_context
from config import is_server_running
from modules.server_manager import manager

server_bp = Blueprint("server", __name__)


@server_bp.route("/api/server/status")
def status():
    # is_server_running de bak (panel başlatmasa bile manuel başlatılmış olabilir)
    alive = manager.is_alive() or is_server_running()
    return jsonify({
        "running": alive,
        "managed": manager.is_alive(),  # panel tarafından mı başlatıldı
    })


@server_bp.route("/api/server/start", methods=["POST"])
def start():
    success, msg = manager.start()
    return jsonify({"success": success, "message": msg})


@server_bp.route("/api/server/stop", methods=["POST"])
def stop():
    success, msg = manager.stop()
    return jsonify({"success": success, "message": msg})


@server_bp.route("/api/server/restart", methods=["POST"])
def restart():
    manager.stop()
    time.sleep(2)
    success, msg = manager.start()
    return jsonify({"success": success, "message": msg})


@server_bp.route("/api/server/command", methods=["POST"])
def command():
    data = request.json or {}
    cmd = data.get("cmd", "").strip()
    if not cmd:
        return jsonify({"success": False, "message": "Boş komut"})
    success, msg = manager.send_command(cmd)
    return jsonify({"success": success, "message": msg})


@server_bp.route("/api/server/logs")
def get_logs():
    """Mevcut buffer'ı dön"""
    return jsonify({"logs": manager.get_buffer()})


@server_bp.route("/api/server/stream")
def stream():
    """SSE - canlı log akışı"""
    def event_stream():
        # İlk olarak mevcut buffer'ı yolla
        for line in manager.get_buffer():
            yield f"data: {json.dumps({'type': 'line', 'text': line})}\n\n"

        # Yeni satırlar için subscribe ol
        q = manager.subscribe()
        try:
            while True:
                try:
                    evt_type, payload = q.get(timeout=15)
                    yield f"data: {json.dumps({'type': evt_type, 'text': payload})}\n\n"
                except queue_mod.Empty:
                    # Heartbeat
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            manager.unsubscribe(q)

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


@server_bp.route("/terminal")
def terminal_page():
    return render_template("terminal.html")
