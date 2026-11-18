"""Unit tests for reply parsing and edit resolution (no model, no audio).

Run:  python -m pytest tests/test_edits.py     or     python tests/test_edits.py
"""
import random

from vocaret import edits as E

M = E.Markers()


def run(buffer: str, reply: str):
    """Buffer uses | for the caret; returns the new buffer with | at the new caret."""
    caret = buffer.index("|")
    text = buffer.replace("|", "", 1)
    new, pos = E.apply_ops(text, caret, caret, E.parse(reply).ops, M)
    return new[:pos] + "|" + new[pos:]


def test_insert_on_blank_indented_line_with_absolute_indentation():
    out = run("class Foo {\n    |\n}\n",
              "=====INSERT\n    int f() {\n        return 1;\n    }\n=====END")
    assert out == "class Foo {\n    int f() {\n        return 1;\n    }|\n}\n"


def test_insert_with_relative_indentation_is_shifted_under_the_caret():
    out = run("class Foo {\n    |\n}\n", "=====INSERT\nint f() {\n    return 1;\n}\n=====END")
    assert out == "class Foo {\n    int f() {\n        return 1;\n    }|\n}\n"


def test_insert_with_only_first_line_unindented_keeps_absolute_rest():
    out = run("class Foo {\n    |\n}\n",
              "=====INSERT\nint f() {\n        return 1;\n    }\n=====END")
    assert out == "class Foo {\n    int f() {\n        return 1;\n    }|\n}\n"


def test_insert_before_existing_code_goes_on_its_own_line():
    assert run("a = 1\n|b = 2\n", "=====INSERT\nx = 0\n=====END") == "a = 1\nx = 0|\nb = 2\n"
    assert (run("def f():\n    |return 1\n", "=====INSERT\n    x = 0\n=====END")
            == "def f():\n    x = 0|\n    return 1\n")


def test_insert_after_code_is_literal():
    assert run("x = |\n", "=====INSERT\nfoo(3)\n=====END") == "x = foo(3)|\n"
    assert run("int x = 5;|\n", "=====INSERT\n\nint y = 6;\n=====END") == "int x = 5;\nint y = 6;|\n"


def test_insert_replaces_selection():
    text = "a = old_value\n"
    start = text.index("old_value")
    new, pos = E.apply_ops(text, start, start + 9, E.parse("=====INSERT\n42\n=====END").ops, M)
    assert new == "a = 42\n" and pos == 6


def test_replace_exact_and_caret_marker():
    out = run("def f():|\n    return 1\n",
              "=====REPLACE\ndef f():\n    return 1\n=====WITH\ndef g():\n    return [[CURSOR]]2\n=====END")
    assert out == "def g():\n    return |2\n"


def test_replace_tolerates_indentation_mismatch():
    out = run("|def f():\n\treturn 1\n",
              "=====REPLACE\ndef f():\n    return 1\n=====WITH\ndef f():\n\treturn 2\n=====END")
    assert out == "|def f():\n\treturn 2\n"


def test_replace_picks_the_occurrence_nearest_the_caret():
    out = run("x = 1\ny = 0\nx = 1|\n", "=====REPLACE\nx = 1\n=====WITH\nx = 2\n=====END")
    assert out == "x = 1\ny = 0\nx = 2|\n"


def test_replace_ignores_markers_copied_into_old_text():
    out = run("a = 1|\n", "=====REPLACE\na = 1[[CURSOR]]\n=====WITH\na = 2\n=====END")
    assert out == "a = 2|\n"


def test_caret_stays_put_inside_unchanged_text():
    out = run("let x = 0;\nfoo(|);\nreturn x;\n",
              "=====REPLACE\nlet x = 0;\n=====WITH\nlet count = 0;\n=====END\n"
              "=====REPLACE\nreturn x;\n=====WITH\nreturn count;\n=====END")
    assert out == "let count = 0;\nfoo(|);\nreturn count;\n"


def test_delete_and_multiple_operations():
    out = run("import a\n\nx = 1\ny = 2|\n",
              "=====REPLACE\nimport a\n=====WITH\nimport a\nimport b\n=====END\n"
              "=====REPLACE\nx = 1\n=====WITH\n=====END")
    assert out == "import a\nimport b\n\n\ny = 2|\n"


def test_missing_text_and_overlap_are_errors():
    for reply in ("=====REPLACE\nnope\n=====WITH\nx\n=====END",
                  "=====REPLACE\na = 1\nb = 2\n=====WITH\nq\n=====END\n"
                  "=====REPLACE\nb = 2\n=====WITH\nr\n=====END"):
        try:
            run("a = 1\nb = 2|\n", reply)
        except E.EditError:
            continue
        raise AssertionError("expected EditError")


def test_parse_undo_note_fences_and_truncation():
    assert E.parse("=====UNDO").undo
    assert E.parse("```\n=====NOTE: nothing to do\n```").note == "nothing to do"
    assert E.parse("sure!\n=====INSERT\nx\n=====END").ops == [("insert", "x")]
    try:
        E.parse("=====INSERT\nx = 1")
    except E.EditError:
        pass
    else:
        raise AssertionError("a cut-off reply must not be applied")


def test_markers_avoid_text_already_in_the_file():
    markers = E.make_markers("print('[[CURSOR]]')")
    assert markers.cursor != "[[CURSOR]]" and markers.cursor not in "print('[[CURSOR]]')"


def test_diff_roundtrip_is_exact_and_minimal():
    rng = random.Random(7)
    alphabet = ["a", "b", "  x = 1", "}", "{", "", "foo()", "é\U0001F600z"]
    for _ in range(2000):
        old = "\n".join(rng.choice(alphabet) for _ in range(rng.randint(0, 8))) + rng.choice(["", "\n"])
        new = "\n".join(rng.choice(alphabet) for _ in range(rng.randint(0, 8))) + rng.choice(["", "\n"])
        edits = E.diff_edits(old, new)
        assert E.apply_edits(old, edits) == new
        for (_, end, _), (start, _, _) in zip(edits, edits[1:]):
            assert end <= start
    old = "line1\nline2\nline3\n"
    assert E.diff_edits(old, "line1\nline2 changed\nline3\n") == [(11, 11, " changed")]


def test_positions_use_utf16_columns():
    pm = E.PositionMap("a\U0001F600b\ncd")
    assert pm.to_position(3) == {"line": 0, "character": 4}
    assert pm.to_index({"line": 0, "character": 3}) == 2
    assert pm.to_position(5) == {"line": 1, "character": 1}
    assert pm.to_index({"line": 9, "character": 99}) == 6


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for name, fn in tests:
        fn()
        print("ok", name)
    print(f"{len(tests)} passed")
