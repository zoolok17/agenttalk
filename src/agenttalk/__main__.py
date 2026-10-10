import sys

# The gateway's scheduled task runs "pythonw.exe -m agenttalk ... gateway run", which has
# no stdout or stderr. Its log is set up before the CLI and the gateway code load, so a
# failure while they load is written down too.
if sys.argv[-2:] == ["gateway", "run"] and (sys.stdout is None or sys.stderr is None):
    from agenttalk.gateway_run_log import route_missing_output_to_log

    route_missing_output_to_log()

from agenttalk.cli import console_main  # noqa: E402 - after the log above

# Thin by design: this is one of several real top-level entry points (the
# installed `agenttalk` console script and cli.py's own `__main__` guard
# are the others) that must all get the SAME bounded-uncaught-exception
# behavior. That behavior lives once, in console_main - see its docstring
# for why it is safe to share across every real entry point without
# affecting a program that imports cli and calls main() directly.
if __name__ == "__main__":
    sys.exit(console_main())
