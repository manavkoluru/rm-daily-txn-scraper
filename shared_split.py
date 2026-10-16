"""
Shared-account split helpers (used by main.py and withdraw.py).

Config: shared_accounts.json
  - Daily credit: split_bot gets daily_share_usd per day; owner_bot gets the rest.
  - Weekly withdrawal (Saturday): split_bot gets weekly_share_usd; owner_bot gets the rest.
  - After split_days (counted from earliest activation date) split_bot share stops
    and owner_bot gets everything.
"""

import json
import os
from datetime import datetime

_BASE = os.path.dirname(os.path.abspath(__file__))
ACTIVATION_DATE_FORMATS = ["%d-%b-%Y", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d"]


def load_shared_accounts() -> dict:
    """Returns {R_ID: config} from shared_accounts.json. Skips '_comment' keys."""
    with open(os.path.join(_BASE, "shared_accounts.json")) as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def parse_activation_date(date_str: str):
    """Parses an activation date string such as '08-Sep-2026'. Returns None on failure."""
    for fmt in ACTIVATION_DATE_FORMATS:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


def earliest_activation_date(activations: list[dict]):
    """Earliest activation date from activation history. None if unavailable."""
    dates = [parse_activation_date(a.get("date", "")) for a in activations or []]
    dates = [d for d in dates if d]
    return min(dates) if dates else None


def split_status(start, cfg: dict, today: datetime = None) -> tuple[bool, int | None]:
    """Returns (split_active, days_elapsed).

    If the activation date is unknown, the split is assumed active (days_elapsed=None).
    """
    if start is None:
        return True, None
    today = today or datetime.now()
    days = max((today - start).days, 0)
    return days < cfg["split_days"], days


def daily_split(credit_usd: float, active: bool, cfg: dict) -> tuple[float, float]:
    """Returns (split_bot_usd, owner_bot_usd) for one day's credit."""
    if not active:
        return 0.0, credit_usd
    share = cfg["daily_share_usd"]  # fixed, irrespective of credit
    return share, max(credit_usd - share, 0.0)


def weekly_split(withdrawn_usd: float, active: bool, cfg: dict) -> tuple[float, float]:
    """Returns (split_bot_usd, owner_bot_usd) for a Saturday withdrawal."""
    if not active:
        return 0.0, withdrawn_usd
    share = cfg["weekly_share_usd"]  # fixed
    return share, max(withdrawn_usd - share, 0.0)
