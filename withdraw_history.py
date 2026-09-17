"""
Withdraw history command to fetch and display last 5 withdrawals.

Usage:
    python3 withdraw_history.py manav
    python3 withdraw_history.py ranjitha
    python3 withdraw_history.py R523341
"""

import argparse
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────

LOGIN_URL = os.getenv("RM_LOGIN_URL", "https://app.richmakers.space")
DASHBOARD_URL = os.getenv(
    "RM_DASHBOARD_URL",
    "https://app.richmakers.space/member/6e4c797573632532425a6f4a77253344/6e62756c736463253344",
)
WITHDRAWAL_HISTORY_URL = "https://app.richmakers.space/member/71363674754d5373/69376131744d4f716d71352532426e4b69706f4b5373"
SHARED_PASSWORD = os.getenv("RM_PASSWORD", "")

_BASE = os.path.dirname(os.path.abspath(__file__))
W = 64  # console width


# ── JSON loaders ──────────────────────────────────────────────────────────────

def _load(filename) -> dict:
    path = os.path.join(_BASE, filename)
    with open(path) as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_to_bot(bot_key: str, bot_config: dict, message: str) -> None:
    """Sends message to Telegram bot."""
    cfg = bot_config.get(bot_key)
    if not cfg:
        print(f"  [WARN] No bot config for {bot_key}")
        return
    token = os.getenv(cfg["token_env"])
    chat_id = os.getenv(cfg["chat_id_env"])
    if not token or not chat_id:
        print(f"  [WARN] No credentials for {bot_key}")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    try:
        res = requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        res.raise_for_status()
        print(f"  [OK] Telegram → {bot_key}")
    except Exception as e:
        print(f"  [ERROR] Telegram {bot_key}: {e}")


# ── HTML Parsing ──────────────────────────────────────────────────────────────

def parse_withdrawal_history(html_content: str) -> list[dict]:
    """Parses withdrawal history HTML, extracting Date, Amount, Status."""
    soup = BeautifulSoup(html_content, "html.parser")
    withdrawals = []

    # Look for the table containing withdrawal history
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")

        # Identify header row to map column indices
        header_row = rows[0] if rows else None
        if not header_row:
            continue

        headers = [h.get_text(strip=True).lower() for h in header_row.find_all(["th", "td"])]

        # Find column indices for Date, Amount, Status
        date_idx = amount_idx = status_idx = -1
        for i, header in enumerate(headers):
            if "date" in header:
                date_idx = i
            elif "amount" in header or "withdrawal" in header:
                amount_idx = i
            elif "status" in header or "transaction" in header:
                status_idx = i

        # Parse data rows
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) < 3:
                continue

            cell_texts = [cell.get_text(strip=True) for cell in cells]

            # Extract values using identified indices (fallback to positional if not found)
            if date_idx >= 0 and amount_idx >= 0 and status_idx >= 0:
                date_val = cell_texts[date_idx] if date_idx < len(cell_texts) else ""
                amount_val = cell_texts[amount_idx] if amount_idx < len(cell_texts) else ""
                status_val = cell_texts[status_idx] if status_idx < len(cell_texts) else ""
            else:
                # Fallback: look for patterns
                # Date pattern: contains month names or date format
                # Amount: contains $ or is numeric
                # Status: Success, Failure, Pending, etc.
                date_val = amount_val = status_val = ""

                for cell in cell_texts:
                    if re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\d{1,2}-\d{1,2}-\d{4})", cell):
                        date_val = cell
                    elif re.search(r"^\$?[\d,]+\.?\d*$|Rupees|INR|USD", cell):
                        amount_val = cell
                    elif re.search(r"(Success|Failure|Pending|Failed|Completed|Processing)", cell, re.I):
                        status_val = cell

            # Validate and add if all fields present
            if date_val and amount_val and status_val:
                if "date" not in date_val.lower() and "amount" not in amount_val.lower():
                    withdrawals.append({
                        "date": date_val,
                        "amount": amount_val,
                        "status": status_val,
                    })

    # Return last 5 withdrawals (reverse chronological order)
    return withdrawals[-5:] if withdrawals else []


def fetch_withdrawal_history(page, username: str) -> list[dict]:
    """Navigates, logs in, and extracts withdrawal history."""
    # Login
    page.goto(LOGIN_URL)
    page.fill("input[name='user_id']", username)
    page.fill("input[name='password']", SHARED_PASSWORD)
    page.click("button[type='submit']")
    page.wait_for_url(DASHBOARD_URL)
    time.sleep(1)

    # Navigate to Withdrawal History
    page.goto(WITHDRAWAL_HISTORY_URL)
    page.wait_for_load_state("domcontentloaded", timeout=15_000)
    time.sleep(1)

    return parse_withdrawal_history(page.content())


# ── Main ──────────────────────────────────────────────────────────────────────

def group_to_bot_key(group: str) -> str:
    """e.g. 'manav' → 'rm_daily_txns_manav_bot'"""
    return f"rm_daily_txns_{group.lower()}_bot"


def derive_known_groups(bot_mapping: dict) -> list[str]:
    """Dynamically derive group names from bot_mapping keys."""
    groups = []
    for key in bot_mapping.keys():
        if key.startswith("rm_daily_txns_") and key.endswith("_bot"):
            group = key.replace("rm_daily_txns_", "").replace("_bot", "")
            if group != "others":
                groups.append(group)
    return sorted(groups)


def main(account_id: str):
    if not PLAYWRIGHT_AVAILABLE:
        print("playwright not installed. Run: pip install playwright && playwright install chromium")
        return
    if not SHARED_PASSWORD:
        print("RM_PASSWORD is not set.")
        return

    name_map = _load("user_name_mapping.json")
    bot_mapping = _load("user_bot_mapping.json")
    known_groups = derive_known_groups(bot_mapping)

    with open(os.path.join(_BASE, "bot_config.json")) as f:
        bot_cfg = json.load(f)

    # Determine if input is a group name or account ID
    is_group = account_id.lower() in known_groups
    accounts_to_fetch = []
    bot_name = "rm_daily_txns_others_bot"

    if is_group:
        # Group name provided - fetch all accounts in this group
        bot_name = group_to_bot_key(account_id)
        accounts_to_fetch = bot_mapping.get(bot_name, [])
        scope_label = f"Group: {account_id.capitalize()}"
    else:
        # Single account provided
        username = account_id
        for rid, name in name_map.items():
            if name.lower() == account_id.lower():
                username = rid
                break

        accounts_to_fetch = [username]
        # Determine bot for this account
        for bot_key, ids in bot_mapping.items():
            if username in ids:
                bot_name = bot_key
                break

        display_name = name_map.get(username, account_id)
        scope_label = f"{display_name} ({username})"

    print(f"\n{'═'*W}")
    print(f"  💰 Withdrawal History — {scope_label}")
    print(f"  🕗 {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"{'═'*W}\n")

    all_results = {}  # {username: [{date, amount, status}, ...]}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for username in accounts_to_fetch:
            display_name = name_map.get(username, username)

            context = browser.new_context()
            page = context.new_page()

            try:
                withdrawals = fetch_withdrawal_history(page, username)

                if withdrawals:
                    all_results[username] = {
                        "display_name": display_name,
                        "withdrawals": withdrawals,
                    }
                    print(f"  ✓ {display_name} ({username}): {len(withdrawals)} withdrawals")
                else:
                    print(f"  ⚠️  {display_name} ({username}): No history found")

            except Exception as e:
                print(f"  ❌ {display_name} ({username}): {e}")

            finally:
                context.close()

        browser.close()

    # Display all results
    if not all_results:
        print("\n  ❌ No withdrawal history found for any account.\n")
    else:
        print("\n  📊 Latest 5 Withdrawals per Account:\n")

        telegram_account_msgs = []

        for username, data in all_results.items():
            display_name = data["display_name"]
            withdrawals = data["withdrawals"]

            print(f"\n  💎 {display_name} ({username})")
            print(f"  {'-' * 60}")

            # Determine column widths
            max_date_len = max(len(w["date"]) for w in withdrawals) if withdrawals else 10
            max_amount_len = max(len(w["amount"]) for w in withdrawals) if withdrawals else 10
            max_status_len = max(len(w["status"]) for w in withdrawals) if withdrawals else 10

            # Print header
            print(f"  {'Date':<{max_date_len}}   {'Amount':<{max_amount_len}}   {'Status':<{max_status_len}}")
            print(f"  {'-'*max_date_len}   {'-'*max_amount_len}   {'-'*max_status_len}")

            # Print rows
            for w in withdrawals:
                print(f"  {w['date']:<{max_date_len}}   {w['amount']:<{max_amount_len}}   {w['status']:<{max_status_len}}")

            # Build Telegram message for this account
            table_rows = "\n".join(
                f"{w['date']:<{max_date_len}} | {w['amount']:<{max_amount_len}} | {w['status']:<{max_status_len}}"
                for w in withdrawals
            )
            account_telegram = (
                f"💎 *{display_name}*\n"
                f"```\n"
                f"Date                 Amount     Status\n"
                f"{'-'*max_date_len} {'-'*max_amount_len} {'-'*max_status_len}\n"
                f"{table_rows}\n"
                f"```"
            )
            telegram_account_msgs.append(account_telegram)

        # Send combined Telegram message
        print(f"\n  📤 Sending to Telegram...\n")
        telegram_msg = (
            f"💰 *Withdrawal History*\n"
            f"_{datetime.now().strftime('%d %b %Y, %I:%M %p IST')}_\n\n"
            + "\n\n".join(telegram_account_msgs)
        )
        send_to_bot(bot_name, bot_cfg, telegram_msg)

    print(f"{'═'*W}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="View withdrawal history for accounts or groups",
        epilog="Examples:\n  python withdraw_history.py manav\n  python withdraw_history.py ranjitha\n  python withdraw_history.py R523341",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "input",
        help="Group name (manav|ranjitha|pavana|poornima|vasu|manjula) or R-ID (e.g. R523341)",
    )
    args = parser.parse_args()
    main(args.input)
