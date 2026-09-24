#!/usr/bin/env python3
"""STAR coagulator client (websocket).

Protocol derived from the samtupy/star source (github.com/samtupy/star):
  - Client sends {"user": <revision>} on connect.
  - Server pushes {"voices": [...]} (list of voice name dicts).
  - Client sends {"user": <rev>, "request": "<voice><r=.. p=..>: text", "id": "<id>"}.
    IMPORTANT: the coagulator REWRITES the id to "<clientid>_<id>_<seq>" before
    handing the request to the voice provider, and the provider echoes that
    rewritten id back in the binary audio frame. So we cannot correlate the
    response by our original id -- we treat the next binary frame we receive
    after sending a request as that request's audio.
  - Server replies with binary frames:
        len(2 bytes, little) + json-meta + audio-bytes
    where audio-bytes is WAV/PCM.
  - If a voice needs extra params, the server sends a JSON {"status": "400 ...", "abort": true}
    instead of audio, and no binary frame arrives.

Speech requests are serialized (one at a time) so a simple FIFO queue of
received audio frames is sufficient for correlation.

Auto-reconnect: when an established connection drops unexpectedly (network
blip, server restart), a supervisor thread redials the same URI with
exponential backoff (1s doubling to a 60s cap, indefinitely) until it gets
through. A manual disconnect() never auto-reconnects, and a manual connect()
replaces the reconnect target. synthesize() waits briefly for an in-progress
reconnect, so /speak recovers transparently after a blip.
"""
import os
import re
import json
import time
import logging
import threading
import queue
import websockets.sync.client

log = logging.getLogger(__name__)

STAR_USER_REVISION = 4  # matches STAR.py USER_REVISION

RECONNECT_BASE_DELAY = 1.0    # first retry after 1s, doubling each failure...
RECONNECT_MAX_DELAY = 60.0    # ...capped at 60s between attempts
VOICES_WAIT = 5.0             # connect(): how long to wait for the voice list
RECONNECT_WAIT = 10.0         # synthesize(): how long to wait out a reconnect


def _redact_uri(uri):
    """Hide any user:pass@ credentials before logging a URI."""
    return re.sub(r"(//[^/@:]+:)[^@]*@", r"\1***@", uri)


class StarCoagulator:
    def __init__(self):
        self.ws = None
        self.uri = None
        self.voices = []
        self._lock = threading.Lock()
        self._audio_q = queue.Queue()           # ("audio", bytes) items
        self._thread = None
        self._abort = threading.Event()
        self._synth_lock = threading.Lock()     # one synthesis at a time
        self._voices_evt = threading.Event()    # set when the server's voice list arrives
        # auto-reconnect machinery
        self._era = 0                           # bumped on every manual connect/disconnect
        self._drop_evt = threading.Event()      # pump -> supervisor: connection lost
        self._reconnecting = threading.Event()  # set while the supervisor is dialing
        self._supervisor = None

    @property
    def connected(self):
        return self.ws is not None

    @property
    def reconnecting(self):
        """True while a dropped connection is being re-established."""
        return self._reconnecting.is_set()

    # ---- connection lifecycle --------------------------------------------

    def connect(self, uri):
        """Manually connect to `uri`. Raises on failure.

        Cancels any in-flight auto-reconnect; the new URI becomes the
        reconnect target for the rest of the session.
        """
        with self._lock:
            self._era += 1
        self._drop_evt.clear()
        self._reconnecting.clear()
        self._dial(uri, wait_voices=True)
        self._ensure_supervisor()
        return True

    def disconnect(self):
        """Manual stop. Never auto-reconnects."""
        with self._lock:
            self._era += 1
            self._abort.set()
            ws = self.ws
            self.ws = None
        self._drop_evt.clear()
        self._reconnecting.clear()
        if ws:
            try:
                ws.close()
            except Exception:
                pass

    def _dial(self, uri, wait_voices=True):
        """Open the socket, say hello, start the receive pump.

        Replaces any existing socket. Raises on failure (nothing is left
        connected). Callers must not hold self._lock.
        """
        with self._lock:
            old = self.ws
            self.ws = None
        if old:
            try:
                old.close()
            except Exception:
                pass
        # ping_interval=None: the library's built-in keepalive thread races
        # our close() and dumps a ConnectionClosedError traceback from its
        # daemon thread at shutdown. Our _pump already pings whenever its
        # recv() times out, so the library's own pinger is redundant.
        ws = websockets.sync.client.connect(
            uri, max_size=None, max_queue=4096, ping_interval=None)
        try:
            ws.send(json.dumps({"user": STAR_USER_REVISION}))
        except Exception:
            try:
                ws.close()
            except Exception:
                pass
            raise
        era = self._era
        with self._lock:
            self.ws = ws
            self.uri = uri
        self._abort.clear()
        self._voices_evt.clear()
        self._thread = threading.Thread(
            target=self._pump, args=(ws, era), daemon=True)
        self._thread.start()
        if wait_voices:
            # The server pushes the voice list right after the hello above; give
            # it a moment so callers that immediately ask for voices (e.g. the
            # auto-connect log line in run.py) see the real list, not a race-lost 0.
            self._voices_evt.wait(VOICES_WAIT)

    # ---- receive pump ------------------------------------------------------

    def _pump(self, ws, era):
        while not self._abort.wait(0.005):
            try:
                message = ws.recv(30)
            except TimeoutError:
                try:
                    ws.ping()
                except Exception:
                    break
                continue
            except Exception:
                break
            if era != self._era or ws is not self.ws:
                break  # socket was replaced; a newer pump owns the connection
            if isinstance(message, bytes):
                self._on_binary(message)
            else:
                try:
                    self._on_json(json.loads(message))
                except json.JSONDecodeError:
                    pass
        # Pump ended. If this is still the live connection for the current era,
        # it dropped unexpectedly -> flag it so the supervisor reconnects.
        self._note_dead(ws, era)

    def _note_dead(self, ws, era):
        """If `ws` is still the live socket of the current era, mark the
        connection dead and schedule a reconnect. No-op for stale pumps and
        after a manual connect/disconnect (era already bumped)."""
        if era != self._era:
            return
        with self._lock:
            if self.ws is not ws:
                return
            self.ws = None
            self._drop_evt.set()

    # ---- auto-reconnect supervisor ------------------------------------------

    def _ensure_supervisor(self):
        if self._supervisor is not None and self._supervisor.is_alive():
            return
        self._supervisor = threading.Thread(
            target=self._supervise, name="star-coag-reconnect", daemon=True)
        self._supervisor.start()

    def _supervise(self):
        while True:
            self._drop_evt.wait()
            self._drop_evt.clear()
            if self._abort.is_set():
                continue  # manual disconnect; nothing to revive
            uri = self.uri
            if not uri:
                continue
            era = self._era
            self._reconnecting.set()
            delay = RECONNECT_BASE_DELAY
            attempt = 0
            try:
                while era == self._era:
                    attempt += 1
                    log.info("STAR coagulator connection lost; reconnecting to %s (attempt %d)",
                             _redact_uri(uri), attempt)
                    try:
                        self._dial(uri, wait_voices=False)
                        log.info("STAR coagulator reconnected (%d voices)", len(self.voices))
                        break
                    except Exception as e:
                        log.warning("STAR reconnect attempt %d failed: %s", attempt, e)
                    # sleep out the backoff; a manual connect/disconnect ends it early
                    deadline = time.monotonic() + delay
                    while time.monotonic() < deadline and era == self._era:
                        time.sleep(0.2)
                    delay = min(delay * 2, RECONNECT_MAX_DELAY)
            finally:
                if era == self._era:
                    self._reconnecting.clear()

    # ---- protocol ------------------------------------------------------------

    def _on_json(self, event):
        if "voices" in event:
            self.voices = event["voices"]
            self._voices_evt.set()
        elif "error" in event:
            # fatal-ish protocol error (e.g. revision mismatch). Wake connect()
            # immediately -- the voice list is never coming.
            self._voices_evt.set()
            self._audio_q.put(("error", event["error"]))
        elif "status" in event and ("abort" in event or "id" in event):
            # provider status/abort (e.g. "400 this voice requires a model name")
            self._audio_q.put(("error", event.get("status", "provider error")))

    def _on_binary(self, message):
        if len(message) < 4:
            return
        meta_len = int.from_bytes(message[:2], "little")
        meta_raw = message[2 : meta_len + 2].decode("utf-8", "replace")
        audio = message[meta_len + 2 :]
        # meta is informational; we just need the audio payload.
        self._audio_q.put(("audio", audio))

    def list_voices(self):
        with self._lock:
            return list(self.voices)

    def _wait_connected(self, timeout):
        """Return True once a live socket exists. While an auto-reconnect is in
        progress, wait up to `timeout` for it to finish; return immediately
        (False) when we are simply not connected."""
        deadline = time.monotonic() + timeout
        while True:
            if self.ws is not None:
                return True
            if not self._reconnecting.is_set() or time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def synthesize(self, textline, timeout=30):
        if not self._wait_connected(RECONNECT_WAIT):
            if self._reconnecting.is_set():
                raise RuntimeError(
                    "Connection to coagulator lost; auto-reconnect in progress -- try again shortly")
            raise RuntimeError("Not connected to a coagulator")
        with self._synth_lock:
            # drain any stale items so we only wait for the response to THIS request
            while not self._audio_q.empty():
                try:
                    self._audio_q.get_nowait()
                except queue.Empty:
                    break
            req_id = f"ttbot_{os.urandom(3).hex()}"
            ws = self.ws
            try:
                ws.send(json.dumps({"user": STAR_USER_REVISION, "request": textline, "id": req_id}))
            except Exception as e:
                # The socket died under us; make sure the reconnect kicks in
                # promptly instead of waiting for the pump to notice.
                self._note_dead(ws, self._era)
                raise RuntimeError(
                    "Connection to coagulator lost mid-request; it will reconnect automatically -- try again shortly") from e
            # wait for the next response frame
            try:
                kind, payload = self._audio_q.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError("STAR synthesis timed out")
            if kind == "error":
                raise RuntimeError(payload)
            return payload
