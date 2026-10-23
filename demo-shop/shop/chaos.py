"""Fault switches for the Negahban demo. Every switch requires the X-Chaos-Token header (env CHAOS_TOKEN).
State lives in memory only, so a restart clears it (which is exactly what an operator would observe)."""
import os, threading, time

STATE = {"slow_ms": 0, "leak_mb": 0, "leaking": False, "leak_target_mb": 0}
_LEAK = []
_lock = threading.Lock()


def authorised(token):
    want = os.environ.get("CHAOS_TOKEN", "")
    return bool(want) and token == want


def _leaker(target_mb, step_mb=6, every=1.0):
    STATE["leaking"] = True
    while STATE["leaking"] and STATE["leak_mb"] < target_mb:
        with _lock:
            _LEAK.append(bytearray(step_mb * 1024 * 1024))  # touched memory: counts as RSS
            for b in _LEAK[-1:]:
                for i in range(0, len(b), 4096):
                    b[i] = 1
            STATE["leak_mb"] += step_mb
        print(f"cache: session cache grew to {STATE['leak_mb']} MB", flush=True)
        time.sleep(every)
    STATE["leaking"] = False


def start_leak(target_mb=700):
    STATE["leak_target_mb"] = target_mb
    if not STATE["leaking"]:
        threading.Thread(target=_leaker, args=(target_mb,), daemon=True).start()


def reset():
    STATE.update(slow_ms=0, leaking=False, leak_mb=0, leak_target_mb=0)
    with _lock:
        _LEAK.clear()
