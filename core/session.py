"""
BlastPlayer collaborative review session.

One machine hosts (SessionServer); others join (SessionClient).
Messages are newline-delimited JSON over a plain TCP socket.

Message types
-------------
seek        {"type":"seek",  "frame":<int>}
play        {"type":"play",  "frame":<int>}
pause       {"type":"pause", "frame":<int>}
stroke      {"type":"stroke","frame":<int>,"stroke":<dict>}
clear_frame {"type":"clear_frame","frame":<int>}
clear_all   {"type":"clear_all"}
load        {"type":"load","path":<str>,"fps":<float>,"total_frames":<int>}
frame       {"type":"frame","data":<base64_jpeg>}
"""

from __future__ import annotations

import base64
import json
import socket
import threading

from PyQt5.QtCore import QObject, pyqtSignal


# -- Server --------------------------------------------------------------------

class SessionServer(QObject):
    """Runs on the host machine.  Broadcasts state to all connected clients."""

    client_joined = pyqtSignal(str)       # "<ip>:<port>"
    client_left   = pyqtSignal(str)
    server_error  = pyqtSignal(str)

    DEFAULT_PORT = 7734

    def __init__(self, parent=None):
        super().__init__(parent)
        self._clients: list[socket.socket] = []
        self._lock    = threading.Lock()
        self._sock    = None
        self._running = False
        # Snapshots replayed to late joiners — 'load' and 'position' stored separately
        self._last_states: dict[str, dict] = {}

    # -- Lifecycle ------------------------------------------------------- #

    def start(self, port: int = DEFAULT_PORT) -> bool:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(('', port))
            self._sock.listen(16)
            self._running = True
            threading.Thread(target=self._accept_loop, daemon=True).start()
            return True
        except Exception as exc:
            self.server_error.emit(str(exc))
            return False

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try: self._sock.close()
            except Exception: pass
            self._sock = None
        with self._lock:
            for c in self._clients:
                try: c.close()
                except Exception: pass
            self._clients.clear()
        self._last_states.clear()

    # -- Broadcast ------------------------------------------------------- #

    def broadcast(self, msg: dict) -> None:
        """Send *msg* to every connected follower."""
        t = msg.get('type', '')
        if t == 'load':
            self._last_states['load'] = msg
        elif t in ('seek', 'pause', 'play'):
            self._last_states['position'] = msg   # only the latest position matters
        # 'frame' is not stored — it's large and transient

        data = (json.dumps(msg, separators=(',', ':')) + '\n').encode()
        dead: list[socket.socket] = []
        with self._lock:
            for c in self._clients:
                try:
                    c.sendall(data)
                except Exception:
                    dead.append(c)
            for c in dead:
                self._clients.remove(c)

    # -- Properties ------------------------------------------------------ #

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    @staticmethod
    def local_ip() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return '127.0.0.1'

    # -- Internal -------------------------------------------------------- #

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except Exception:
                break
            addr_str = f"{addr[0]}:{addr[1]}"
            with self._lock:
                self._clients.append(conn)
            self._sync_new_client(conn)
            self.client_joined.emit(addr_str)
            threading.Thread(
                target=self._watch_client, args=(conn, addr_str), daemon=True
            ).start()

    def _sync_new_client(self, conn: socket.socket) -> None:
        """Replay load + position snapshots so a new joiner is immediately in sync."""
        for key in ('load', 'position'):
            msg = self._last_states.get(key)
            if msg:
                try:
                    conn.sendall(
                        (json.dumps(msg, separators=(',', ':')) + '\n').encode()
                    )
                except Exception:
                    pass

    def _watch_client(self, conn: socket.socket, addr_str: str) -> None:
        """Detect when a client disconnects (we don't expect data from them)."""
        try:
            while self._running:
                data = conn.recv(64)
                if not data:
                    break
        except Exception:
            pass
        with self._lock:
            if conn in self._clients:
                self._clients.remove(conn)
        self.client_left.emit(addr_str)
        try: conn.close()
        except Exception: pass


# -- Client --------------------------------------------------------------------

class SessionClient(QObject):
    """Runs on follower machines.  Receives state from the host."""

    seek_received      = pyqtSignal(int)
    play_received      = pyqtSignal(int)
    pause_received     = pyqtSignal(int)
    stroke_received    = pyqtSignal(int, object)   # (frame, stroke_dict)
    clear_received     = pyqtSignal(int)
    clear_all_received = pyqtSignal()
    load_received      = pyqtSignal(str, float, int)   # (path, fps, total_frames)
    frame_received     = pyqtSignal(object)            # bytes: JPEG-compressed frame
    connected          = pyqtSignal()
    disconnected       = pyqtSignal()
    connect_error      = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sock    = None
        self._running = False

    # -- Lifecycle ------------------------------------------------------- #

    def connect_to(self, host: str,
                   port: int = SessionServer.DEFAULT_PORT) -> bool:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(5.0)
            self._sock.connect((host, port))
            self._sock.settimeout(None)
            self._running = True
            threading.Thread(target=self._recv_loop, daemon=True).start()
            self.connected.emit()
            return True
        except Exception as exc:
            self.connect_error.emit(str(exc))
            return False

    def disconnect(self) -> None:
        self._running = False
        if self._sock:
            try: self._sock.close()
            except Exception: pass
            self._sock = None

    # -- Internal -------------------------------------------------------- #

    def _recv_loop(self) -> None:
        buf = ''
        try:
            while self._running:
                chunk = self._sock.recv(65536)
                if not chunk:
                    break
                buf += chunk.decode(errors='replace')
                while '\n' in buf:
                    line, buf = buf.split('\n', 1)
                    line = line.strip()
                    if line:
                        try:
                            self._dispatch(json.loads(line))
                        except Exception:
                            pass
        except Exception:
            pass
        self.disconnected.emit()

    def _dispatch(self, msg: dict) -> None:
        t = msg.get('type')
        if t == 'seek':
            self.seek_received.emit(int(msg['frame']))
        elif t == 'play':
            self.play_received.emit(int(msg['frame']))
        elif t == 'pause':
            self.pause_received.emit(int(msg['frame']))
        elif t == 'stroke':
            self.stroke_received.emit(int(msg['frame']), msg['stroke'])
        elif t == 'clear_frame':
            self.clear_received.emit(int(msg['frame']))
        elif t == 'clear_all':
            self.clear_all_received.emit()
        elif t == 'load':
            self.load_received.emit(
                str(msg.get('path', '')),
                float(msg.get('fps', 24.0)),
                int(msg.get('total_frames', 0)),
            )
        elif t == 'frame':
            try:
                self.frame_received.emit(base64.b64decode(msg['data']))
            except Exception:
                pass
