"""Meghana commission tracker (Sujatha R152545, referral Meghana2 R256332).

All math is in USD. INR is display-only.

Rules:
  - Commission starts 2026-11-07: $250 bonus (paid ₹22,000) + $50/week (paid ₹4,450 each), 26 weeks.
  - D = commission given so far in USD = 250 + 50 × weeks_due.       T = D.
  - Referral drain: Meghana2 daily credit is deducted from Sujatha's pool each day
    while active, i.e. for 200 days from Meghana2's activation date (exact day 200 = stop).
  - Sujatha's own daily credit is already reflected in the scraper's remaining.
  - pool = scraper remaining − referral drain accrued so far.
  - V = pool − D
  - V > T            → RETURNS : sister receives weekly returns
  - 0 < V <= T       → HOLD    : user takes sister's weekly returns
  - V <= 0           → ZERO    : alert; take the balance; waits for top-up
  - Top-up = remaining rises by more than TOPUP_THRESHOLD_USD between runs.
    A top-up during HOLD keeps HOLD until the old pool (pool − carry_topup) is cleared.

State (commission_state.json, committed by the workflow):
  last_remaining, last_phase, last_run, referral_accrued, carry, carry_topup, topups
"""

import json
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ACCOUNT = "R152545"
REFERRAL_ACCOUNT = "R256332"
REFERRAL_DAILY_USD = 6.0      # fixed referral commission per day from Meghana2
REFERRAL_DAYS = 200
BOT = "rm_daily_txns_manav_bot"
START = date(2026, 11, 7)
BONUS_USD = 250.0
WEEKLY_USD = 50.0
BONUS_INR_PAID = 22000.0
WEEKLY_INR_PAID = 4450.0
WEEKS_TOTAL = 26
TOPUP_THRESHOLD_USD = 500.0
PROJECTION_DAYS = 3650
IST = ZoneInfo("Asia/Kolkata")

_BASE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(_BASE, "commission_state.json")


def today_ist() -> date:
    return datetime.now(IST).date()


def weeks_due(today: date) -> int:
    if today < START:
        return 0
    return min(WEEKS_TOTAL, (today - START).days // 7 + 1)


def commission_paid_usd(today: date) -> float:
    if today < START:
        return 0.0
    return BONUS_USD + WEEKLY_USD * weeks_due(today)


def commission_paid_inr(today: date) -> float:
    n = weeks_due(today)
    if n == 0:
        return 0.0
    return BONUS_INR_PAID + WEEKLY_INR_PAID * n


def referral_active(day: date, referral_start: date | None) -> bool:
    if referral_start is None:
        return False
    return referral_start <= day < referral_start + timedelta(days=REFERRAL_DAYS)


def daily_burn(day: date, sujatha_daily: float, referral_daily: float, referral_start: date | None) -> float:
    """USD deducted from Sujatha's pool on `day`."""
    burn = sujatha_daily
    if referral_active(day, referral_start):
        burn += referral_daily
    return burn


def next_saturday(d: date) -> date:
    """Action dates are reported on the first Saturday on/after the projected day (weekly cadence)."""
    return d + timedelta(days=(5 - d.weekday()) % 7)


def phase(v: float, t: float) -> str:
    if v <= 0:
        return "ZERO"
    if v <= t:
        return "HOLD"
    return "RETURNS"


def _project(pool: float, today: date, sujatha_daily: float, referral_daily: float,
             referral_start: date | None, stop_when) -> tuple[date | None, int | None]:
    """Walks forward day by day. stop_when(pool_after_day, day) -> bool. Returns (day, days_ahead)."""
    if stop_when(pool, today):
        return today, 0
    for d in range(1, PROJECTION_DAYS + 1):
        day = today + timedelta(days=d)
        pool -= daily_burn(day, sujatha_daily, referral_daily, referral_start)
        if stop_when(pool, day):
            return day, d
    return None, None


def project_hold(pool: float, today: date, sujatha_daily, referral_daily, referral_start):
    """First day V <= T (HOLD begins). V and T evaluated on each future day."""
    return _project(
        pool, today, sujatha_daily, referral_daily, referral_start,
        lambda p, day: (p - commission_paid_usd(day)) <= commission_paid_usd(day),
    )


def project_zero(pool: float, today: date, sujatha_daily, referral_daily, referral_start):
    """First day V <= 0 (balance fully taken)."""
    return _project(
        pool, today, sujatha_daily, referral_daily, referral_start,
        lambda p, day: (p - commission_paid_usd(day)) <= 0,
    )


def project_old_pool_clear(old_pool: float, today: date, sujatha_daily, referral_daily, referral_start):
    """First day the carried-over pool is exhausted."""
    return _project(
        old_pool, today, sujatha_daily, referral_daily, referral_start,
        lambda p, day: p <= 0,
    )


def accrue_referral(state: dict, today: date, referral_daily: float, referral_start: date | None) -> float:
    """Adds referral drain for days since last run (or since START on first run). Returns accrued total."""
    if referral_start is None or referral_daily <= 0:
        return state.get("referral_accrued", 0.0)
    last = state.get("last_run")
    day = date.fromisoformat(last) if last else START
    accrued = state.get("referral_accrued", 0.0)
    while day < today:
        if referral_active(day, referral_start):
            accrued += referral_daily
        day += timedelta(days=1)
    return round(accrued, 2)


def default_state() -> dict:
    return {
        "last_remaining": None, "last_phase": None, "last_run": None,
        "referral_accrued": 0.0, "carry": False, "carry_topup": 0.0, "topups": [],
    }


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


def build_report(remaining: float, sujatha_daily: float, referral_daily: float,
                 referral_start: date | None, today: date, state: dict) -> tuple[str, dict]:
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

    accrued = accrue_referral(state, today, referral_daily, referral_start)
    state["referral_accrued"] = accrued
    state["last_run"] = today.isoformat()

    pool = remaining - accrued
    paid = commission_paid_usd(today)
    v = pool - paid

    old_pool = pool - state.get("carry_topup", 0.0)
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

    ref_on = referral_active(today, referral_start)
    burn_today = sujatha_daily + (referral_daily if ref_on else 0.0)
    ref_end = (referral_start + timedelta(days=REFERRAL_DAYS)) if referral_start else None

    lines = [
        f"💼 *Commission Status — Sujatha ({ACCOUNT})*",
        f"_{today.strftime('%d %b %Y')}_",
        "",
        f"📅 *Weeks due:* {weeks_due(today)} of {WEEKS_TOTAL}",
        f"🧾 *Total commission given:* ₹{commission_paid_inr(today):,.0f} (${paid:,.2f})",
        "",
        f"🏦 *Scraper remaining:* ${remaining:,.2f}",
        f"📉 *Daily burn:* ${burn_today:,.2f} (Sujatha ${sujatha_daily:,.2f}"
        + (f" + Meghana2 ${referral_daily:,.2f})" if ref_on else ")"),
        f"🔗 *Referral drain accrued:* ${accrued:,.2f}",
        f"⚖️ *Remaining after commission (V):* ${v:,.2f}",
        "",
        phase_line,
    ]

    if carry:
        clear_day, clear_in = project_old_pool_clear(old_pool, today, sujatha_daily, referral_daily, referral_start)
        lines.append(f"🔒 Old pool ${old_pool:,.2f} must clear before returns resume")
        if clear_day is not None:
            lines.append(
                f"⏭️ Old pool clears: {next_saturday(clear_day).strftime('%d %b %Y')} (Sat) "
                f"— projected {clear_day.strftime('%d %b')}, ~{clear_in} days"
            )
    elif cur_phase == "RETURNS":
        hold_day, hold_in = project_hold(pool, today, sujatha_daily, referral_daily, referral_start)
        if hold_day is not None:
            lines.append(
                f"⏭️ *Stop giving returns from:* {next_saturday(hold_day).strftime('%d %b %Y')} (Sat) "
                f"— projected {hold_day.strftime('%d %b')}, ~{hold_in} days; remaining reaches commission given"
            )
    elif cur_phase == "HOLD":
        zero_day, zero_in = project_zero(pool, today, sujatha_daily, referral_daily, referral_start)
        if zero_day is not None:
            lines.append(
                f"⏭️ *Remaining hits zero:* {next_saturday(zero_day).strftime('%d %b %Y')} (Sat) "
                f"— projected {zero_day.strftime('%d %b')}, ~{zero_in} days"
            )
    else:
        lines.append("👉 Top-up of $1,000 expected. Returns resume once remaining is above commission given.")

    if ref_end and today < ref_end:
        lines.append(f"ℹ️ Meghana2 referral drain ends {ref_end.strftime('%d %b %Y')} (day {REFERRAL_DAYS})")
    if weeks_due(today) >= WEEKS_TOTAL:
        lines.append("ℹ️ All 26 weekly payments done. No further commission added.")

    topups = state.get("topups", [])
    if topups:
        lines.append("")
        lines.append("🔁 *Top-ups recorded:*")
        for t in topups[-5:]:
            lines.append(f"  • {t['date']}: +${t['amount']:,.2f}")

    return "\n".join(lines), state
