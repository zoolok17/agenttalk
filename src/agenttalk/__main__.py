import sys

from agenttalk.cli import main

if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        # main() already let this propagate with its original type so an
        # embedder calling cli.main([...]) directly - which never reaches
        # this guard at all - gets it back uncaught, exactly as it would
        # from calling main() with no wrapper involved. This IS that
        # uncaught boundary for a real `python -m agenttalk` invocation:
        # bound the traceback Python's own default printer would otherwise
        # write here, unbounded, into a supervised wrapper's redirected
        # stderr file.
        from agenttalk.wrapper_logs import print_bounded_uncaught_exception

        print_bounded_uncaught_exception()
        sys.exit(1)
