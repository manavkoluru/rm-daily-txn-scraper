"""
Telegram → GitHub Actions trigger bot.

Listens for commands in the withdrawal bot's chat and fires GitHub Actions
workflow_dispatch events. Run this on any machine (your Mac, a VPS, etc.)
while you want manual control.

─────────────────────────────────────────────────────────
SETUP (one-time)
─────────────────────────────────────────────────────────
1. Create a GitHub Personal Access Token (PAT):
   → https://github.com/settings/tokens/new
   → Scopes: check ✅  "workflow"
   → Copy the token

2. Set environment variables (add to ~/.zshrc or ~/.bash_profile):

   export TELEGRAM_BOT_TOKEN_WITHDRAW_MANAV="<your_withdrawal_bot_token>"
   export TELEGRAM_CHAT_ID_MANAV="<your_chat_id>"
   export GITHUB_PAT="<your_github_pat>"
   export GITHUB_OWNER="<your_github_username_or_org>"
   export GITHUB_REPO="rm-daily-txn-scraper"

3. Run:
   python3 bot_trigger.py

─────────────────────────────────────────────────────────
TELEGRAM COMMANDS (send in the bot's chat)
─────────────────────────────────────────────────────────
  /help                  — show all commands
  /withdraw_all          — run withdrawal for ALL accounts now
  /withdraw R553232      — run withdrawal for one specific account
  /scrape                — run the daily scraper now
  /status                — show last workflow run status
─────────────────────────────────────────────────────────
"""

import json
import os
import sys
import time
from datetime import datetime

import requests

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN_WITHDRAW_MANAV", "")
ALLOWED_CHAT = os.getenv("TELEGRAM_CHAT_ID_MANAV", "")       # only this chat can send commands
GITHUB_PAT   = os.getenv("GITHUB_PAT", "")
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "")
GITHUB_REPO  = os.getenv("GITHUB_REPO", "rm-daily-txn-scraper")
WORKFLOW_FILE = "daily_scrape.yml"
BRANCH        = "main"

TELEGRAM_API  = f"https://api.telegram.org/bot{BOT_TOKEN}"
GITHUB_API    = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"

HELP_TEXT = """
🤖 *RM Withdrawal Bot — Commands*

/withdraw\\_all
  Run withdrawal for *all accounts* now

/withdraw R553232
  Run withdrawal for *one account* (replace R-ID)

/scrape
  Run the daily account scraper now

/status
  Show last GitHub Actions run status

/help
  Show this message
""".strip()


# ── Telegram helpers ──────────────────────────────────────────────────────────

def tg_send(chat_id: str, text: str, parse_mode: str = "Markdown") -> None:
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
    except Exception as e:
        print(f"[WARN] Failed to send Telegram message: {e}")


def tg_get_updates(offset: int) -> list:
    try:
        r = requests.get(
            f"{TELEGRAM_API}/getUpdates",
            params={"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
            timeout=40,
        )
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        print(f"[WARN] getUpdates error: {e}")
        return []


# ── GitHub Actions helpers ────────────────────────────────────────────────────

def gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def trigger_workflow(job: str, account: str = "") -> tuple[bool, str]:
    """
    Fires a workflow_dispatch event.
    Returns (success, message).
    """
    inputs = {"job": job}
    if account:
        inputs["account"] = account

    url = f"{GITHUB_API}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    try:
        r = requests.post(
            url,
            headers=gh_headers(),
            json={"ref": BRANCH, "inputs": inputs},
            timeout=15,
        )
        if r.status_code == 204:
            return True, "Workflow triggered successfully ✅"
        else:
            return False, f"GitHub returned {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"Request error: {e}"


def get_last_run_status() -> str:
    """Returns a summary of the last few workflow runs."""
    url = f"{GITHUB_API}/actions/workflows/{WORKFLOW_FILE}/runs"
    try:
        r = requests.get(url, headers=gh_headers(), params={"per_page": 5}, timeout=10)
        r.raise_for_status()
        runs = r.json().get("workflow_runs", [])
        if not runs:
            return "No runs found."
        lines = ["*Last 5 workflow runs:*\n"]
        for run in runs:
            status     = run["status"]
            conclusion = run.get("conclusion") or "in_progress"
            triggered  = run.get("event", "?")
            created    = run["created_at"][:16].replace("T", " ")
            icon = {"success": "✅", "failure": "❌", "in_progress": "🔄", "cancelled": "⏹"}.get(conclusion, "❓")
            lines.append(f"{icon} `{created}` — {conclusion} ({triggered})")
        return "\n".join(lines)
    except Exception as e:
        return f"Could not fetch status: {e}"


# ── Command handler ───────────────────────────────────────────────────────────

def handle_command(chat_id: str, text: str) -> None:
    text = text.strip()
    now  = datetime.now().strftime("%d %b %Y %H:%M IST")

    # Security: only respond to the authorised chat
    if str(chat_id) != str(ALLOWED_CHAT):
        print(f"[WARN] Ignored message from unauthorised chat: {chat_id}")
        return

    print(f"[{now}] Command from {chat_id}: {text!r}")

    if text.startswith("/help") or text == "/start":
        tg_send(chat_id, HELP_TEXT)

    elif text.startswith("/withdraw_all"):
        tg_send(chat_id, "⏳ Triggering withdrawal for *all accounts*…")
        ok, msg = trigger_workflow("withdrawal")
        if ok:
            tg_send(chat_id,
                f"✅ *Withdrawal job started!*\n"
                f"All accounts will be processed. Results will arrive in this chat once done.\n"
                f"_Triggered at {now}_"
            )
        else:
            tg_send(chat_id, f"❌ Failed to trigger:\n`{msg}`")

    elif text.startswith("/withdraw "):
        parts   = text.split()
        account = parts[1].upper() if len(parts) > 1 else ""
        if not account or not account.startswith("R") or not account[1:].isdigit():
            tg_send(chat_id, "❌ Invalid R-ID. Example: `/withdraw R553232`")
            return
        tg_send(chat_id, f"⏳ Triggering withdrawal for *{account}*…")
        ok, msg = trigger_workflow("withdrawal", account=account)
        if ok:
            tg_send(chat_id,
                f"✅ *Withdrawal job started for {account}!*\n"
                f"Result will arrive in this chat once done.\n"
                f"_Triggered at {now}_"
            )
        else:
            tg_send(chat_id, f"❌ Failed to trigger:\n`{msg}`")

    elif text.startswith("/scrape"):
        tg_send(chat_id, "⏳ Triggering daily scraper for *all accounts*…")
        ok, msg = trigger_workflow("scraper")
        if ok:
            tg_send(chat_id,
                f"✅ *Scraper job started!*\n"
                f"Results will arrive in each bot's chat once done.\n"
                f"_Triggered at {now}_"
            )
        else:
            tg_send(chat_id, f"❌ Failed to trigger:\n`{msg}`")

    elif text.startswith("/status"):
        tg_send(chat_id, "⏳ Fetching workflow status…")
        status_text = get_last_run_status()
        tg_send(chat_id, status_text)

    elif text.startswith("/"):
        tg_send(chat_id, f"❓ Unknown command. Send /help for available commands.")


# ── Main loop ─────────────────────────────────────────────────────────────────

def validate_env() -> bool:
    missing = []
    if not BOT_TOKEN:    missing.append("TELEGRAM_BOT_TOKEN_WITHDRAW_MANAV")
    if not ALLOWED_CHAT: missing.append("TELEGRAM_CHAT_ID_MANAV")
    if not GITHUB_PAT:   missing.append("GITHUB_PAT")
    if not GITHUB_OWNER: missing.append("GITHUB_OWNER")
    if missing:
        print("❌ Missing environment variables:")
        for m in missing:
            print(f"   export {m}=<value>")
        return False
    return True


def main():
    if not validate_env():
        sys.exit(1)

    print("=" * 56)
    print("  RM Withdrawal Trigger Bot — listening for commands")
    print(f"  Chat  : {ALLOWED_CHAT}")
    print(f"  Repo  : {GITHUB_OWNER}/{GITHUB_REPO}")
    print(f"  Branch: {BRANCH}")
    print("=" * 56)
    print("  Commands: /withdraw_all  /withdraw <R-ID>  /scrape  /status  /help")
    print("  Press Ctrl+C to stop.\n")

    # Announce startup in the chat
    tg_send(
        ALLOWED_CHAT,
        f"🤖 *RM Trigger Bot is online*\n"
        f"_Started {datetime.now().strftime('%d %b %Y %H:%M IST')}_\n\n"
        f"Send /help to see available commands."
    )

    offset = 0
    while True:
        try:
            updates = tg_get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text    = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if text:
                    handle_command(chat_id, text)
        except KeyboardInterrupt:
            print("\n  Shutting down.")
            tg_send(ALLOWED_CHAT, "🔴 *RM Trigger Bot stopped.*")
            break
        except Exception as e:
            print(f"[ERROR] Main loop: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
