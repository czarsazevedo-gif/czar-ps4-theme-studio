"""Supervised cancellable subprocess runner with continuous bounded stdout draining.

Eliminates pipe-buffer deadlocks on verbose output (CZAR-AUD-011), propagates
cancellation uniformly across build/validate/extract stages (CZAR-AUD-012), and
always closes stdout handles cleanly without ResourceWarning leaks.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections import deque
from typing import Callable

from .. import media

MAX_LOG_BYTES = 256 * 1024


class SubprocessTimeoutError(RuntimeError):
    """Raised when a supervised subprocess exceeds its configured wall-clock timeout."""


def kill_process_tree(proc: subprocess.Popen | None) -> None:
    """Terminate `proc` and any descendant child processes immediately."""
    if proc is None or proc.poll() is not None:
        return
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_cancellable_process(
    cmd: list[str],
    *,
    timeout: float,
    check: Callable[[], None] = lambda: None,
    register_proc: Callable[[subprocess.Popen | None], None] = lambda *_: None,
    max_log_bytes: int = MAX_LOG_BYTES,
) -> tuple[int, str]:
    """Run `cmd` while continuously draining stdout/stderr into a bounded tail buffer."""
    check()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"),
        creationflags=media.NO_WINDOW,
    )
    register_proc(proc)

    chunks: deque[bytes] = deque()
    kept_bytes = 0

    def _drain() -> None:
        nonlocal kept_bytes
        stream = proc.stdout
        if stream is None:
            return
        try:
            while True:
                data = stream.read(4096)
                if not data:
                    break
                chunks.append(data)
                kept_bytes += len(data)
                while kept_bytes > max_log_bytes and chunks:
                    dropped = chunks.popleft()
                    kept_bytes -= len(dropped)
        except OSError:
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    reader = threading.Thread(target=_drain, name="czar-subproc-drain", daemon=True)
    reader.start()

    started = time.monotonic()
    try:
        while True:
            check()
            rc = proc.poll()
            if rc is not None:
                check()
                reader.join(timeout=5.0)
                return rc, b"".join(chunks).decode("utf-8", "replace")
            if time.monotonic() - started > timeout:
                kill_process_tree(proc)
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    pass
                reader.join(timeout=1.0)
                raise SubprocessTimeoutError(f"O processo excedeu o tempo limite ({timeout:g}s).")
            time.sleep(0.05)
    except BaseException:
        kill_process_tree(proc)
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass
        reader.join(timeout=1.0)
        raise
    finally:
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        register_proc(None)
