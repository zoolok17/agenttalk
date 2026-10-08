"""Catch doc dependencies that the CI classifier's conservative scan misses."""

import os
import runpy
import sys
from contextlib import contextmanager
from pathlib import Path


class DocReadGuard:
    def __init__(self, repo: Path):
        scope = runpy.run_path(str(repo / "scripts/ci_scope.py"))
        self.referenced = scope["test_document_names"](repo)
        self.skippable = scope["skippable_document"]
        self.prefix = os.path.normcase(str(repo.resolve())) + os.sep
        self.reads = None
        sys.addaudithook(self.record)

    def record(self, event, args):
        if self.reads is None or event != "open" or not isinstance(args[0], (str, bytes)):
            return
        name = os.fsdecode(args[0])
        if not name.endswith(".md"):
            return
        absolute = os.path.abspath(name)
        path = os.path.normcase(absolute)
        if path.startswith(self.prefix):
            relative = path[len(self.prefix):].replace(os.sep, "/")
            # Preserve the real spelling for the case-sensitive Git path rule.
            original = absolute[len(self.prefix):].replace(os.sep, "/")
            if self.skippable(original, self.referenced):
                self.reads.add(relative)

    @contextmanager
    def check(self):
        reads = set()
        self.reads = reads
        try:
            yield
        finally:
            self.reads = None
            assert not reads, (
                "Tests opened documents that CI would skip: " + ", ".join(sorted(reads))
                + ". Name the document literally in the test so the scope scan protects it."
            )
