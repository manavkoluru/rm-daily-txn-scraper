"""
Flushout check script for richmakers.space.
Identifies accounts where remaining balance < 15 days of daily credit.
Flags false positives (e.g., new ID activation bonuses) by detecting major credit spikes.

Runs on-demand via command line.

For each account:
  1. Logs in via Playwright
  2. Scrapes remaining balance and last 3 credit transactions
  3. Calculates: days_remaining = remaining / avg_daily_credit
  4. Alerts if days_remaining < 15
  5. Detects false positives: if max(credits) - min(credits) >= $50, adds caveat
  6. Sends per-group Telegram summary with alerts only

Usage:
    python flushout_check.py                   # check all accounts
    python flushout_check.py --group pavana   # check pavana group only
    python flushout_check.py --group all      # check all groups except manjula
    python flushout_check.py --account R523341 # single account only
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
PERSONAL_INFO_URL = "https://app.richmakers.space/member/70724b7875394773/70724b347264476365715761644b79576f3559253344"
SHARED_PASSWORD = os.getenv("RM_PASSWORD", "")
FLUSHOUT_THRESHOLD_DAYS = 15
FALSE_POSITIVE_THRESHOLD = 50.0  # $50 difference triggers caveat

_BASE = os.path.dirname(os.path.abspath(__file__))
W = 64  # console width


# ── INR conversion (mirrors main.py & withdraw.py) ────────────────────────────

def load_rate_91_set() -> set:
    path = os.path.join(_BASE, "user_rate_mapping.json")
    with open(path) as f:
        data = json.load(f)
    return set(data.get("rate_91", []))


def returns_inr(usd: float, is_91: bool) -> float:
    """E-wallet conversion with 7% tax deducted."""
    return round(usd * (91 if is_91 else 97) * 0.93, 2)


# ── JSON loaders ──────────────────────────────────────────────────────────────

def _load(filename) -> dict:
    path = os.path.join(_BASE, filename)
    with open(path) as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def get_all_accounts(mapping: dict) -> list[str]:
    """Extract unique R-IDs from bot→[ids] mapping."""
    seen, accounts = set(), []
    for ids in mapping.values():
        for uid in ids:
            if uid not in seen:
                seen.add(uid)
                accounts.append(uid)
    return accounts


# ── Telegram ──────────────────────────────────────────────────────────────────

def chunk_message(message: str, max_length: int = 4096) -> list[str]:
    """Splits a message into chunks at \n\n boundaries to respect Telegram's character limit."""
    if len(message) <= max_length:
        return [message]

    chunks = []
    current_chunk = ""

    for paragraph in message.split("\n\n"):
        if len(current_chunk) + len(paragraph) + 2 <= max_length:
            current_chunk += paragraph + "\n\n"
        else:
            if current_chunk:
                chunks.append(current_chunk.rstrip("\n"))
            current_chunk = paragraph + "\n\n"

    if current_chunk:
        chunks.append(current_chunk.rstrip("\n"))

    return chunks if chunks else [message]


def send_to_bot(bot_name: str, bot_config: dict, message: str) -> None:
    cfg = bot_config.get(bot_name)
    if not cfg:
        return
    token = os.getenv(cfg["token_env"])
    chat_id = os.getenv(cfg["chat_id_env"])
    if not token or not chat_id:
        if bot_name != "rm_daily_txns_others_bot":
            send_to_bot("rm_daily_txns_others_bot", bot_config, message)
        else:
            print(f"  [ERROR] No credentials for others_bot — cannot deliver message")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    chunks = chunk_message(message)
    for i, chunk in enumerate(chunks):
        chunk_info = f" (part {i+1}/{len(chunks)})" if len(chunks) > 1 else ""
        try:
            res = requests.post(
                url,
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"},
                timeout=10,
            )
            res.raise_for_status()
            print(f"  [OK] Telegram → {bot_name}{chunk_info}")
        except Exception as e:
            print(f"  [ERROR] Telegram {bot_name}{chunk_info}: {e}")


# ── HTML Parsing ──────────────────────────────────────────────────────────────

def parse_dashboard_html(html_content: str) -> dict:
    """Parses dashboard HTML, extracting remaining balance and credit history."""
    soup = BeautifulSoup(html_content, "html.parser")
    details = {}

    # 1. User ID
    user_name_elem = soup.find(class_="user-name")
    if user_name_elem:
        match = re.search(r"R\d+", user_name_elem.get_text())
        if match:
            details["user_id"] = match.group(0)

    # 2. Remaining balance (key for flushout check)
    cards = soup.find_all("div", class_="card")
    for card in cards:
        h5_elem = card.find("h5")
        p_elem = card.find("p")
        if h5_elem and p_elem:
            label = p_elem.get_text(strip=True).lower()
            val_match = re.search(r"\$\s*([\d,]+\.?\d*)", h5_elem.get_text())
            if val_match:
                amount = float(val_match.group(1).replace(",", ""))
                if "remaining" in label:
                    details["remaining"] = amount
                elif "total" in label and "reward" in label:
                    details["total_rewards"] = amount

    # 3. Last 3 credit transactions (for history & average)
    txn_table = soup.find("h5", string=re.compile("Recent Transaction"))
    if txn_table:
        table = txn_table.find_parent("div", class_="card-body")
        if table:
            rows = table.find_all("tr")
            credit_entries = []
            for row in rows:
                cells = row.find_all("td")
                if len(cells) < 4:
                    continue
                wallet_badge = cells[1].find("span")
                mode_badge = cells[2].find("span")
                if not wallet_badge or not mode_badge:
                    continue
                if "E-wallet" in wallet_badge.get_text() and "Credit" in mode_badge.get_text():
                    amt_match = re.search(r"\$\s*([\d,]+\.?\d*)", cells[3].get_text())
                    if amt_match:
                        amount = float(amt_match.group(1).replace(",", ""))
                        date_str = cells[0].get_text(strip=True).split(" ")[0]
                        credit_entries.append({"amount": amount, "date": date_str})
                        if len(credit_entries) >= 3:
                            break
            if credit_entries:
                details["credit_history"] = [e["amount"] for e in credit_entries]
                details["recent_credit_amount"] = credit_entries[0]["amount"]
                details["recent_credit_date"] = credit_entries[0]["date"]

    return details


def fetch_account_name(page) -> str:
    """Fetches account name from Personal Information section."""
    try:
        page.goto(PERSONAL_INFO_URL)
        page.wait_for_load_state("domcontentloaded", timeout=10_000)
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")

        # Look for "Your Personal Information" section and "Your Name" attribute
        sections = soup.find_all(["div", "section"])
        for section in sections:
            section_text = section.get_text().lower()
            if "your personal information" in section_text:
                # Find "Your Name" in this section
                rows = section.find_all(["tr", "div"])
                for row in rows:
                    row_text = row.get_text()
                    if "your name" in row_text.lower():
                        # Extract name from the next element or same row
                        cells = row.find_all(["td", "span", "p"])
                        for cell in cells:
                            cell_text = cell.get_text(strip=True)
                            if cell_text and "your name" not in cell_text.lower():
                                return cell_text
        return ""
    except Exception as e:
        print(f"[WARN] Could not fetch account name: {e}")
        return ""


def fetch_data_for_account(page, username: str) -> dict:
    """Navigates, logs in, and extracts flushout check metrics."""
    page.goto(LOGIN_URL)
    page.fill("input[name='user_id']", username)
    page.fill("input[name='password']", SHARED_PASSWORD)
    page.click("button[type='submit']")
    page.wait_for_url(DASHBOARD_URL)
    return parse_dashboard_html(page.content())


# ── Flushout Logic ────────────────────────────────────────────────────────────

def check_flushout(username: str, data: dict, is_91: bool) -> dict:
    """
    Analyzes account for flushout risk.

    Returns:
      {
        "username": str,
        "status": "alert" | "ok" | "error",
        "remaining": float,
        "daily_credit": float,
        "days_remaining": float,
        "is_false_positive": bool,
        "credit_history": [float],
        "error": str,
        "is_91": bool,
      }
    """
    result = {
        "username": username,
        "status": "ok",
        "remaining": 0.0,
        "daily_credit": 0.0,
        "days_remaining": 0.0,
        "is_false_positive": False,
        "credit_history": [],
        "error": "",
        "is_91": is_91,
    }

    # Validate required fields
    if "remaining" not in data:
        result["status"] = "error"
        result["error"] = "Could not read remaining balance"
        return result

    if "credit_history" not in data or not data["credit_history"]:
        result["status"] = "error"
        result["error"] = "No credit history found"
        return result

    remaining = data["remaining"]
    credit_history = data["credit_history"]
    daily_credit = credit_history[0]  # most recent

    result["remaining"] = remaining
    result["daily_credit"] = daily_credit
    result["credit_history"] = credit_history

    # Calculate days remaining
    if daily_credit <= 0:
        result["status"] = "error"
        result["error"] = "Daily credit is zero or negative"
        return result

    days_remaining = remaining / daily_credit
    result["days_remaining"] = days_remaining

    # Detect false positive: major credit spike
    if len(credit_history) >= 3:
        max_credit = max(credit_history)
        min_credit = min(credit_history)
        credit_diff = max_credit - min_credit
        if credit_diff >= FALSE_POSITIVE_THRESHOLD:
            result["is_false_positive"] = True

    # Alert if below threshold
    if days_remaining < FLUSHOUT_THRESHOLD_DAYS:
        result["status"] = "alert"
    
    return result


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
            if group != "others":  # exclude others
                groups.append(group)
    return sorted(groups)


def main(only_account: str = None, only_group: str = None):
    if not PLAYWRIGHT_AVAILABLE:
        print("playwright not installed. Run: pip install playwright && playwright install chromium")
        return
    if not SHARED_PASSWORD:
        print("RM_PASSWORD is not set.")
        return

    mapping = _load("user_bot_mapping.json")
    name_map = _load("user_name_mapping.json")
    rate_91_set = load_rate_91_set()
    known_groups = derive_known_groups(mapping)

    with open(os.path.join(_BASE, "bot_config.json")) as f:
        bot_cfg = json.load(f)

    # Handle special "all" group: all except manjula
    if only_group == "all":
        mapping = {k: v for k, v in mapping.items() if k != "rm_daily_txns_manjula_bot" and k != "rm_daily_txns_others_bot"}
    elif only_group:
        bot_key = group_to_bot_key(only_group)
        if bot_key not in mapping:
            print(f"Group '{only_group}' not found. Valid: {', '.join(known_groups)}")
            return
        mapping = {bot_key: mapping[bot_key]}

    all_accounts = get_all_accounts(mapping)

    # Filter by single account if specified
    if only_account:
        all_accounts = [a for a in all_accounts if a == only_account]
        if not all_accounts:
            print(f"Account '{only_account}' not found in user_bot_mapping.json")
            return

    # Map username to bot_name for grouping results
    user_to_bot: dict[str, str] = {}
    for bot_name, ids in mapping.items():
        for uid in ids:
            if uid not in user_to_bot:
                user_to_bot[uid] = bot_name

    results: list[dict] = []
    scope = f"group:{only_group}" if only_group else (f"account:{only_account}" if only_account else "all accounts")

    print(f"\n{'═'*W}")
    print(f"  ⚠️  Flushout Check — {len(all_accounts)} account(s) [{scope}]")
    print(f"  📅 Threshold: {FLUSHOUT_THRESHOLD_DAYS} days of daily credit")
    print(f"  💡 False Positive Detection: Credit spike >= ${FALSE_POSITIVE_THRESHOLD}")
    print(f"  🕗 Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"{'═'*W}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for i, username in enumerate(all_accounts, 1):
            is_91 = username in rate_91_set

            context = browser.new_context()
            page = context.new_page()

            # Fetch account name from website (fallback to mapping if not available)
            display = fetch_account_name(page)
            if not display:
                display = name_map.get(username, username)
            label = f"{display} ({username})"

            print(f"  [{i:02d}/{len(all_accounts):02d}] {label}... ", end="", flush=True)

            try:
                data = fetch_data_for_account(page, username)
                result = check_flushout(username, data, is_91)
                result["bot"] = user_to_bot.get(username, "rm_daily_txns_others_bot")
                result["display_name"] = display
                results.append(result)

                if result["status"] == "alert":
                    print(f"⚠️  {result['days_remaining']:.1f} days")
                elif result["status"] == "error":
                    print(f"❌ {result['error']}")
                else:
                    print(f"✓ OK ({result['days_remaining']:.1f} days)")

            except Exception as e:
                r = {
                    "username": username,
                    "status": "error",
                    "error": str(e),
                    "display_name": display,
                    "bot": user_to_bot.get(username, "rm_daily_txns_others_bot"),
                    "is_91": is_91,
                    "remaining": 0.0,
                    "daily_credit": 0.0,
                    "days_remaining": 0.0,
                    "is_false_positive": False,
                    "credit_history": [],
                }
                results.append(r)
                print(f"❌ Exception: {e}")
            finally:
                context.close()

        browser.close()

    # ── Console summary ───────────────────────────────────────────────────────
    alert_list = [r for r in results if r["status"] == "alert"]
    error_list = [r for r in results if r["status"] == "error"]

    print(f"\n{'═'*W}")
    print(f"  ⚠️  Alerts: {len(alert_list):>3}   ❌ Errors: {len(error_list):>3}   ✓ OK: {len(results) - len(alert_list) - len(error_list):>3}")
    if alert_list:
        print(f"\n  🚨 Accounts needing flushout:")
        for r in alert_list:
            caveat = " (⚠️ POSSIBLE FALSE POSITIVE — new ID activation?)" if r["is_false_positive"] else ""
            print(f"     • {r['display_name']} ({r['username']})")
            print(f"       Remaining: ${r['remaining']:,.2f} | Daily: ${r['daily_credit']:,.2f} | Days: {r['days_remaining']:.1f}{caveat}")
    if error_list:
        print(f"\n  ❌ Failed accounts:")
        for r in error_list:
            print(f"     • {r['display_name']} ({r['username']}): {r['error']}")
    print(f"{'═'*W}\n")

    # ── Telegram — send per-group messages ────────────────────────────────────
    def group_label(bot_key: str) -> str:
        part = bot_key.replace("rm_daily_txns_", "").replace("_bot", "")
        return part.capitalize()

    # Group all accounts (not just alerts) for reporting
    group_all_results: dict[str, list] = defaultdict(list)
    for r in results:
        if r["status"] != "error":  # Include both alerts and OK
            group_all_results[r["bot"]].append(r)

    for group_bot, group_res in group_all_results.items():
        # Separate alerts from OK accounts
        alerts = [r for r in group_res if r["status"] == "alert"]
        ok_accounts = [r for r in group_res if r["status"] == "ok"]

        account_lines = []
        # Add alert accounts first
        for r in alerts:
            name = f"{r['display_name']} ({r['username']})"
            rate = "@91rs" if r["is_91"] else "@97rs"
            inr_remaining = returns_inr(r["remaining"], r["is_91"])
            inr_daily = returns_inr(r["daily_credit"], r["is_91"])

            caveat = ""
            if r["is_false_positive"]:
                credit_str = " | ".join(f"${c:,.2f}" for c in r["credit_history"])
                caveat = f"\n     ⚠️  POSSIBLE FALSE POSITIVE (credit history: {credit_str})\n     _Check if new ID activation or referral bonus applied_"

            account_lines.append(
                f"  🚨 *{name}* _{rate}_\n"
                f"     Remaining: ${r['remaining']:,.2f} {inr_bracket(inr_remaining)}\n"
                f"     Daily Credit: ${r['daily_credit']:,.2f} {inr_bracket(inr_daily)}\n"
                f"     Days Until Flushout: *{r['days_remaining']:.1f} days*"
                f"{caveat}"
            )

        if alerts:
            # Send alert message only if there are alerts
            group_msg = (
                f"⚠️  *Flushout Alert — {group_label(group_bot)}*\n"
                f"_{datetime.now().strftime('%d %b %Y, %I:%M %p IST')}_\n\n"
                f"The following account(s) have <{FLUSHOUT_THRESHOLD_DAYS} days of remaining balance:\n\n"
                + "\n\n".join(account_lines)
            )
        else:
            # Send "all clear" message when no alerts
            ok_count = len(ok_accounts)
            group_msg = (
                f"✅ *Flushout Status — {group_label(group_bot)}*\n"
                f"_{datetime.now().strftime('%d %b %Y, %I:%M %p IST')}_\n\n"
                f"No flushout alerts. All {ok_count} account(s) have sufficient balance (≥{FLUSHOUT_THRESHOLD_DAYS} days)."
            )

        # Send to group's own bot
        send_to_bot(group_bot, bot_cfg, group_msg)
        # Send to others bot ONLY if not manav or others bot
        if group_bot != "rm_daily_txns_others_bot" and group_bot != "rm_daily_txns_manav_bot":
            send_to_bot("rm_daily_txns_others_bot", bot_cfg, group_msg)


def inr_bracket(amount_inr: float) -> str:
    """Returns formatted INR string in brackets."""
    return f"(₹{amount_inr:,.2f})"


if __name__ == "__main__":
    bot_mapping = _load("user_bot_mapping.json")
    valid_groups = derive_known_groups(bot_mapping)
    valid_groups.append("all")  # Add "all" as valid group

    parser = argparse.ArgumentParser(description="Flushout check — richmakers.space")
    parser.add_argument("--account", help="Single R-ID (e.g. R523341)")
    parser.add_argument("--group", help=f"Group name: {' | '.join(valid_groups)}")
    args = parser.parse_args()
    main(only_account=args.account, only_group=args.group)
