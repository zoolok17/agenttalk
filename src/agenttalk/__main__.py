import sys

from agenttalk.cli import console_main

# Thin by design: this is one of several real top-level entry points (the
# installed `agenttalk` console script and cli.py's own `__main__` guard
# are the others) that must all get the SAME bounded-uncaught-exception
# behavior. That behavior lives once, in console_main - see its docstring
# for why it is safe to share across every real entry point without
# affecting a program that imports cli and calls main() directly.
if __name__ == "__main__":
    sys.exit(console_main())
