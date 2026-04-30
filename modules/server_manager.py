"""Sunucu process manager - PTY üzerinden başlat, log buffer, komut gönder"""
import os
import pty
import select
import threading
import time
import signal
import collections
import fcntl
import termios
import struct
from config import SERVER_DIR, SERVER_SCRIPT, is_server_running


class ServerManager:
    def __init__(self):
        self.pid = None
        self.master_fd = None
        self.log_buffer = collections.deque(maxlen=2000)  # son 2000 satır
        self.subscribers = []  # (queue,) tuples for SSE
        self.subscribers_lock = threading.Lock()
        self.reader_thread = None
        self.running = False

    def start(self):
        """Sunucuyu PTY üzerinden başlat"""
        if self.is_alive() or is_server_running():
            return False, "Sunucu zaten çalışıyor"

        try:
            pid, master_fd = pty.fork()
            if pid == 0:
                # Child process
                os.chdir(SERVER_DIR)
                os.execvp("bash", ["bash", SERVER_SCRIPT])
                # buraya gelmez
            else:
                # Parent
                self.pid = pid
                self.master_fd = master_fd
                self.running = True
                self.log_buffer.clear()
                self._broadcast_clear()

                # Set non-blocking
                flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
                fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

                # Terminal boyutu ayarla (önemli, bazı uygulamalar için)
                try:
                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                                struct.pack("HHHH", 40, 120, 0, 0))
                except Exception:
                    pass

                # Reader thread başlat
                self.reader_thread = threading.Thread(target=self._read_loop, daemon=True)
                self.reader_thread.start()

                return True, "Sunucu başlatılıyor..."
        except Exception as e:
            self.pid = None
            self.master_fd = None
            self.running = False
            return False, f"Hata: {e}"

    def _read_loop(self):
        """Sürekli PTY'den oku ve buffer'a + subscriber'lara yaz"""
        partial = ""
        while self.running and self.master_fd is not None:
            try:
                ready, _, _ = select.select([self.master_fd], [], [], 0.5)
                if not ready:
                    # Process hâlâ yaşıyor mu kontrol et
                    if not self._check_alive():
                        break
                    continue
                try:
                    data = os.read(self.master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                # ANSI escape sekanslarını temizle (basit)
                text = self._strip_ansi(text)

                # Satırlara böl
                full = partial + text
                lines = full.split("\n")
                partial = lines[-1]
                for ln in lines[:-1]:
                    line = ln.rstrip("\r")
                    self.log_buffer.append(line)
                    self._broadcast(line)
            except Exception as e:
                self.log_buffer.append(f"[reader hatası: {e}]")
                break

        self.running = False
        if partial:
            self.log_buffer.append(partial)
            self._broadcast(partial)
        self._broadcast("[--- sunucu kapandı ---]")

    @staticmethod
    def _strip_ansi(s):
        import re
        # CSI sequences
        s = re.sub(r'\x1B\[[0-?]*[ -/]*[@-~]', '', s)
        # Other escape sequences
        s = re.sub(r'\x1B[@-Z\\-_]', '', s)
        return s

    def _check_alive(self):
        """Process hâlâ yaşıyor mu?"""
        if self.pid is None:
            return False
        try:
            wpid, status = os.waitpid(self.pid, os.WNOHANG)
            if wpid != 0:
                # Process bitti
                self.pid = None
                return False
            return True
        except ChildProcessError:
            self.pid = None
            return False
        except Exception:
            return True

    def is_alive(self):
        if not self.running or self.pid is None:
            return False
        return self._check_alive()

    def send_command(self, cmd):
        """Sunucuya komut gönder"""
        if not self.is_alive() or self.master_fd is None:
            return False, "Sunucu çalışmıyor"
        try:
            data = (cmd + "\n").encode("utf-8")
            os.write(self.master_fd, data)
            return True, "Komut gönderildi"
        except Exception as e:
            return False, f"Hata: {e}"

    def stop(self):
        """Sunucuyu temiz şekilde kapat ('quit' komutu gönder)"""
        if self.is_alive():
            self.send_command("quit")
            # Birkaç saniye bekle
            for _ in range(15):
                time.sleep(1)
                if not self.is_alive():
                    break
            # Hâlâ yaşıyorsa kill
            if self.is_alive() and self.pid:
                try:
                    os.kill(self.pid, signal.SIGTERM)
                except Exception:
                    pass
                time.sleep(2)
                if self.is_alive() and self.pid:
                    try:
                        os.kill(self.pid, signal.SIGKILL)
                    except Exception:
                        pass

        # Eski yöntemle de pkill (panel dışında başlatılmış olabilir)
        import subprocess
        subprocess.run(["pkill", "-f", "ProjectZomboid"])

        self._cleanup()
        return True, "Sunucu durduruldu"

    def _cleanup(self):
        self.running = False
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except Exception:
                pass
            self.master_fd = None
        self.pid = None

    def get_buffer(self, last_n=None):
        """Buffer'daki logları al"""
        if last_n:
            return list(self.log_buffer)[-last_n:]
        return list(self.log_buffer)

    def subscribe(self):
        """SSE için yeni subscriber ekle"""
        import queue
        q = queue.Queue(maxsize=500)
        with self.subscribers_lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.subscribers_lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def _broadcast(self, line):
        with self.subscribers_lock:
            for q in self.subscribers[:]:
                try:
                    q.put_nowait(("line", line))
                except Exception:
                    pass

    def _broadcast_clear(self):
        with self.subscribers_lock:
            for q in self.subscribers[:]:
                try:
                    q.put_nowait(("clear", ""))
                except Exception:
                    pass


# Singleton
manager = ServerManager()
