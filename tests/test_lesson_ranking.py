"""The lessons shown before a turn are ranked by how well they fit it, not filtered by scope or tag.

Scope match, tag overlap and shared words rank every active team lesson; "process" keeps only a
small bonus. Pack lessons and expired, retracted or superseded lessons still never take a place.
"""

from __future__ import annotations

from agenttalk import knowledge as kn, knowledge_pack as kp, lesson_context as lc

PROCESS = kn.PROCESS_DOMAIN
TURN = "task build: tune the retry budget against the timeout"


def _lesson(key: str, *, scope: str, trigger: str = "t", body: str = "Rule. Why: w. Check: c.",
            applies_to: tuple[str, ...] = (), updated: str = "2026-01-01T00:00:00Z",
            author: str = "dev", supersedes: tuple[str, ...] = (),
            expires_at: str = "2100-01-01", retract: bool = False, note_id: str | None = None) -> list[dict]:
    loader = author == kp.PACK_AUTHOR
    origin = kn.PACK_RESOLVED_FROM if loader else "active_agent"
    lesson = {"scope": scope, "trigger": trigger, "evidence_ref": "e", "owner": author,
              "review_after": "2019-01-01" if expires_at < "2026" else "2099-01-01", "expires_at": expires_at,
              "applies_to": list(applies_to), "supersedes": list(supersedes)}
    pub = kn.new_publish_event(
        note_id=note_id or "kn-" + key.replace(".", "-").replace(":", "-"), key=key, type=kn.TYPE_LESSON,
        domain_id=PROCESS, body=body, anchor=None, verified_against_sha=None,
        domain_registry_hash="rh", domain_definition_hash=kn.VIRTUAL_PROCESS_DOMAIN_HASH,
        author=author, resolved_from=origin, at=updated, lesson=lesson)
    events = [pub, kn.new_curate_event(base=pub, action="verify", curated_by=author if loader else "lead",
                                       resolved_from=origin, at=updated, reason=None)]
    if retract:  # a retraction curates the verified version, after it
        events.append(kn.new_curate_event(base=events[-1], action="retract", curated_by="lead",
                                          resolved_from="lead", at="2026-12-01T00:00:00Z", reason="wrong advice"))
    return events


def _newer_process_lessons() -> list[dict]:
    """Five newer process lessons that share nothing with the turn: the old rule's favourites."""
    return [event for n in range(5) for event in _lesson(
        f"filler.n{n}", scope="process", trigger="unrelated", body="Nothing in common here.",
        updated=f"2026-10-0{n + 1}T00:00:00Z")]


def _shown(events: list[dict], *, scope: str = "craft", turn: str = TURN, limit: int = 5) -> list[str]:
    picked = lc.select_lessons(events, context_scope=scope, tags=lc.tokenize_tags(turn), limit=limit)
    return [note["key"] for note, _verdict in picked]


def test_an_older_fitting_lesson_of_another_scope_is_shown() -> None:
    # Before: only "process" and the guessed scope were eligible, newest first, so the five newer
    # process lessons filled every place and this review lesson never reached the seat.
    events = _newer_process_lessons() + _lesson(
        "review.retry-budget", scope="review", trigger="reviewing a retry budget change",
        body="Check the retry budget against the timeout before approving.")
    assert _shown(events)[0] == "review.retry-budget"


def test_a_lesson_whose_tags_miss_the_turn_still_competes_on_its_words() -> None:
    events = _newer_process_lessons() + _lesson(
        "craft.retry-timeout", scope="craft", applies_to=("database",),
        trigger="changing a retry budget", body="Keep the retry budget below the timeout.")
    assert _shown(events)[0] == "craft.retry-timeout"


def test_a_matching_tag_counts_more_than_a_shared_word() -> None:
    events = (_lesson("craft.one-word", scope="craft", trigger="retry", body="Only one shared word.",
                      updated="2026-10-09T00:00:00Z")
              + _lesson("craft.tagged", scope="craft", applies_to=("timeout",), trigger="t", body="b."))
    assert _shown(events) == ["craft.tagged", "craft.one-word"]


def test_process_lessons_get_only_a_small_bonus() -> None:
    events = (_lesson("process.newest", scope="process", trigger="unrelated", body="Nothing in common.",
                      updated="2026-10-09T00:00:00Z")
              + _lesson("docs.timeout", scope="docs", trigger="documenting a timeout", body="Say which timeout."))
    assert _shown(events, scope="docs", limit=1) == ["docs.timeout"]


def test_with_no_fit_anywhere_process_and_then_the_newest_come_first() -> None:
    events = (_lesson("craft.newer", scope="craft", updated="2026-10-09T00:00:00Z")
              + _lesson("process.older", scope="process", updated="2026-01-01T00:00:00Z"))
    assert _shown(events, scope="docs", turn="kind note") == ["process.older", "craft.newer"]


def test_pack_lessons_never_take_a_place_even_when_they_fit_best() -> None:
    events = (_lesson("pack.demo:retry", scope="craft", applies_to=("retry",), author=kp.PACK_AUTHOR,
                      trigger="tuning a retry budget", body="The retry budget and the timeout.",
                      updated="2026-10-09T00:00:00Z")
              + _lesson("team.other", scope="docs", trigger="unrelated", body="Nothing in common."))
    assert _shown(events) == ["team.other"]


def test_expired_retracted_and_superseded_lessons_never_take_a_place() -> None:
    fitting = {"scope": "craft", "applies_to": ("retry",), "trigger": "tuning a retry budget",
               "body": "The retry budget and the timeout."}
    events = (_lesson("craft.expired", expires_at="2020-01-01", **fitting)
              + _lesson("craft.retracted", retract=True, **fitting)
              + _lesson("craft.old", **fitting)
              + _lesson("craft.new", scope="docs", trigger="unrelated", body="Nothing in common.",
                        supersedes=("craft.old",)))
    assert _shown(events) == ["craft.new"]


def test_a_changed_lesson_is_ranked_by_its_new_words() -> None:
    first = _lesson("craft.rule", scope="docs", trigger="unrelated", body="Nothing in common.")
    assert _shown(first + _newer_process_lessons())[0] != "craft.rule"
    second = _lesson("craft.rule", scope="docs", trigger="tuning a retry budget", note_id="kn-craft-rule-v2",
                     body="The retry budget and the timeout.", updated="2026-02-01T00:00:00Z")
    assert _shown(first + second + _newer_process_lessons())[0] == "craft.rule"
