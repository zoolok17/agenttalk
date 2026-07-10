from pathlib import Path

from agenttalk._jsonl import append_record, iter_lines


def test_iter_lines_isolates_invalid_utf8_physical_line(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"ok":1}\n{"partial":"\xe2')

    append_record(path, {"ok": 2})

    assert list(iter_lines(path)) == [
        (1, '{"ok":1}\n'),
        (2, None),
        (3, '{"ok": 2}\n'),
    ]
