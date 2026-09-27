"""The double-click start (``hike_finder.launch``): ask for a contact once, then open.

Every test points the per-user folder at ``tmp_path`` (both the Windows and the XDG
variable, so it holds on either OS): the saved answer lives there, and a contact the
developer saved with their own double-click must neither leak into these tests nor be
overwritten by them. ``web.main`` is stubbed — what is under test is what reaches it.
"""
import os

import pytest

from hike_finder import launch, web


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    # setenv-then-delenv, not a bare delenv: launch.main writes os.environ directly, and
    # monkeypatch only restores a variable it recorded — a bare delenv of an absent one
    # records nothing, so the contact set here would leak into every later test.
    monkeypatch.setenv("HIKE_OVERPASS_UA", "")
    monkeypatch.delenv("HIKE_OVERPASS_UA")
    calls = []
    monkeypatch.setattr(web, "main", lambda argv: calls.append(argv))
    return calls


def _answers(monkeypatch, *replies):
    """Feed ``input()``; running out means stdin closed (EOFError)."""
    it = iter(replies)
    asked = []

    def _input(prompt=""):
        asked.append(prompt)
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", _input)
    return asked


def test_asks_saves_and_starts_with_the_answer(home, monkeypatch):
    _answers(monkeypatch, "me@example.org")
    launch.main(["--port", "9000"])
    assert os.environ["HIKE_OVERPASS_UA"] == "me@example.org"
    assert launch.contact_file().read_text(encoding="utf-8").strip() == "me@example.org"
    assert home == [["--port", "9000", "--open"]]


def test_a_saved_answer_is_used_without_asking(home, monkeypatch):
    launch.contact_file().parent.mkdir(parents=True, exist_ok=True)
    launch.contact_file().write_text("saved@example.org\n", encoding="utf-8")
    asked = _answers(monkeypatch)
    launch.main([])
    assert asked == []
    assert os.environ["HIKE_OVERPASS_UA"] == "saved@example.org"


def test_a_contact_you_set_yourself_wins_and_nothing_is_saved(home, monkeypatch):
    monkeypatch.setenv("HIKE_OVERPASS_UA", "mine@example.org")
    asked = _answers(monkeypatch)
    launch.main([])
    assert asked == []
    assert os.environ["HIKE_OVERPASS_UA"] == "mine@example.org"
    assert not launch.contact_file().exists()


def test_a_bad_answer_is_asked_again(home, monkeypatch):
    asked = _answers(monkeypatch, "not an email", "me@example.org")
    launch.main([])
    assert len(asked) == 2
    assert os.environ["HIKE_OVERPASS_UA"] == "me@example.org"


def test_enter_skips_without_saving_so_it_asks_next_time(home, monkeypatch):
    _answers(monkeypatch, "")
    launch.main([])
    assert "HIKE_OVERPASS_UA" not in os.environ
    assert not launch.contact_file().exists()
    assert home == [["--open"]]  # the UI still starts


def test_no_keyboard_does_not_crash(home, monkeypatch):
    _answers(monkeypatch)  # immediate EOFError
    launch.main([])
    assert home == [["--open"]]


def test_help_does_not_ask(home, monkeypatch):
    asked = _answers(monkeypatch)
    launch.main(["--help"])
    assert asked == []
    assert home == [["--help", "--open"]]


@pytest.mark.parametrize("text,ok", [
    ("me@example.org", True),
    ("https://example.org/contact", True),
    ("me@localhost", False),               # no dot after the @
    ("two words@example.org", False),      # whitespace can't go in a User-Agent
    ("me@example.org\r\nX-Evil: 1", False),  # nor can a line break
    ("", False),
])
def test_what_counts_as_a_contact(text, ok):
    assert launch.valid_contact(text) is ok


def test_a_hand_edited_saved_file_that_is_not_a_contact_is_asked_over(home, monkeypatch):
    launch.contact_file().parent.mkdir(parents=True, exist_ok=True)
    launch.contact_file().write_text("garbage\n", encoding="utf-8")
    _answers(monkeypatch, "me@example.org")
    launch.main([])
    assert os.environ["HIKE_OVERPASS_UA"] == "me@example.org"
