"""
Per-account conversion rate eligibility (91 vs 97).

Rule:
  - Accounts listed in user_rate_mapping.json "rate_91" start on 91.
  - Once an account has a top-up (2+ activation rows), it moves to 97.
  - Accounts not in the list are always 97.
  - If the activation history could not be read (empty list), keep the static 91.
"""

import logging

logger = logging.getLogger(__name__)


def resolve_is_91(username: str, rate_91_set: set, activations: list[dict] | None) -> bool:
    """Returns True if the account should be converted at 91 rs, else False (97 rs)."""
    if username not in rate_91_set:
        return False
    if not activations:
        logger.warning("No activation history for %s; keeping static 91 rate", username)
        return True
    return len(activations) <= 1
