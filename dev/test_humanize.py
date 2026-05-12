"""Unit test offline per humanize.py (no Playwright, no network).

Run con: .venv/Scripts/python.exe dev/test_humanize.py

Test:
- _bezier_curve_points: numero punti, monotonicita' approssimata
- random_session_duration_min: in range plausibile
- is_active_hour: rispetta 9-22
- random_gap_between_dms_min: in range 8-30
"""
import sys
from pathlib import Path

# Import diretto del modulo (no install)
sys.path.insert(0, str(Path(__file__).parent / "social_outreach"))
import humanize  # noqa: E402


def test_bezier_curve_points():
    pts = humanize._bezier_curve_points((0, 0), (100, 200), steps=25)
    assert len(pts) == 26, f"steps=25 → 26 punti, ottenuti {len(pts)}"
    assert pts[0] == (0, 0), f"primo punto deve essere start, ottenuto {pts[0]}"
    # ultimo punto deve essere ~end (Bezier passa esattamente da start ed end)
    assert abs(pts[-1][0] - 100) < 0.001, f"ultimo x ~100, got {pts[-1][0]}"
    assert abs(pts[-1][1] - 200) < 0.001, f"ultimo y ~200, got {pts[-1][1]}"
    print(f"  ✓ bezier 26 pts, start={pts[0]}, end={pts[-1]}")


def test_random_session_duration():
    durations = [humanize.random_session_duration_min() for _ in range(100)]
    avg = sum(durations) / len(durations)
    assert 5 < avg < 40, f"average duration plausibile (5-40 min), got {avg:.1f}"
    assert all(0 < d < 120 for d in durations), "tutte le durate < 2h"
    print(f"  ✓ random_session_duration_min avg={avg:.1f} min over 100 samples")


def test_is_active_hour():
    # Test esplicito per ore note
    assert humanize.is_active_hour(9), "09:00 deve essere active"
    assert humanize.is_active_hour(15), "15:00 deve essere active"
    assert humanize.is_active_hour(21), "21:00 deve essere active"
    assert not humanize.is_active_hour(8), "08:00 NON deve essere active"
    assert not humanize.is_active_hour(22), "22:00 NON deve essere active"
    assert not humanize.is_active_hour(3), "03:00 NON deve essere active"
    print("  ✓ is_active_hour 9-22 ok")


def test_random_gap():
    gaps = [humanize.random_gap_between_dms_min() for _ in range(50)]
    assert all(8 <= g <= 30 for g in gaps), "tutti i gap in [8, 30]"
    avg = sum(gaps) / len(gaps)
    assert 15 < avg < 25, f"avg ~19 min atteso, got {avg:.1f}"
    print(f"  ✓ random_gap_between_dms_min avg={avg:.1f}, all in [8,30]")


def main():
    print("=== humanize.py unit tests ===")
    tests = [
        test_bezier_curve_points,
        test_random_session_duration,
        test_is_active_hour,
        test_random_gap,
    ]
    n_pass = 0
    for t in tests:
        try:
            t()
            n_pass += 1
        except AssertionError as e:
            print(f"  ✗ {t.__name__}: {e}")
        except Exception as e:
            print(f"  ! {t.__name__}: {type(e).__name__}: {e}")
    print(f"\nResult: {n_pass}/{len(tests)} passed")
    return 0 if n_pass == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
