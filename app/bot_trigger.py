"""
Telegram → GitHub Actions trigger bot.

Polls TWO bots simultaneously:
  1. @rm_daily_txns_manav_bot    — daily scraper commands only
  2. @rm_withdraw_saturday_manav_bot — all commands (daily + withdrawal)

Run:  python3 app/bot_trigger.py
Deploy to Railway for 24/7 operation (NOT Vercel — needs persistent process).

─────────────────────────────────────────────────────────
ENV VARS REQUIRED
─────────────────────────────────────────────────────────
  TELEGRAM_BOT_TOKEN_MANAV          — daily bot token
  TELEGRAM_BOT_TOKEN_WITHDRAW_MANAV — withdrawal bot token
  TELEGRAM_CHAT_ID_MANAV            — authorised chat ID (same for both)
  GITHUB_PAT                        — GitHub PAT with 'workflow' scope
  GITHUB_OWNER                      — GitHub username / org
  GITHUB_REPO                       — repo name (default: rm-daily-txn-scraper)
─────────────────────────────────────────────────────────
"""

import os
import sys
import time
from datetime import datetime

import requests

# ── Config ────────────────────────────────────────────────────────────────────

DAILY_BOT_TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN_MANAV", "")
WITHDRAW_BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN_WITHDRAW_MANAV", "")
VASU_BOT_TOKEN         = os.getenv("TELEGRAM_BOT_TOKEN_VASU", "")
ALLOWED_CHAT           = os.getenv("TELEGRAM_CHAT_ID_MANAV", "")
ALLOWED_CHAT_VASU      = os.getenv("TELEGRAM_CHAT_ID_VASU", "")
GITHUB_PAT         = os.getenv("GITHUB_PAT", "")
GITHUB_OWNER       = os.getenv("GITHUB_OWNER", "")
GITHUB_REPO        = os.getenv("GITHUB_REPO", "rm-daily-txn-scraper")
WORKFLOW_FILE      = "daily_scrape.yml"
BRANCH             = "main"

GITHUB_API    = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
KNOWN_GROUPS  = ["manav", "ranjitha", "pavana", "poornima", "others", "vasu"]

# ── Help texts ────────────────────────────────────────────────────────────────

DAILY_HELP = """
🤖 *RM Daily Bot — Commands*

*📊 Daily scraper:*
`/daily`              — All accounts
`/daily manav`        — Manav's accounts only
`/daily ranjitha`     — Ranjitha's accounts only
`/daily pavana`       — Pavana's accounts only
`/daily poornima`     — Poornima's accounts only
`/daily others`       — Others' accounts only

*ℹ️ Info:*
`/status`             — Last 5 GitHub Actions runs
`/help`               — Show this message
""".strip()

WITHDRAW_HELP = """
🤖 *RM Master Bot — Commands*

*📊 Daily scraper:*
`/daily`              — All accounts
`/daily manav`        — Manav's accounts only
`/daily ranjitha`     — Ranjitha's accounts only
`/daily pavana`       — Pavana's accounts only
`/daily poornima`     — Poornima's accounts only
`/daily others`       — Others' accounts only

*💸 Withdrawals:*
`/withdraw_all`       — All accounts
`/withdraw manav`     — Manav's accounts
`/withdraw ranjitha`  — Ranjitha's accounts
`/withdraw pavana`    — Pavana's accounts
`/withdraw poornima`  — Poornima's accounts
`/withdraw others`    — Others' accounts
`/withdraw R553232`   — Single account by R\\-ID

*ℹ️ Info:*
`/status`             — Last 5 GitHub Actions runs
`/help`               — Show this message
""".strip()


# ── Telegram helpers ──────────────────────────────────────────────────────────

def tg_send(token: str, chat_id: str, text: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"[WARN] Telegram send failed: {e}")


def tg_get_updates(token: str, offset: int) -> list:
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": offset, "timeout": 25, "allowed_updates": ["message"]},
            timeout=35,
        )
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        print(f"[WARN] getUpdates error: {e}")
        return []


# ── GitHub Actions ────────────────────────────────────────────────────────────

def gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def trigger_workflow(job: str, account: str = "", group: str = "", bot: str = "") -> tuple[bool, str]:
    inputs = {"job": job}
    if account: inputs["account"] = account
    if group:   inputs["group"]   = group
    if bot:     inputs["bot"]     = bot
    url = f"{GITHUB_API}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    try:
        r = requests.post(url, headers=gh_headers(),
                          json={"ref": BRANCH, "inputs": inputs}, timeout=15)
        return (True, "ok") if r.status_code == 204 else (False, f"GitHub {r.status_code}: {r.text[:200]}")
    except Exception as e:
        return False, str(e)


def get_status() -> str:
    try:
        r = requests.get(f"{GITHUB_API}/actions/workflows/{WORKFLOW_FILE}/runs",
                         headers=gh_headers(), params={"per_page": 5}, timeout=10)
        r.raise_for_status()
        runs = r.json().get("workflow_runs", [])
        if not runs:
            return "No runs found."
        lines = ["*Last 5 workflow runs:*\n"]
        for run in runs:
            conclusion = run.get("conclusion") or "in_progress"
            event      = run.get("event", "?")
            created    = run["created_at"][:16].replace("T", " ")
            icon = {"success": "✅", "failure": "❌", "in_progress": "🔄", "cancelled": "⏹"}.get(conclusion, "❓")
            lines.append(f"{icon} `{created}` — {conclusion} ({event})")
        return "\n".join(lines)
    except Exception as e:
        return f"Could not fetch status: {e}"


# ── Command handlers ──────────────────────────────────────────────────────────

def handle_daily_commands(token: str, chat_id: str, text: str) -> None:
    """Handles commands for the daily bot — scraper only, no withdrawals."""
    now = datetime.now().strftime("%d %b %Y %H:%M IST")

    if text.startswith("/help") or text == "/start":
        tg_send(token, chat_id, DAILY_HELP)

    elif text.startswith("/daily"):
        parts = text.split()
        arg   = parts[1].lower() if len(parts) > 1 else ""

        if arg and arg in KNOWN_GROUPS:
            # Group: /daily manav
            label = f"*{arg.capitalize()}* group"
            tg_send(token, chat_id, f"⏳ Running daily scraper for {label}…")
            ok, err = trigger_workflow("scraper", bot=arg)
            if ok:
                tg_send(token, chat_id,
                    f"✅ *Scraper started for {label}!*\n"
                    f"Results with balances, E-Wallet & INR totals will arrive in the group's chat.\n"
                    f"_Triggered at {now}_"
                )
            else:
                tg_send(token, chat_id, f"❌ Failed:\n`{err}`")

        elif arg and arg.upper().startswith("R") and arg[1:].isdigit():
            # Single account: /daily R553232
            account = arg.upper()
            tg_send(token, chat_id, f"⏳ Running daily scraper for *{account}*…")
            ok, err = trigger_workflow("scraper", account=account)
            if ok:
                tg_send(token, chat_id,
                    f"✅ *Scraper started for {account}!*\n"
                    f"_Triggered at {now}_"
                )
            else:
                tg_send(token, chat_id, f"❌ Failed:\n`{err}`")

        elif not arg:
            # All accounts: /daily
            tg_send(token, chat_id, "⏳ Running daily scraper for *all accounts*…")
            ok, err = trigger_workflow("scraper")
            if ok:
                tg_send(token, chat_id,
                    f"✅ *Scraper started for all accounts!*\n"
                    f"Results with balances, E-Wallet & INR totals will arrive in each group's chat.\n"
                    f"_Triggered at {now}_"
                )
            else:
                tg_send(token, chat_id, f"❌ Failed:\n`{err}`")

        else:
            groups_str = " | ".join(KNOWN_GROUPS)
            tg_send(token, chat_id,
                f"❌ Unknown argument: `{arg}`\n"
                f"Groups: `{groups_str}`\n"
                f"Single account: `/daily R553232`\n"
                f"All: `/daily`"
            )

    elif text.startswith("/status"):
        tg_send(token, chat_id, "⏳ Fetching status…")
        tg_send(token, chat_id, get_status())

    elif text.startswith("/withdraw"):
        tg_send(token, chat_id,
            "⚠️ Withdrawal commands are not available here.\n"
            "Use @rm\\_withdraw\\_saturday\\_manav\\_bot for withdrawals."
        )

    elif text.startswith("/"):
        tg_send(token, chat_id, "❓ Unknown command. Send /help for all commands.")


def handle_all_commands(token: str, chat_id: str, text: str) -> None:
    """Handles all commands for the withdrawal bot — daily + withdrawals."""
    now = datetime.now().strftime("%d %b %Y %H:%M IST")

    if text.startswith("/help") or text == "/start":
        tg_send(token, chat_id, WITHDRAW_HELP)

    elif text.startswith("/daily"):
        parts = text.split()
        arg   = parts[1].lower() if len(parts) > 1 else ""

        if arg and arg in KNOWN_GROUPS:
            # Group: /daily manav
            label = f"*{arg.capitalize()}* group"
            tg_send(token, chat_id, f"⏳ Running daily scraper for {label}…")
            ok, err = trigger_workflow("scraper", bot=arg)
            if ok:
                tg_send(token, chat_id,
                    f"✅ *Scraper started for {label}!*\n"
                    f"Results will arrive in the group's chat.\n"
                    f"_Triggered at {now}_"
                )
            else:
                tg_send(token, chat_id, f"❌ Failed:\n`{err}`")

        elif arg and arg.upper().startswith("R") and arg[1:].isdigit():
            # Single account: /daily R553232
            account = arg.upper()
            tg_send(token, chat_id, f"⏳ Running daily scraper for *{account}*…")
            ok, err = trigger_workflow("scraper", account=account)
            if ok:
                tg_send(token, chat_id,
                    f"✅ *Scraper started for {account}!*\n"
                    f"_Triggered at {now}_"
                )
            else:
                tg_send(token, chat_id, f"❌ Failed:\n`{err}`")

        elif not arg:
            # All accounts: /daily
            tg_send(token, chat_id, "⏳ Running daily scraper for *all accounts*…")
            ok, err = trigger_workflow("scraper")
            if ok:
                tg_send(token, chat_id,
                    f"✅ *Scraper started for all accounts!*\n"
                    f"Results will arrive in each group's chat.\n"
                    f"_Triggered at {now}_"
                )
            else:
                tg_send(token, chat_id, f"❌ Failed:\n`{err}`")

        else:
            groups_str = " | ".join(KNOWN_GROUPS)
            tg_send(token, chat_id,
                f"❌ Unknown argument: `{arg}`\n"
                f"Groups: `{groups_str}`\n"
                f"Single account: `/daily R553232`\n"
                f"All: `/daily`"
            )

    elif text.startswith("/withdraw_all"):
        tg_send(token, chat_id, "⏳ Triggering withdrawal for *all accounts*…")
        ok, err = trigger_workflow("withdrawal")
        if ok:
            tg_send(token, chat_id,
                f"✅ *Withdrawal started for all accounts!*\n"
                f"Results will arrive here once done.\n"
                f"_Triggered at {now}_"
            )
        else:
            tg_send(token, chat_id, f"❌ Failed:\n`{err}`")

    elif text.startswith("/withdraw "):
        parts = text.split()
        arg   = parts[1].lower() if len(parts) > 1 else ""

        if arg in KNOWN_GROUPS:
            tg_send(token, chat_id, f"⏳ Triggering withdrawal for *{arg.capitalize()}* group…")
            ok, err = trigger_workflow("withdrawal", group=arg)
            if ok:
                tg_send(token, chat_id,
                    f"✅ *Withdrawal started for {arg.capitalize()} group!*\n"
                    f"_Triggered at {now}_"
                )
            else:
                tg_send(token, chat_id, f"❌ Failed:\n`{err}`")

        elif arg.upper().startswith("R") and arg[1:].isdigit():
            account = arg.upper()
            tg_send(token, chat_id, f"⏳ Triggering withdrawal for *{account}*…")
            ok, err = trigger_workflow("withdrawal", account=account)
            if ok:
                tg_send(token, chat_id,
                    f"✅ *Withdrawal started for {account}!*\n"
                    f"_Triggered at {now}_"
                )
            else:
                tg_send(token, chat_id, f"❌ Failed:\n`{err}`")

        else:
            tg_send(token, chat_id,
                f"❌ Unknown: `{arg}`\n"
                f"Groups: `{'` | `'.join(KNOWN_GROUPS)}`\n"
                f"Single account: `/withdraw R553232`\n"
                f"All: `/withdraw_all`"
            )

    elif text.startswith("/status"):
        tg_send(token, chat_id, "⏳ Fetching status…")
        tg_send(token, chat_id, get_status())

    elif text.startswith("/"):
        tg_send(token, chat_id, "❓ Unknown command. Send /help for all commands.")


# ── Main loop — polls both bots ───────────────────────────────────────────────

def validate_env() -> bool:
    missing = []
    if not DAILY_BOT_TOKEN:       missing.append("TELEGRAM_BOT_TOKEN_MANAV")
    if not WITHDRAW_BOT_TOKEN:    missing.append("TELEGRAM_BOT_TOKEN_WITHDRAW_MANAV")
    if not ALLOWED_CHAT:          missing.append("TELEGRAM_CHAT_ID_MANAV")
    if not VASU_BOT_TOKEN:        missing.append("TELEGRAM_BOT_TOKEN_VASU")
    if not ALLOWED_CHAT_VASU:     missing.append("TELEGRAM_CHAT_ID_VASU")
    if not GITHUB_PAT:            missing.append("GITHUB_PAT")
    if not GITHUB_OWNER:          missing.append("GITHUB_OWNER")
    if missing:
        print("❌ Missing env vars:")
        for m in missing: print(f"   export {m}=<value>")
        return False
    return True


def main():
    if not validate_env():
        sys.exit(1)

    W = 60
    print("=" * W)
    print("  RM Trigger Bot — polling 3 bots")
    print("  ├─ @rm_daily_txns_manav_bot          → scraper only")
    print("  ├─ @rm_withdraw_saturday_manav_bot   → all commands (manav)")
    print("  └─ @rm_weekly_withdraw_vasu_bot      → all commands (vasu)")
    print(f"  Repo  : {GITHUB_OWNER}/{GITHUB_REPO}")
    print("  Press Ctrl+C to stop.")
    print("=" * W + "\n")

    now = datetime.now().strftime("%d %b %Y %H:%M IST")
    startup_msg = (
        f"🤖 *RM Trigger Bot is online*\n"
        f"_Started {now}_\n\n"
        f"Send /help to see available commands."
    )
    tg_send(DAILY_BOT_TOKEN,       ALLOWED_CHAT, startup_msg)
    tg_send(WITHDRAW_BOT_TOKEN,    ALLOWED_CHAT, startup_msg)
    tg_send(VASU_BOT_TOKEN,        ALLOWED_CHAT_VASU, startup_msg)

    daily_offset       = 0
    withdraw_offset    = 0
    vasu_offset        = 0

    while True:
        try:
            # Poll daily bot — Manav
            for update in tg_get_updates(DAILY_BOT_TOKEN, daily_offset):
                daily_offset = update["update_id"] + 1
                msg     = update.get("message", {})
                text    = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if text and str(chat_id) == str(ALLOWED_CHAT):
                    print(f"  [daily_manav] {text!r}")
                    handle_daily_commands(DAILY_BOT_TOKEN, chat_id, text)
                elif text:
                    print(f"  [WARN] daily_manav — unauthorised chat: {chat_id}")

            # Poll withdrawal bot — Manav
            for update in tg_get_updates(WITHDRAW_BOT_TOKEN, withdraw_offset):
                withdraw_offset = update["update_id"] + 1
                msg     = update.get("message", {})
                text    = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if text and str(chat_id) == str(ALLOWED_CHAT):
                    print(f"  [withdraw_manav] {text!r}")
                    handle_all_commands(WITHDRAW_BOT_TOKEN, chat_id, text)
                elif text:
                    print(f"  [WARN] withdraw_manav — unauthorised chat: {chat_id}")

            # Poll Vasu bot — all commands
            for update in tg_get_updates(VASU_BOT_TOKEN, vasu_offset):
                vasu_offset = update["update_id"] + 1
                msg     = update.get("message", {})
                text    = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if text and str(chat_id) == str(ALLOWED_CHAT_VASU):
                    print(f"  [vasu_bot] {text!r}")
                    handle_all_commands(VASU_BOT_TOKEN, chat_id, text)
                elif text:
                    print(f"  [WARN] vasu_bot — unauthorised chat: {chat_id}")

        except KeyboardInterrupt:
            print("\n  Shutting down.")
            tg_send(DAILY_BOT_TOKEN,       ALLOWED_CHAT, "🔴 *RM Trigger Bot stopped.*")
            tg_send(WITHDRAW_BOT_TOKEN,    ALLOWED_CHAT, "🔴 *RM Trigger Bot stopped.*")
            tg_send(VASU_BOT_TOKEN,        ALLOWED_CHAT_VASU, "🔴 *RM Trigger Bot stopped.*")
            break
        except Exception as e:
            print(f"[ERROR] {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
