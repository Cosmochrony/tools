#!/usr/bin/env python3
"""Run zenodo_publish.py (or one failure-injection case) against the SANDBOX without the token ever being
on a command line.

    python3 tools/pilot/sandbox_launch.py [--case F-x] -- <zenodo_publish.py arguments, incl. --sandbox>

The sandbox token is read from the terminal (getpass: /dev/tty, no echo, not in the shell history) and is put
into THIS process's environment only; the tool then runs in-process. Consequences:
  * the token is never in an argument vector (nothing for `ps` to show, in this process or in any child). It IS in
    the environment of this process: a child process inherits that environment unless it is scrubbed. The tool
    runs its four `git` helper calls with a scrubbed environment (no ZENODO_* variable); the commands of the
    pre-deposit battery (predeposit_check.py) and any other child would inherit it;
  * the process environment is reduced to PATH and HOME plus ZENODO_SANDBOX_TOKEN, and the launcher refuses if
    the production variable ZENODO_TOKEN is present at all;
  * the arguments after `--` must contain --sandbox (the tool then reads ONLY ZENODO_SANDBOX_TOKEN).
Run it once per command: it asks for the token each time (a pilot is a few dozen commands, on purpose).
"""
from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
KEEP = ("PATH", "HOME")


def main(argv=None, getpass_fn=getpass.getpass, zp=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        sys.exit("usage: sandbox_launch.py [--case F-x] -- <zenodo_publish arguments, incl. --sandbox>")
    cut = argv.index("--")
    own, passthrough = argv[:cut], argv[cut + 1:]
    case = None
    if own:
        if len(own) == 2 and own[0] == "--case":
            case = own[1]
        else:
            sys.exit("sandbox_launch.py: only `--case F-x` may precede `--`")
    if "--sandbox" not in passthrough:
        sys.exit("sandbox_launch.py: REFUSED: the arguments after `--` must contain --sandbox")
    if "ZENODO_TOKEN" in os.environ:
        sys.exit("sandbox_launch.py: REFUSED: ZENODO_TOKEN (production) is present in the environment; unset it "
                 "in this shell")
    token = getpass_fn("sandbox token (not echoed): ").strip()
    if not token:
        sys.exit("sandbox_launch.py: no token entered. Nothing was sent.")
    kept = {k: os.environ[k] for k in KEEP if k in os.environ}
    os.environ.clear()
    os.environ.update(kept)
    os.environ["ZENODO_SANDBOX_TOKEN"] = token          # this process's environment; never argv (a child inherits
    #                                                     the environment unless it scrubs it, as the tool's git calls do)
    if zp is None:
        sys.path.insert(0, str(TOOLS))
        import zenodo_publish as zp  # noqa: PLC0415
    if case:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import inject_failures  # noqa: PLC0415
        return inject_failures.main(["--sandbox", "--case", case, "--", *passthrough], zp=zp, environ=os.environ)
    try:
        zp.main(passthrough)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (print(exc.code, file=sys.stderr) or 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
