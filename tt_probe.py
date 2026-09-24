#!/usr/bin/env python3
"""One-shot diagnostic: replicate the bot's login, then dump EVERYTHING raw.

Run:  uv run python tt_probe.py

Prints every raw event the DLL queue yields for ~6s after doLogin, plus
client-state flags, getMyUserID/getRootChannelID/getServerChannels and
getChannelIDFromPath results. No bot logic involved -- pure SDK observation,
so we can see whether the DLL event queue is truly silent or the bot's pump
is misreading it.

Credentials come from config.py / config.local.py, exactly like the bot.
"""
import os
import sys
import time
import faulthandler

# If anything in the SDK deadlocks, dump all thread stacks after 45s and die.
faulthandler.dump_traceback_later(45, exit=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Same library resolution as bot.py: vendored SDK pair, $TT_SDK_DIR, system.
from tt_sdk import load as _tt_sdk_load
_tt_lib, _tt_src = _tt_sdk_load()
print("Resolved native lib:", _tt_lib, "(%s)" % _tt_src, flush=True)

import config  # noqa: E402  (pulls in config.local.py overrides if present)
import importlib.util as ilu

_spec = ilu.spec_from_file_location(
    "teamtalk.implementation.TeamTalkPy",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tt_vendor", "TeamTalkPy", "__init__.py"),
)
_mod = ilu.module_from_spec(_spec)
sys.modules["teamtalk.implementation.TeamTalkPy"] = _mod
_spec.loader.exec_module(_mod)
from teamtalk.implementation.TeamTalkPy import TeamTalk5 as sdk  # noqa: E402

cfg = config.CONFIG

def out(*a):
    print(*a, flush=True)

out("=== TT probe ===")
# NOTE: intentionally NOT calling sdk.getVersion() -- the vendored wrapper
# declares TT_GetVersion with a bogus restype (TTCHAR_P) and calling it can
# access-violate.
out("Host:", cfg["host"], "tcp", cfg["tcp_port"], "udp", cfg["udp_port"], "enc", cfg.get("encrypted"))
out("User:", cfg.get("username"))

# Which TeamTalk5.dll did the OS actually load? A stale copy earlier on PATH
# is a classic cause of half-working SDK behavior on Windows.
import ctypes
_k32 = ctypes.windll.kernel32
_h = _k32.GetModuleHandleW("TeamTalk5.dll")
if _h:
    _buf = ctypes.create_unicode_buffer(512)
    _k32.GetModuleFileNameW(_h, _buf, 512)
    out("Loaded TeamTalk5.dll from:", _buf.value)
else:
    out("TeamTalk5.dll module handle: not loaded yet (will resolve on first TeamTalk())")

tt = sdk.TeamTalk()
_h = _k32.GetModuleHandleW("TeamTalk5.dll")
if _h:
    _buf = ctypes.create_unicode_buffer(512)
    _k32.GetModuleFileNameW(_h, _buf, 512)
    out("TeamTalk5.dll actually loaded from:", _buf.value)
ok = tt.connect(cfg["host"], int(cfg["tcp_port"]), int(cfg["udp_port"]),
                nLocalTcpPort=0, nLocalUdpPort=0, bEncrypted=bool(cfg.get("encrypted", False)))
out("connect ->", ok, "(async: now waiting for handshake)")

# Mirror the bot's fix: TT_Connect is async; doLogin before CLIENT_CONNECTED
# is illegal and returns -1.
_t0 = time.time()
while not (int(tt.getFlags()) & 0x00004000) and time.time() - _t0 < 10:
    time.sleep(0.05)
out(f"handshake complete after {time.time()-_t0:.2f}s, flags=0x{int(tt.getFlags()):08x}")

ok = tt.doLogin(cfg.get("nickname", "starbot"), cfg.get("username", ""), cfg.get("password", ""), "STAR-TT-Probe")
out("doLogin ->", ok)

# ClientEvent -> name table for readable dumps
_ev_names = {}
for name in dir(sdk.ClientEvent):
    if name.startswith("CLIENTEVENT"):
        _ev_names[int(getattr(sdk.ClientEvent, name))] = name

_tttype_names = {}
try:
    for name in dir(sdk.TTType):
        if name.isupper():
            _tttype_names[int(getattr(sdk.TTType, name))] = name
except Exception:
    pass

FLAGS = [
    (0x00002000, "CLIENT_CONNECTING"),
    (0x00004000, "CLIENT_CONNECTED"),
    (0x00008000, "CLIENT_AUTHORIZED"),
    (0x00010000, "CLIENT_STREAM_AUDIO"),
]

def dump_flags():
    try:
        f = int(tt.getFlags())
    except Exception as e:
        out("  getFlags error:", e)
        return
    active = [n for bit, n in FLAGS if f & bit]
    out(f"  flags=0x{f:08x} [{' '.join(active) or 'none-of-interest'}]")

def describe(msg):
    ev = int(msg.nClientEvent)
    name = _ev_names.get(ev, "?")
    tttype = int(msg.ttType)
    tname = _tttype_names.get(tttype, str(tttype))
    extra = ""
    try:
        if ev == int(sdk.ClientEvent.CLIENTEVENT_CMD_CHANNEL_NEW) and msg.channel:
            ch = msg.channel
            extra = f" channel id={int(ch.nChannelID)} parent={int(ch.nParentID)} name={sdk.ttstr(ch.szName)!r}"
        elif ev == int(sdk.ClientEvent.CLIENTEVENT_CMD_ERROR):
            em = msg.clienterrormsg
            extra = f" nError={int(em.nErrorNo)} text={sdk.ttstr(em.szErrorMsg)!r}"
        elif ev == int(sdk.ClientEvent.CLIENTEVENT_CMD_MYSELF_LOGGEDIN):
            extra = f" nSource={int(msg.nSource)}"
        elif ev == int(sdk.ClientEvent.CLIENTEVENT_CMD_USER_TEXTMSG) and msg.textmessage:
            extra = f" text={sdk.ttstr(msg.textmessage.szMessage)!r}"
    except Exception as e:
        extra = f" <union render error: {e}>"
    return f"  EVENT {ev} ({name}) ttType={tname} src={int(msg.nSource)}{extra}"

out("\n--- draining raw events for ~7s ---")
start = time.time()
empty_polls = 0
events = 0
last_flags = time.time()
while time.time() - start < 7.0:
    m = tt.getMessage(200)
    if int(m.nClientEvent) == 0 and int(m.ttType) == 0:
        empty_polls += 1
    else:
        events += 1
        out(describe(m))
    if time.time() - last_flags >= 1.0:
        last_flags = time.time()
        dump_flags()
        out(f"  getMyUserID={tt.getMyUserID()} getRootChannelID={tt.getRootChannelID()} "
            f"getMyChannelID={tt.getMyChannelID()}")

out(f"\n--- summary: {events} real events, {empty_polls} empty polls in 7s ---")

out("\n--- direct queries (no events needed) ---")
chs = tt.getServerChannels()
out("getServerChannels ->", len(chs), "channel(s)")
for ch in list(chs)[:25]:
    out(f"  id={int(ch.nChannelID)} parent={int(ch.nParentID)} name={sdk.ttstr(ch.szName)!r}")
root = tt.getRootChannelID()
out("getRootChannelID ->", root)
if root > 0:
    try:
        out("root path =", sdk.ttstr(tt.getChannelPath(root)))
    except Exception as e:
        out("getChannelPath failed:", e)
out("getChannelIDFromPath('/hangout area/') ->", tt.getChannelIDFromPath("/hangout area/"))
out("getChannelIDFromPath('hangout area') ->", tt.getChannelIDFromPath("hangout area"))
out("getMyUserID ->", tt.getMyUserID(), " getMyChannelID ->", tt.getMyChannelID())
out("\nDone. (Not disconnecting cleanly on purpose; process exits here.)")
