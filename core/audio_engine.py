"""
AudioEngine — low-latency audio for BlastPlayer.

Architecture
------------
* ffmpeg decodes the audio track(s) once into a float32 PCM numpy array.
* A single sounddevice OutputStream runs permanently; the audio callback
  switches between three modes without any process-spawn latency:

    STOPPED   – silence
    PLAYING   – advances _play_pos each block (continuous playback)
    SCRUBBING – plays a short window then returns to STOPPED

Multi-clip
----------
All clips are decoded in the background and concatenated into one array.
A sample-offset table maps global scrubber position → sample index so
seeking across clip boundaries is instant.

Thread safety
-------------
The OutputStream callback runs on a PortAudio thread.  Only simple
reads/writes to primitive Python attributes occur there.  CPython's GIL
makes these safe; we never allocate memory inside the callback.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
from PyQt5.QtCore import QObject, pyqtSignal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ffmpeg_exe() -> str:
    try:
        from core import constants
        exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
        candidate = Path(constants.FFMPEG_PATH).parent / exe
        return str(candidate) if candidate.exists() else exe
    except Exception:
        return "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"


# ---------------------------------------------------------------------------
# AudioEngine
# ---------------------------------------------------------------------------

class AudioEngine(QObject):
    """
    Signals
    -------
    load_finished(ok: bool)
        Emitted on the Qt main thread after background PCM decode completes.
    """

    load_finished = pyqtSignal(bool)

    # Stream configuration — fixed for the lifetime of the object.
    SAMPLE_RATE  = 48_000
    CHANNELS     = 2
    BLOCK_SIZE   = 512      # ~10.7 ms/block at 48 kHz

    # How many samples constitute one scrub snippet (~100 ms).
    SCRUB_SAMPLES = int(SAMPLE_RATE * 0.10)

    # Minimum gap between successive scrub snippets (seconds).
    # Prevents the callback from being hammered while the user drags.
    SCRUB_MIN_GAP = 0.06    # 60 ms

    # Modes used inside the audio callback
    _STOPPED          = 0
    _PLAYING          = 1
    _SCRUBBING        = 2
    _PLAYING_REVERSE  = 3   # reads PCM buffer backwards — classic "tape rewind" sound

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

        # PCM data — written once (background), read many times (callback + main)
        self._pcm: np.ndarray | None = None   # shape (N_samples, CHANNELS), float32
        self._sample_offsets: list[int] = []  # per-clip start sample for mc mode

        # Volume scalar applied in callback (0.0 – 1.0)
        self._volume: float = 1.0

        # Playback speed multiplier — changes pitch along with tempo (simple resampling)
        self._speed: float = 1.0

        # Frames-per-second of the currently loaded content
        self._fps: float = 24.0

        # Callback state — read by audio thread, written by main thread
        self._mode:            int = self._STOPPED
        self._play_pos:        int = 0   # next sample to output in PLAYING mode
        self._play_start_pos:  int = 0   # sample at which current play() started
        self._scrub_pos:       int = 0   # current read pointer in SCRUBBING mode
        self._scrub_end:       int = 0   # exclusive end sample for current snippet

        # Rate-limit scrub snippet restarts
        self._last_scrub_t: float = 0.0
        # User-facing toggle (mirrors the "Audio Scrubbing" menu action)
        self._scrub_enabled: bool = True

        # Background decode bookkeeping
        self._loading:        bool = False
        self._decode_thread:  threading.Thread | None = None
        self._cancel_decode:  threading.Event = threading.Event()

        # Open the PortAudio stream once; it stays open until close()
        self._stream: sd.OutputStream | None = None
        self._open_stream()

    # ------------------------------------------------------------------
    # Stream lifecycle
    # ------------------------------------------------------------------

    def _open_stream(self) -> None:
        try:
            self._stream = sd.OutputStream(
                samplerate=self.SAMPLE_RATE,
                channels=self.CHANNELS,
                dtype="float32",
                blocksize=self.BLOCK_SIZE,
                callback=self._audio_callback,
            )
            self._stream.start()
        except Exception as exc:
            print(f"[AudioEngine] Could not open audio stream: {exc}")
            self._stream = None

    def close(self) -> None:
        """Call when the player is shutting down."""
        self._mode = self._STOPPED
        self._cancel_decode.set()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    # ------------------------------------------------------------------
    # Load API  (call from main thread)
    # ------------------------------------------------------------------

    def load(self, path: str, fps: float,
             mc_clips: list[dict] | None = None) -> None:
        """
        Start background PCM decode for *path*.

        Parameters
        ----------
        path      : video file path (used as the primary audio source)
        fps       : frames-per-second of the content
        mc_clips  : if not None, decode *all* clips and concatenate them;
                    *path* / *fps* are taken from mc_clips[0] for consistency
        """
        self.stop()
        self._pcm = None
        self._sample_offsets = []
        self._fps = fps
        self._loading = True

        self._cancel_decode.clear()
        if self._decode_thread and self._decode_thread.is_alive():
            self._cancel_decode.set()
            self._decode_thread.join(timeout=0.5)
            self._cancel_decode.clear()

        paths = ([c["path"] for c in mc_clips] if mc_clips else [path])
        t = threading.Thread(
            target=self._decode_worker,
            args=(paths,),
            daemon=True,
        )
        self._decode_thread = t
        t.start()

    def _decode_worker(self, paths: list[str]) -> None:
        ffmpeg = _ffmpeg_exe()
        chunks: list[np.ndarray] = []
        offsets: list[int] = []
        cursor = 0

        for p in paths:
            if self._cancel_decode.is_set():
                break
            chunk = self._decode_file(ffmpeg, p)
            if chunk is not None and len(chunk):
                offsets.append(cursor)
                cursor += len(chunk)
                chunks.append(chunk)
            else:
                offsets.append(cursor)

        ok = bool(chunks)
        if ok and not self._cancel_decode.is_set():
            self._pcm = np.concatenate(chunks, axis=0)
            self._sample_offsets = offsets

        self._loading = False
        # Emit signal on the Qt main thread via a queued meta-call
        self.load_finished.emit(ok)

    def _decode_file(self, ffmpeg: str, path: str) -> np.ndarray | None:
        """Run ffmpeg to decode one file's audio track to float32 PCM."""
        cmd = [
            ffmpeg, "-y", "-i", path,
            "-vn",
            "-ac", str(self.CHANNELS),
            "-ar", str(self.SAMPLE_RATE),
            "-f",  "f32le",
            "pipe:1",
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            raw = proc.stdout.read()
            proc.wait()
        except Exception as exc:
            print(f"[AudioEngine] decode error for {path}: {exc}")
            return None

        if not raw:
            return None

        arr = np.frombuffer(raw, dtype=np.float32)
        # Trim to a multiple of CHANNELS
        remainder = len(arr) % self.CHANNELS
        if remainder:
            arr = arr[:-remainder]
        return arr.reshape(-1, self.CHANNELS)

    # ------------------------------------------------------------------
    # Playback control  (call from main thread)
    # ------------------------------------------------------------------

    def play(self, frame: int) -> None:
        """Start continuous playback from *frame*."""
        if self._pcm is None:
            return
        self._play_pos = self._frame_to_sample(frame)
        self._play_start_pos = self._play_pos
        self._mode = self._PLAYING

    def stop(self) -> None:
        """Halt all audio output."""
        self._mode = self._STOPPED

    def play_reverse(self, frame: int) -> None:
        """
        Start continuous reverse playback from *frame* going backwards.

        The PCM buffer is read right-to-left each callback block, producing
        the characteristic reversed-tape sound animators use to check timing.
        """
        if self._pcm is None:
            return
        # _play_pos is the exclusive upper bound of the next block to read.
        self._play_pos = min(self._frame_to_sample(frame) + 1, len(self._pcm))
        self._mode = self._PLAYING_REVERSE

    def seek_to_clip(self, clip_idx: int) -> None:
        """
        In multi-clip mode, hard-seek audio to the start of *clip_idx* and
        resume PLAYING.  Call this every time the mc timeline advances to a
        new clip so the audio clock stays locked to the video clock.
        """
        if self._pcm is None:
            return
        if 0 <= clip_idx < len(self._sample_offsets):
            self._play_pos = self._sample_offsets[clip_idx]
        else:
            self._play_pos = 0
        self._mode = self._PLAYING

    def scrub(self, frame: int, mc_clips: list | None = None) -> None:
        """
        Play a short audio snippet at *frame* for scrub drag feedback.

        Pass *mc_clips* when scrubbing a multi-clip timeline so the global
        frame is mapped to the correct sample via the clip offset table.

        Rate-limited: successive calls closer than SCRUB_MIN_GAP apart
        are ignored so the callback is never starved.
        """
        if self._pcm is None or not self._scrub_enabled:
            return
        now = time.monotonic()
        if now - self._last_scrub_t < self.SCRUB_MIN_GAP:
            return
        self._last_scrub_t = now

        pos = (self.global_frame_to_sample(frame, mc_clips)
               if mc_clips else self._frame_to_sample(frame))
        self._scrub_pos = pos
        self._scrub_end = min(pos + self.SCRUB_SAMPLES, len(self._pcm))
        self._mode = self._SCRUBBING

    def set_volume(self, vol_0_100: int) -> None:
        """Set volume (0–100).  Takes effect on the next callback block."""
        self._volume = max(0.0, min(1.0, vol_0_100 / 100.0))

    def set_speed(self, speed: float) -> None:
        """Set playback speed multiplier.  Pitch shifts along with tempo."""
        self._speed = max(0.05, speed)

    # ------------------------------------------------------------------
    # Status queries  (main thread)
    # ------------------------------------------------------------------

    def is_loaded(self) -> bool:
        return self._pcm is not None

    def is_loading(self) -> bool:
        return self._loading

    def duration_samples(self) -> int:
        return len(self._pcm) if self._pcm is not None else 0

    def audio_clock_elapsed(self) -> float:
        """
        Content-seconds elapsed since play() was last called.

        Returns -1.0 when audio is not actively playing (not loaded, stopped,
        scrubbing, or reverse-playing) so callers can fall back to wall-clock.

        The value is derived from _play_pos which the PortAudio callback
        advances at speed × SAMPLE_RATE samples per wall-second, so speed is
        already factored in — callers must NOT multiply by self._speed again.
        """
        if self._mode != self._PLAYING or self._pcm is None:
            return -1.0
        elapsed_samples = self._play_pos - self._play_start_pos
        if elapsed_samples < 0:
            return -1.0
        return elapsed_samples / self.SAMPLE_RATE

    # ------------------------------------------------------------------
    # Multi-clip helpers
    # ------------------------------------------------------------------

    def global_frame_to_sample(self, global_frame: int,
                                mc_clips: list[dict]) -> int:
        """
        Convert a global scrubber frame (across the full mc timeline)
        to an absolute sample index in the concatenated PCM array.
        """
        if self._pcm is None or not self._sample_offsets:
            return 0
        offset_frames = 0
        for i, clip in enumerate(mc_clips):
            nf = clip["total_frames"]
            if global_frame < offset_frames + nf or i == len(mc_clips) - 1:
                local_frame = global_frame - offset_frames
                clip_fps    = clip.get("fps", self._fps)
                sample_off  = self._sample_offsets[i] if i < len(self._sample_offsets) else 0
                s = sample_off + int(local_frame / clip_fps * self.SAMPLE_RATE)
                return max(0, min(s, len(self._pcm) - 1))
            offset_frames += nf
        return 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _frame_to_sample(self, frame: int) -> int:
        if self._pcm is None:
            return 0
        s = int(frame / self._fps * self.SAMPLE_RATE)
        return max(0, min(s, len(self._pcm) - 1))

    # ------------------------------------------------------------------
    # Audio callback  (called by PortAudio thread — must not block or alloc)
    # ------------------------------------------------------------------

    def _audio_callback(
        self,
        outdata:   np.ndarray,
        frames:    int,
        time_info: object,
        status:    sd.CallbackFlags,
    ) -> None:
        pcm = self._pcm
        if pcm is None:
            outdata[:] = 0
            return

        vol  = self._volume
        mode = self._mode

        if mode == self._PLAYING:
            pos     = self._play_pos
            spd     = self._speed
            consume = max(1, round(frames * spd))
            src_end = min(pos + consume, len(pcm))
            n_src   = src_end - pos
            if n_src <= 0:
                outdata[:] = 0
                self._mode = self._STOPPED
                return
            src = pcm[pos:src_end]
            if n_src == frames:
                # Speed is effectively 1.0 — no resampling needed
                np.multiply(src, vol, out=outdata)
            else:
                # Linear resample: stretch/compress n_src PCM samples → frames output
                t  = np.linspace(0.0, n_src - 1.0, frames, dtype=np.float32)
                i0 = t.astype(np.intp)
                i1 = np.minimum(i0 + 1, n_src - 1)
                frac = (t - i0)[:, None].astype(np.float32)
                np.multiply(src[i0] + frac * (src[i1] - src[i0]), vol, out=outdata)
            self._play_pos = src_end
            if src_end >= len(pcm):
                self._mode = self._STOPPED

        elif mode == self._PLAYING_REVERSE:
            pos     = self._play_pos   # exclusive upper bound
            spd     = self._speed
            consume = max(1, round(frames * spd))
            src_start = max(0, pos - consume)
            n_src     = pos - src_start
            if n_src <= 0:
                outdata[:] = 0
                self._mode = self._STOPPED
                return
            # Read the slice in reverse order
            src = pcm[src_start:pos][::-1]
            if n_src == frames:
                np.multiply(src, vol, out=outdata)
            else:
                t    = np.linspace(0.0, n_src - 1.0, frames, dtype=np.float32)
                i0   = t.astype(np.intp)
                i1   = np.minimum(i0 + 1, n_src - 1)
                frac = (t - i0)[:, None].astype(np.float32)
                np.multiply(src[i0] + frac * (src[i1] - src[i0]), vol, out=outdata)
            self._play_pos = src_start
            if src_start <= 0:
                self._mode = self._STOPPED

        elif mode == self._SCRUBBING:
            pos = self._scrub_pos
            end = self._scrub_end
            n   = min(frames, end - pos)
            if n > 0:
                np.multiply(pcm[pos:pos + n], vol, out=outdata[:n])
                self._scrub_pos = pos + n
            else:
                n = 0
            if n < frames:
                outdata[n:] = 0
            if self._scrub_pos >= end:
                self._mode = self._STOPPED

        else:  # STOPPED
            outdata[:] = 0
