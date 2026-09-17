"""
Rich status check script for richmakers.space.
Analyzes Power Leg structure and suggests actions to reach the next Rich status.

Runs on-demand via command line.

For each account:
  1. Logs in via Playwright
  2. Navigates to Income Reports → Rank Reward page
  3. Extracts: Power Leg, 2nd Power Leg, Other Legs values (in USD)
  4. Calculates total business and current Rich status
  5. Determines next Rich status and required growth
  6. Shows multiple suggestions:
     - Minimum: Add to weakest leg to reach next status
     - Balanced: Maintain 40-30-30 ratio while growing
  7. Sends per-group Telegram summary

Usage:
    python3 rich_status_check.py                    # check all accounts
    python3 rich_status_check.py --group pavana    # check pavana group only
    python3 rich_status_check.py --group all       # check all groups except manjula
    python3 rich_status_check.py --account R523341 # single account only
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
INCOME_REPORTS_URL = "https://app.richmakers.space/member/70724b7875394773/70724b347264476365715761644b79576f3559253344"
SHARED_PASSWORD = os.getenv("RM_PASSWORD", "")

_BASE = os.path.dirname(os.path.abspath(__file__))
W = 64  # console width

# Rich status thresholds with per-leg minimums (40-30-30 ratio)
# Format: {level, name, total_usd, power_min, second_min, other_min}
RICH_STATUSES = [
    {"level": 1, "name": "Rich1", "total_usd": 10000, "power_min": 4000, "second_min": 3000, "other_min": 3000, "total_lakh": 10},
    {"level": 2, "name": "Rich2", "total_usd": 30000, "power_min": 12000, "second_min": 9000, "other_min": 9000, "total_lakh": 30},
    {"level": 3, "name": "Rich3", "total_usd": 90000, "power_min": 36000, "second_min": 27000, "other_min": 27000, "total_lakh": 90},
    {"level": 4, "name": "Rich4", "total_usd": 250000, "power_min": 100000, "second_min": 75000, "other_min": 75000, "total_lakh": 250},
    {"level": 5, "name": "Rich5", "total_usd": 750000, "power_min": 300000, "second_min": 225000, "other_min": 225000, "total_lakh": 750},
    {"level": 6, "name": "Rich6", "total_usd": 2500000, "power_min": 1000000, "second_min": 750000, "other_min": 750000, "total_lakh": 2500},
    {"level": 7, "name": "Rich7", "total_usd": 7500000, "power_min": 3000000, "second_min": 2250000, "other_min": 2250000, "total_lakh": 7500},
]

# Leg distribution ratios
POWER_LEG_RATIO = 0.40  # 40%
SECOND_LEG_RATIO = 0.30  # 30%
OTHER_LEGS_RATIO = 0.30  # 30%


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

def parse_income_report_html(html_content: str) -> dict:
    """Parses income report HTML, extracting Power Leg, 2nd Power Leg, Other Legs."""
    soup = BeautifulSoup(html_content, "html.parser")
    details = {}

    # Extract leg values from tables/cards
    # Look for "Power Leg", "2nd Power Leg", "Other Legs" labels and their values
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) >= 2:
                label = cells[0].get_text(strip=True).lower()
                val_text = cells[1].get_text(strip=True)

                # Extract currency value (handles $ and commas)
                val_match = re.search(r"\$\s*([\d,]+\.?\d*)", val_text)
                if val_match:
                    amount = float(val_match.group(1).replace(",", ""))

                    if "power leg" in label and "2nd" not in label:
                        details["power_leg"] = amount
                    elif "2nd" in label and "power" in label:
                        details["second_power_leg"] = amount
                    elif "other" in label and "leg" in label:
                        details["other_legs"] = amount

    # Fallback: Look for divs/cards with these labels
    if not all(k in details for k in ["power_leg", "second_power_leg", "other_legs"]):
        elements = soup.find_all("div")
        for elem in elements:
            text = elem.get_text(strip=True).lower()
            if "power leg" in text and "2nd" not in text:
                # Try to find the value nearby
                val_match = re.search(r"\$\s*([\d,]+\.?\d*)", elem.get_text())
                if val_match:
                    details["power_leg"] = float(val_match.group(1).replace(",", ""))
            elif "2nd" in text and "power" in text:
                val_match = re.search(r"\$\s*([\d,]+\.?\d*)", elem.get_text())
                if val_match:
                    details["second_power_leg"] = float(val_match.group(1).replace(",", ""))
            elif "other" in text and "leg" in text:
                val_match = re.search(r"\$\s*([\d,]+\.?\d*)", elem.get_text())
                if val_match:
                    details["other_legs"] = float(val_match.group(1).replace(",", ""))

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
    """Navigates, logs in, and extracts rich status metrics."""
    # Login
    page.goto(LOGIN_URL)
    page.fill("input[name='user_id']", username)
    page.fill("input[name='password']", SHARED_PASSWORD)
    page.click("button[type='submit']")
    page.wait_for_url(DASHBOARD_URL)
    time.sleep(1)

    # Navigate to Income Reports
    page.goto(INCOME_REPORTS_URL)
    page.wait_for_load_state("domcontentloaded", timeout=15_000)
    time.sleep(1)

    return parse_income_report_html(page.content())


# ── Rich Status Logic ─────────────────────────────────────────────────────────

def get_current_rich_status(total_usd: float, power_leg: float, second_leg: float, other_legs: float) -> dict:
    """Determines current Rich status based on individual leg minimums AND total.

    Checks if all legs meet minimum requirements for a rank.
    Returns highest rank achieved, or Unranked if requirements not met.
    """
    current = {"level": 0, "name": "Unranked", "total_usd": 0, "power_min": 0, "second_min": 0, "other_min": 0, "total_lakh": 0}

    # Check each rank in order and find the highest one that meets ALL requirements
    for status in RICH_STATUSES:
        if (power_leg >= status["power_min"] and
            second_leg >= status["second_min"] and
            other_legs >= status["other_min"]):
            current = status

    return current


def get_next_rich_status(current_level: int) -> dict:
    """Returns next Rich status."""
    for status in RICH_STATUSES:
        if status["level"] > current_level:
            return status
    return None


def calculate_suggestions(current: dict, next_status: dict, power_leg: float, second_leg: float, other_legs: float, is_91: bool) -> list[dict]:
    """Calculates what's needed to reach next Rich status based on per-leg minimums."""
    suggestions = []

    if not next_status:
        return [{"type": "info", "title": "Already at Maximum Rich Status (Rich7)", "details": []}]

    # Calculate deficit for each leg
    add_power = max(0, next_status["power_min"] - power_leg)
    add_second = max(0, next_status["second_min"] - second_leg)
    add_other = max(0, next_status["other_min"] - other_legs)

    total_needed = add_power + add_second + add_other

    if total_needed <= 0:
        return [{"type": "info", "title": "Already meets next Rich status requirements", "details": []}]

    # Show exact amounts needed to reach next rank
    suggestion = {
        "type": "balanced",
        "title": f"To Reach {next_status['name']} (${next_status['total_usd']:,.0f} total)",
        "details": []
    }

    suggestion["details"].append(f"Power Leg: +${add_power:,.2f} (Target: ${next_status['power_min']:,.2f})")
    suggestion["details"].append(f"2nd Power Leg: +${add_second:,.2f} (Target: ${next_status['second_min']:,.2f})")
    suggestion["details"].append(f"Other Legs: +${add_other:,.2f} (Target: ${next_status['other_min']:,.2f})")
    suggestion["needed_total"] = total_needed
    suggestion["new_power"] = power_leg + add_power
    suggestion["new_second"] = second_leg + add_second
    suggestion["new_other"] = other_legs + add_other
    suggestions.append(suggestion)

    return suggestions


def check_rich_status(username: str, data: dict, is_91: bool) -> dict:
    """Analyzes Rich status and checks all leg ratios."""
    result = {
        "username": username,
        "status": "ok",
        "power_leg": 0.0,
        "second_power_leg": 0.0,
        "other_legs": 0.0,
        "total_business": 0.0,
        "current_rich": None,
        "next_rich": None,
        "leg_issues": [],
        "suggestions": [],
        "error": "",
    }

    # Validate required fields
    if "power_leg" not in data:
        result["status"] = "error"
        result["error"] = "Could not read Power Leg"
        return result

    if "second_power_leg" not in data:
        result["status"] = "error"
        result["error"] = "Could not read 2nd Power Leg"
        return result

    if "other_legs" not in data:
        result["status"] = "error"
        result["error"] = "Could not read Other Legs"
        return result

    power_leg = data["power_leg"]
    second_leg = data["second_power_leg"]
    other_legs = data["other_legs"]
    total_business = power_leg + second_leg + other_legs

    result["power_leg"] = power_leg
    result["second_power_leg"] = second_leg
    result["other_legs"] = other_legs
    result["total_business"] = total_business

    # Determine current and next rich status (passing legs for ratio validation)
    current = get_current_rich_status(total_business, power_leg, second_leg, other_legs)
    next_status = get_next_rich_status(current["level"])

    result["current_rich"] = current
    result["next_rich"] = next_status


    # Generate suggestions for next status only if not at max
    if next_status:
        suggestions = calculate_suggestions(current, next_status, power_leg, second_leg, other_legs, is_91)
        result["suggestions"] = suggestions

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
            if group != "others":
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
    print(f"  💎 Rich Status Check — {len(all_accounts)} account(s) [{scope}]")
    print(f"  📊 Analyzing Power Leg structure (40-30-30 ratio)")
    print(f"  🕗 Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"{'═'*W}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for i, username in enumerate(all_accounts, 1):
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
                result = check_rich_status(username, data, False)
                result["bot"] = user_to_bot.get(username, "rm_daily_txns_others_bot")
                result["display_name"] = display
                results.append(result)

                if result["status"] == "error":
                    print(f"❌ {result['error']}")
                else:
                    current = result["current_rich"]
                    next_r = result["next_rich"]
                    next_label = next_r["name"] if next_r else "Max"
                    print(f"✓ {current['name']} → {next_label}")

            except Exception as e:
                r = {
                    "username": username,
                    "status": "error",
                    "error": str(e),
                    "display_name": display,
                    "bot": user_to_bot.get(username, "rm_daily_txns_others_bot"),
                    "power_leg": 0.0,
                    "second_power_leg": 0.0,
                    "other_legs": 0.0,
                    "total_business": 0.0,
                    "current_rich": None,
                    "next_rich": None,
                    "leg_issues": [],
                    "suggestions": [],
                }
                results.append(r)
                print(f"❌ Exception: {e}")
            finally:
                context.close()

        browser.close()

    # ── Console summary ───────────────────────────────────────────────────────
    ok_list = [r for r in results if r["status"] == "ok"]
    error_list = [r for r in results if r["status"] == "error"]

    print(f"\n{'═'*W}")
    print(f"  ✓ OK: {len(ok_list):>3}   ❌ Errors: {len(error_list):>3}")
    if error_list:
        print(f"\n  ❌ Failed accounts:")
        for r in error_list:
            print(f"     • {r['display_name']} ({r['username']}): {r['error']}")
    print(f"{'═'*W}\n")

    # ── Telegram — send per-group messages ──────────────────────────────────
    def group_label(bot_key: str) -> str:
        part = bot_key.replace("rm_daily_txns_", "").replace("_bot", "")
        return part.capitalize()

    # Group all non-error results
    group_results: dict[str, list] = defaultdict(list)
    for r in ok_list:
        group_results[r["bot"]].append(r)

    for group_bot, group_res in group_results.items():
        account_lines = []
        for r in group_res:
            name = f"{r['display_name']} ({r['username']})"
            current = r["current_rich"]
            next_r = r["next_rich"]

            account_header = (
                f"💎 *{name}*\n"
                f"  Status: *{current['name']}* → {next_r['name'] if next_r else 'MAX'}\n"
                f"  Total Business: ${r['total_business']:,.2f}\n\n"
                f"  📊 Leg Breakdown:\n"
                f"    • Power Leg (40%): ${r['power_leg']:,.2f}\n"
                f"    • 2nd Power Leg (30%): ${r['second_power_leg']:,.2f}\n"
                f"    • Other Legs (30%): ${r['other_legs']:,.2f}"
            )

            if next_r and r["suggestions"]:
                suggestions_text = ""
                for sugg in r["suggestions"]:
                    sugg_title = sugg["title"]
                    sugg_details = "\n    ".join(sugg["details"])
                    sugg_needed = f"  (Total needed: ${sugg.get('needed_total', 0):,.2f})" if sugg.get("needed_total", 0) > 0 else ""
                    suggestions_text += f"\n  {sugg_title}{sugg_needed}\n    {sugg_details}"

                account_header += (
                    f"\n\n  🎯 To reach *{next_r['name']}* (${next_r['total_usd']:,.2f}):"
                    f"{suggestions_text}"
                )

            account_lines.append(account_header)

        group_msg = (
            f"💎 *Rich Status Report — {group_label(group_bot)}*\n"
            f"_{datetime.now().strftime('%d %b %Y, %I:%M %p IST')}_\n\n"
            + "\n\n".join(account_lines)
        )

        # Send to group's own bot
        send_to_bot(group_bot, bot_cfg, group_msg)
        # Send to others bot ONLY if not manav or others bot
        if group_bot != "rm_daily_txns_others_bot" and group_bot != "rm_daily_txns_manav_bot":
            send_to_bot("rm_daily_txns_others_bot", bot_cfg, group_msg)


if __name__ == "__main__":
    bot_mapping = _load("user_bot_mapping.json")
    valid_groups = derive_known_groups(bot_mapping)
    valid_groups.append("all")

    parser = argparse.ArgumentParser(description="Rich status check — richmakers.space")
    parser.add_argument("--account", help="Single R-ID (e.g. R523341)")
    parser.add_argument("--group", help=f"Group name: {' | '.join(valid_groups)}")
    args = parser.parse_args()
    main(only_account=args.account, only_group=args.group)
