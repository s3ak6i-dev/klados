# Klados base-image RNG fork-safety hook. Auto-imported by every Python process on
# PYTHONPATH, so it applies with zero application code changes.
#
# KLADOS_RESEED=event (used with the guest agent): reseed userspace PRNGs (random + OpenSSL)
#   the moment /run/klados/forkgen changes. Two layers:
#     - call-site interception: random.getrandbits/random and ssl.RAND_bytes check the
#       generation and reseed INLINE before returning -> zero-window on the security path.
#     - a fast backup poller for code paths that don't hit the wrapped calls.
#   Preserves app-set determinism between forks (only reseeds on an actual fork event).
# KLADOS_RESEED=periodic (no agent): rekey every KLADOS_RESEED_MS ms. Robust, needs no
#   signal, but bounds (not eliminates) the collision window and clobbers app-set seeds.
import os
import threading
import time

_GEN = "/run/klados/forkgen"
_state = {"gen": None}


def _read_gen():
    try:
        with open(_GEN) as f:
            return f.read().strip()
    except Exception:
        return None


def _reseed():
    import random
    random.seed(os.urandom(32))
    try:
        import ssl
        ssl.RAND_add(os.urandom(32), 32.0)
    except Exception:
        pass


def _maybe_reseed():
    g = _read_gen()
    if g is not None and g != _state["gen"]:
        if _state["gen"] is not None:
            _reseed()
        _state["gen"] = g


def _install_wrappers():
    import random as _r
    _orig_gr, _orig_rr = _r.getrandbits, _r.random

    def _gr(k):
        _maybe_reseed()
        return _orig_gr(k)

    def _rr():
        _maybe_reseed()
        return _orig_rr()

    _r.getrandbits = _gr
    _r.random = _rr
    try:
        import ssl as _s
        _orig_rb = _s.RAND_bytes

        def _rb(n):
            _maybe_reseed()
            return _orig_rb(n)

        _s.RAND_bytes = _rb
    except Exception:
        pass


def _backup_poller():
    while True:
        _maybe_reseed()
        time.sleep(0.002)


def _periodic():
    interval = float(os.environ.get("KLADOS_RESEED_MS", "20")) / 1000.0
    while True:
        time.sleep(interval)
        _reseed()


def _start():
    mode = os.environ.get("KLADOS_RESEED", "periodic")
    if mode == "event":
        _state["gen"] = _read_gen()  # baseline; reseed only on change
        _install_wrappers()
        threading.Thread(target=_backup_poller, daemon=True).start()
    elif mode == "periodic":
        threading.Thread(target=_periodic, daemon=True).start()


try:
    _start()
except Exception:
    pass
