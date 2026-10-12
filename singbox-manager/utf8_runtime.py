"""Keep CLI text, configuration files and child processes in UTF-8."""

import os
import sys


def ensure_utf8():
    # Limit the change to this process tree; do not modify system locales.
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    if not sys.flags.utf8_mode:
        # Python chooses the default file encoding at startup. Re-enter before
        # any CLI action so Chinese names survive JSON reads and writes too.
        os.execv(sys.executable, [sys.executable, "-X", "utf8", *sys.argv])
    for stream, errors in ((sys.stdin, "surrogateescape"),
                           (sys.stdout, "strict"), (sys.stderr, "backslashreplace")):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors=errors)
