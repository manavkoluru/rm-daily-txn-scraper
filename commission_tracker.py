"""Meghana commission tracker (account R152545 / Sujatha).

Rules:
  - Commission starts 2026-11-07: one-time bonus $250 (paid with week 1) + $50/week for 26 weeks.
  - D = total commission given so far (bonus + weeks due). Deducted from remaining, hypothetically.
  - V = scraper remaining − D
  - T = D
  - V > T            → RETURNS : sister receives weekly returns normally
  - 0 < V <= T       → HOLD    : user takes sister's weekly returns
  - V <= 0           → ZERO    : alert user to take the balance; waits for top-up
  - Top-up = remaining rises by more than TOPUP_THRESHOLD_USD between runs.
  - Top-up during HOLD (old pool still > 0): HOLD continues until the old pool
    (remaining − carry_topup) is cleared. Then normal rules resume.

State (commission_state.json, committed by the workflow):
  last_remaining, last_phase, carry (bool), carry_topup (float), topups [{date, amount}]
"""

import json
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ACCOUNT = "R152545"
BOT = "rm_daily_txns_manav_bot"
START = date(2026, 11, 7)
BONUS_USD = 250.0
WEEKLY_USD = 50.0
WEEKLY_INR_PAID = 4450.0      # what is actually paid per week
BONUS_INR_PAID = 22000.0      # what was actually paid as bonus
WEEKS_TOTAL = 26
TOPUP_THRESHOLD_USD = 500.0
RATE_91 = 91
TAX_KEEP = 0.93
IST = ZoneInfo("Asia/Kolkata")

_BASE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(_BASE, "commission_state.json")


def today_ist() -> date:
    return datetime.now(IST).date()


def usd_to_inr(usd: float) -> float:
    return round(usd * RATE_91 * TAX_KEEP, 2)


def weeks_due(today: date) -> int:
    if today < START:
        return 0
    return min(WEEKS_TOTAL, (today - START).days // 7 + 1)


def commission_paid(today: date) -> float:
    """Total commission given so far in USD: bonus + weekly payments due."""
    if today < START:
        return 0.0
    return BONUS_USD + WEEKLY_USD * weeks_due(today)


def phase(v: float, t: float) -> str:
    if v <= 0:
        return "ZERO"
    if v <= t:
        return "HOLD"
    return "RETURNS"


def project_hold_date(remaining: float, daily_credit: float, today: date):
    """First day V <= T. Returns (date, days) or (None, None)."""
    if remaining - commission_paid(today) <= commission_paid(today):
        return today, 0
    if daily_credit <= 0:
        return None, None
    rem = remaining
    for d in range(1, 3651):
        day = today + timedelta(days=d)
        rem -= daily_credit
        t = commission_paid(day)
        if rem - t <= t:
            return day, d
    return None, None


def project_zero_date(remaining: float, daily_credit: float, today: date):
    """First day V <= 0. Returns (date, days) or (None, None)."""
    if remaining - commission_paid(today) <= 0:
        return today, 0
    if daily_credit <= 0:
        return None, None
    rem = remaining
    for d in range(1, 3651):
        day = today + timedelta(days=d)
        rem -= daily_credit
        if rem - commission_paid(day) <= 0:
            return day, d
    return None, None


def default_state() -> dict:
    return {"last_remaining": None, "last_phase": None, "carry": False, "carry_topup": 0.0, "topups": []}


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return default_state()
    with open(STATE_PATH, "r") as f:
        state = json.load(f)
    return {**default_state(), **state}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def build_report(remaining: float, daily_credit: float, today: date, state: dict) -> tuple[str, dict]:
    """Returns (telegram_message, updated_state)."""
    prev_phase = state.get("last_phase")
    last = state.get("last_remaining")

    if last is not None and remaining - last > TOPUP_THRESHOLD_USD:
        amount = round(remaining - last, 2)
        state.setdefault("topups", []).append({"date": today.isoformat(), "amount": amount})
        if prev_phase == "HOLD" or state.get("carry"):
            state["carry"] = True
            state["carry_topup"] = round(state.get("carry_topup", 0.0) + amount, 2)
    state["last_remaining"] = remaining

    wd = weeks_due(today)
    paid = commission_paid(today)
    v = remaining - paid

    old_pool = remaining - state.get("carry_topup", 0.0)
    if state.get("carry") and old_pool <= 0:
        state["carry"] = False
        state["carry_topup"] = 0.0
    carry = bool(state.get("carry"))

    cur_phase = "HOLD" if carry else phase(v, paid)
    state["last_phase"] = cur_phase

    phase_line = {
        "RETURNS": "✅ *RETURNS* — Meghana receives weekly returns",
        "HOLD": "🛑 *HOLD* — take Meghana's weekly returns",
        "ZERO": "🚨 *ZERO* — remaining cleared. Take the balance. Waiting for top-up",
    }[cur_phase]

    lines = [
        f"💼 *Commission Status — Sujatha ({ACCOUNT})*",
        f"_{today.strftime('%d %b %Y')}_",
        "",
        f"📅 *Weeks due:* {wd} of {WEEKS_TOTAL}",
        f"🎁 *One-time bonus:* ${BONUS_USD:,.0f} (paid ₹{BONUS_INR_PAID:,.0f}) — paid 07 Nov 2026",
        f"💵 *Weekly commission:* ${WEEKLY_USD:,.0f} (paid ₹{WEEKLY_INR_PAID:,.0f})",
        f"🧾 *Total commission given:* ${paid:,.2f} (≈ ₹{usd_to_inr(paid):,.2f})",
        "",
        f"🏦 *Scraper remaining:* ${remaining:,.2f} (≈ ₹{usd_to_inr(remaining):,.2f})",
        f"📉 *Daily credit:* ${daily_credit:,.2f}",
        f"⚖️ *Remaining after commission (V):* ${v:,.2f}",
        "",
        phase_line,
    ]

    if carry:
        clear_day, clear_in = project_zero_date(old_pool, daily_credit, today)
        lines.append(f"🔒 Old pool ${old_pool:,.2f} must clear before returns resume")
        if clear_day and clear_in:
            lines.append(f"⏭️ Old pool clears: ~{clear_day.strftime('%d %b %Y')} (~{clear_in} days)")
    elif cur_phase == "RETURNS":
        hold_day, hold_in = project_hold_date(remaining, daily_credit, today)
        if hold_day and hold_in:
            lines.append(
                f"⏭️ *Stop giving returns from:* {hold_day.strftime('%d %b %Y')} "
                f"(~{hold_in} days) — remaining reaches commission given"
            )
    elif cur_phase == "HOLD":
        zero_day, zero_in = project_zero_date(remaining, daily_credit, today)
        if zero_day and zero_in:
            lines.append(f"⏭️ *Remaining hits zero:* ~{zero_day.strftime('%d %b %Y')} (~{zero_in} days)")
    else:
        lines.append("👉 Top-up of $1,000 expected. Returns resume once it is above commission given.")

    if wd >= WEEKS_TOTAL:
        lines.append("ℹ️ All 26 weekly payments done. No further commission added.")

    topups = state.get("topups", [])
    if topups:
        lines.append("")
        lines.append("🔁 *Top-ups recorded:*")
        for t in topups[-5:]:
            lines.append(f"  • {t['date']}: +${t['amount']:,.2f}")

    return "\n".join(lines), state
