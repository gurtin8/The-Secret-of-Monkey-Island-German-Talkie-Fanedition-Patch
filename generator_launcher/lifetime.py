"""Keep a native launcher's server and helper processes tied to its lifetime."""
from __future__ import annotations

import os
import signal
import threading


class NativeHostLifetime:
    def __init__(self, parent_pid=None, native_owned=False):
        self.parent_pid = parent_pid
        self.native_owned = native_owned
        self.done = threading.Event()
        self.watcher = None
        self.previous_handler = None

    def __enter__(self):
        if not self.native_owned:
            if self.parent_pid is not None:
                raise ValueError('Launcher-Elternprozess benötigt --native-owned')
            return self
        if self.parent_pid is None or self.parent_pid <= 1 or os.getppid() != self.parent_pid:
            raise ValueError('Launcher-Elternprozess stimmt nicht mit dem Serverstart überein')
        # Never signal the GUI's or a shell's process group. Foundation may
        # already spawn a separate group; otherwise isolate it here.
        if os.getpgrp() != os.getpid():
            os.setsid()
        if os.getpgrp() != os.getpid():
            raise RuntimeError('Eigene Server-Prozessgruppe konnte nicht eingerichtet werden')
        self.previous_handler = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, self.terminate)
        self.watcher = threading.Thread(target=self.watch_parent, daemon=True)
        self.watcher.start()
        return self

    @staticmethod
    def terminate(_signum, _frame):
        # Let main's finally/context cleanup close sockets and release the lock,
        # even if the user closes the GUI during a foreground operation.
        raise SystemExit(0)

    def watch_parent(self):
        while not self.done.wait(0.2):
            # A dead parent reparents us to launchd. Checking the relationship
            # avoids confusing a subsequently reused PID with our launcher.
            if os.getppid() != self.parent_pid:
                os.kill(os.getpid(), signal.SIGTERM)
                return

    def __exit__(self, *_exc):
        self.done.set()
        if self.watcher is not None:
            self.watcher.join(timeout=0.3)
        if self.native_owned and os.getpgrp() == os.getpid():
            # Stop only children in the explicitly isolated server group; never
            # all Python/Wine processes or the user's other launcher sessions.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            try:
                os.killpg(os.getpid(), signal.SIGTERM)
            except ProcessLookupError:
                pass
            finally:
                signal.signal(signal.SIGTERM, self.previous_handler)
        return False
