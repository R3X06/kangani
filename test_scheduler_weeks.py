"""Unit tests for the recess-aware week-number math in scheduler.py.

Pure functions, no DB or network -- runnable either under pytest
(`pytest test_scheduler_weeks.py`) or standalone (`python test_scheduler_weeks.py`).

Terminology:
- CONTINUOUS week = raw 7-day blocks since the anchor, recess weeks included.
- OFFICIAL week    = what the school calls it: continuous minus recess weeks
                     that fall before it. This is what /week N and week_pattern mean.
"""

from datetime import date, timedelta

import scheduler

ANCHOR = date(2026, 8, 10)  # a Monday = week 1
WEEK = timedelta(weeks=1)
# Offset from a week's Monday to its Wednesday (a representative teaching day).
WED = timedelta(days=2)


def _monday_of_continuous(c: int) -> date:
    """The Monday of the c-th continuous week."""
    return ANCHOR + (c - 1) * WEEK


# --- _raw_week_number: uncapped, no recess adjustment ---------------------

def test_raw_week_number_basic():
    assert scheduler._raw_week_number(ANCHOR, ANCHOR) == 1
    assert scheduler._raw_week_number(ANCHOR, date(2026, 8, 16)) == 1  # Sun of wk1
    assert scheduler._raw_week_number(ANCHOR, date(2026, 8, 17)) == 2  # next Mon
    assert scheduler._raw_week_number(ANCHOR, date(2026, 8, 9)) is None  # before anchor


def test_raw_week_number_uncapped():
    # No 13-week cap: continuous count keeps going.
    assert scheduler._raw_week_number(ANCHOR, ANCHOR + 19 * WEEK) == 20


# --- compute_week_number: official, recess-aware, capped at 13 ------------

def test_compute_no_recess_matches_legacy():
    # With no recess, official == continuous, capped at 13.
    for c in range(1, 14):
        assert scheduler.compute_week_number(ANCHOR, _monday_of_continuous(c)) == c
    assert scheduler.compute_week_number(ANCHOR, _monday_of_continuous(14)) is None
    assert scheduler.compute_week_number(ANCHOR, date(2026, 8, 9)) is None


def test_compute_recess_week_itself_has_no_number():
    recess = frozenset({7})
    # Continuous week 7 IS the recess week -> no official number.
    assert scheduler.compute_week_number(ANCHOR, _monday_of_continuous(7), recess) is None


def test_compute_shifts_weeks_after_recess():
    recess = frozenset({7})
    # Before the recess: unchanged.
    assert scheduler.compute_week_number(ANCHOR, _monday_of_continuous(6), recess) == 6
    # After the recess: each continuous week is one official number lower.
    assert scheduler.compute_week_number(ANCHOR, _monday_of_continuous(8), recess) == 7
    assert scheduler.compute_week_number(ANCHOR, _monday_of_continuous(9), recess) == 8
    # The teaching semester now extends one continuous week further (to c=14).
    assert scheduler.compute_week_number(ANCHOR, _monday_of_continuous(14), recess) == 13
    assert scheduler.compute_week_number(ANCHOR, _monday_of_continuous(15), recess) is None


def test_compute_two_recess_weeks():
    recess = frozenset({7, 14})
    assert scheduler.compute_week_number(ANCHOR, _monday_of_continuous(6), recess) == 6
    assert scheduler.compute_week_number(ANCHOR, _monday_of_continuous(7), recess) is None
    assert scheduler.compute_week_number(ANCHOR, _monday_of_continuous(8), recess) == 7
    assert scheduler.compute_week_number(ANCHOR, _monday_of_continuous(14), recess) is None
    assert scheduler.compute_week_number(ANCHOR, _monday_of_continuous(15), recess) == 13


# --- official_to_continuous: the inverse ----------------------------------

def test_official_to_continuous_no_recess():
    for w in range(1, 14):
        assert scheduler.official_to_continuous(w) == w


def test_official_to_continuous_single_recess():
    recess = frozenset({7})
    assert scheduler.official_to_continuous(6, recess) == 6   # before recess
    assert scheduler.official_to_continuous(7, recess) == 8   # skips continuous 7
    assert scheduler.official_to_continuous(8, recess) == 9


def test_official_to_continuous_two_recess():
    recess = frozenset({7, 8})  # a two-week break
    assert scheduler.official_to_continuous(7, recess) == 9
    assert scheduler.official_to_continuous(6, recess) == 6


def test_round_trip_inverse():
    # compute_week_number(official_to_continuous(w)) == w for every official w.
    for recess in (frozenset(), frozenset({7}), frozenset({7, 14}), frozenset({7, 8})):
        for w in range(1, 14):
            c = scheduler.official_to_continuous(w, recess)
            assert scheduler.compute_week_number(ANCHOR, _monday_of_continuous(c), recess) == w, (
                f"round trip failed for official week {w}, recess={set(recess)}"
            )


# --- resolve_week_range: /week N lands on the recess-shifted calendar week -

def test_resolve_week_range_shifts_with_recess():
    recess = frozenset({7})
    today = date(2026, 8, 12)  # irrelevant when an explicit week is given
    # Official week 8 lives in continuous week 9 -> Monday = anchor + 8 weeks.
    monday, sunday, label = scheduler.resolve_week_range(ANCHOR, today, 8, recess)
    assert monday == _monday_of_continuous(9)
    assert sunday == monday + (date(2026, 8, 16) - date(2026, 8, 10))
    assert label == 8


def test_resolve_week_range_current_week_labelled_officially():
    recess = frozenset({7})
    # "today" sits in continuous week 9 -> official week 8.
    today = _monday_of_continuous(9) + (date(2026, 8, 13) - date(2026, 8, 10))
    monday, _sunday, label = scheduler.resolve_week_range(ANCHOR, today, None, recess)
    assert monday == _monday_of_continuous(9)
    assert label == 8


def test_resolve_week_range_no_anchor_raises_for_explicit_week():
    try:
        scheduler.resolve_week_range(None, date(2026, 8, 12), 3)
    except scheduler.AnchorNotSetError:
        pass
    else:
        raise AssertionError("expected AnchorNotSetError for explicit week without anchor")


# --- expand_occurrences: classes hidden during recess ---------------------

def _weekly_block(day="WED", pattern="every"):
    return {
        "id": 1, "module_name": "SC2001", "class_type": "Lecture",
        "location": "LT1A", "start_time": "09:30", "end_time": "10:20",
        "day_of_week": day, "specific_date": None, "week_pattern": pattern,
    }


def test_every_block_hidden_during_recess():
    recess = frozenset({7})
    wed_wk7 = _monday_of_continuous(7) + (date(2026, 8, 12) - date(2026, 8, 10))
    wed_wk6 = _monday_of_continuous(6) + (date(2026, 8, 12) - date(2026, 8, 10))
    # Recess week -> nothing.
    assert scheduler.expand_occurrences(
        [_weekly_block()], wed_wk7.isoformat(), wed_wk7.isoformat(), ANCHOR, recess
    ) == []
    # Ordinary teaching week -> the class shows.
    got = scheduler.expand_occurrences(
        [_weekly_block()], wed_wk6.isoformat(), wed_wk6.isoformat(), ANCHOR, recess
    )
    assert len(got) == 1 and got[0]["occurrence_date"] == wed_wk6.isoformat()


def test_every_block_hidden_after_semester_end():
    # 'every' is bounded to the 13 official teaching weeks even with no recess.
    wed_wk14 = _monday_of_continuous(14) + (date(2026, 8, 12) - date(2026, 8, 10))
    assert scheduler.expand_occurrences(
        [_weekly_block()], wed_wk14.isoformat(), wed_wk14.isoformat(), ANCHOR
    ) == []


def test_odd_even_pattern_uses_official_weeks():
    recess = frozenset({7})
    block = _weekly_block(pattern="odd")
    # Continuous week 8 = official week 7 (odd) -> shows despite even continuous #.
    wed_c8 = _monday_of_continuous(8) + (date(2026, 8, 12) - date(2026, 8, 10))
    got = scheduler.expand_occurrences(
        [block], wed_c8.isoformat(), wed_c8.isoformat(), ANCHOR, recess
    )
    assert len(got) == 1, "odd/even must match the OFFICIAL week number, not continuous"


def test_no_anchor_every_shows_non_every_raises():
    wed = date(2026, 8, 12)
    # 'every' with no anchor -> still shows (nothing to bound against).
    assert len(scheduler.expand_occurrences(
        [_weekly_block()], wed.isoformat(), wed.isoformat(), None
    )) == 1
    # odd/even with no anchor -> unresolvable.
    try:
        scheduler.expand_occurrences(
            [_weekly_block(pattern="odd")], wed.isoformat(), wed.isoformat(), None
        )
    except scheduler.AnchorNotSetError:
        pass
    else:
        raise AssertionError("expected AnchorNotSetError for odd pattern without anchor")


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception:
            failed += 1
            print(f"FAIL: {t.__name__}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    raise SystemExit(1 if failed else 0)
