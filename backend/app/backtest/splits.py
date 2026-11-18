"""Chronological splits and walk-forward windows (pure).

Windows are inclusive date ranges over the trading calendar, never overlapping. Parameters
are chosen on development/validation (or each walk-forward train window) only; the holdout is
evaluated once, after selection (see runner).
"""
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Window:
    name: str
    start: date
    end: date
    days: int

    def to_dict(self) -> dict:
        return {"name": self.name, "start": self.start.isoformat(), "end": self.end.isoformat(),
                "days": self.days}


def _window(name: str, dates: list[date]) -> Window:
    return Window(name, dates[0], dates[-1], len(dates))


def chronological_split(dates: list[date], dev_frac: float = 0.6, val_frac: float = 0.2
                        ) -> dict[str, Window]:
    if not (0 < dev_frac and 0 < val_frac and dev_frac + val_frac < 1):
        raise ValueError("need 0 < dev_frac, val_frac and dev_frac + val_frac < 1")
    dates = sorted(set(dates))
    n = len(dates)
    a, b = int(n * dev_frac), int(n * (dev_frac + val_frac))
    if a < 1 or b - a < 1 or n - b < 1:
        raise ValueError(f"not enough dates ({n}) for development/validation/holdout windows")
    return {
        "development": _window("development", dates[:a]),
        "validation": _window("validation", dates[a:b]),
        "holdout": _window("holdout", dates[b:]),
    }


def walk_forward_windows(dates: list[date], train_days: int, test_days: int,
                         step: int | None = None) -> list[tuple[Window, Window]]:
    """Rolling (train, test) pairs; test windows advance by `step` (default test_days), so with
    the default they never overlap and every test window is strictly after its train window."""
    dates = sorted(set(dates))
    step = step or test_days
    out = []
    i = 0
    while i + train_days + test_days <= len(dates):
        out.append((_window(f"train_{len(out)}", dates[i:i + train_days]),
                    _window(f"test_{len(out)}", dates[i + train_days:i + train_days + test_days])))
        i += step
    return out


def slice_dates(dates: list[date], window: Window) -> list[date]:
    return [d for d in dates if window.start <= d <= window.end]
