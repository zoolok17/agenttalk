"""#55 slice-1 PR-B item 6: features.json record assembly
(DESIGN-55-comprehension-plane.md, Artifact 3)."""

from __future__ import annotations

from agenttalk.comprehension import features_artifact as fa
from agenttalk.comprehension.adapters import java as java_adapter


def _parse(relative_path: str, source: str) -> java_adapter.JavaFileResult:
    return java_adapter.parse_java_source(relative_path, source)


def test_a_main_method_produces_one_candidate_feature_with_one_entry_point():
    results = {"App.java": _parse(
        "App.java",
        "package p;\nclass App {\n  public static void main(String[] args) {}\n}\n",
    )}
    entry_points, features = fa.build_features(results)
    assert len(entry_points) == 1
    assert entry_points[0].kind == "cli_main"
    assert len(features) == 1
    assert features[0].state == "candidate"
    assert features[0].origin == "detected"
    assert features[0].label == "App"
    assert entry_points[0].feature_ids == [features[0].feature_id]


def test_two_routes_on_the_same_controller_group_into_one_feature():
    source = (
        "package p;\nclass Controller {\n"
        '  @GetMapping("/api/widgets")\n  void list() {}\n'
        '  @PostMapping("/api/widgets")\n  void create() {}\n'
        "}\n"
    )
    results = {"Controller.java": _parse("Controller.java", source)}
    entry_points, features = fa.build_features(results)
    assert len(entry_points) == 2
    assert len(features) == 1
    assert sorted(features[0].entry_point_ids) == sorted(e.entry_point_id for e in entry_points)


def test_entry_points_on_different_classes_produce_separate_features():
    results = {
        "App.java": _parse(
            "App.java", "package p;\nclass App {\n  public static void main(String[] a) {}\n}\n"),
        "Controller.java": _parse(
            "Controller.java",
            'package p;\nclass Controller {\n  @GetMapping("/x")\n  void get() {}\n}\n',
        ),
    }
    entry_points, features = fa.build_features(results)
    assert len(features) == 2
    assert {f.label for f in features} == {"App", "Controller"}


def test_confirmed_labels_promote_feature_state():
    results = {"App.java": _parse(
        "App.java", "package p;\nclass App {\n  public static void main(String[] a) {}\n}\n")}
    _entry_points, features = fa.build_features(results, confirmed_labels=frozenset({"App"}))
    assert features[0].state == "confirmed"


def test_unconfirmed_label_stays_candidate():
    results = {"App.java": _parse(
        "App.java", "package p;\nclass App {\n  public static void main(String[] a) {}\n}\n")}
    _entry_points, features = fa.build_features(results, confirmed_labels=frozenset({"SomeOther"}))
    assert features[0].state == "candidate"


def test_no_entry_points_means_no_features():
    results = {"Widget.java": _parse("Widget.java", "package p;\nclass Widget {}\n")}
    entry_points, features = fa.build_features(results)
    assert entry_points == []
    assert features == []


def test_ids_are_deterministic_across_two_builds():
    results = {"App.java": _parse(
        "App.java", "package p;\nclass App {\n  public static void main(String[] a) {}\n}\n")}
    first_ep, first_f = fa.build_features(results)
    second_ep, second_f = fa.build_features(results)
    assert {e.entry_point_id for e in first_ep} == {e.entry_point_id for e in second_ep}
    assert {f.feature_id for f in first_f} == {f.feature_id for f in second_f}


def test_to_json_sorts_id_lists():
    results = {"App.java": _parse(
        "App.java", "package p;\nclass App {\n  public static void main(String[] a) {}\n}\n")}
    entry_points, features = fa.build_features(results)
    payload = features[0].to_json()
    assert payload["entry_point_ids"] == sorted(payload["entry_point_ids"])
    assert entry_points[0].to_json()["feature_ids"] == [features[0].feature_id]
