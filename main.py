import json
import os
import re
from collections import defaultdict

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ── Constants ─────────────────────────────────────────────────────────────────
LOGIN_URL = os.getenv("RM_LOGIN_URL", "https://app.richmakers.space")
DASHBOARD_URL = os.getenv(
    "RM_DASHBOARD_URL",
    "https://app.richmakers.space/member/6e4c797573632532425a6f4a77253344/6e62756c736463253344",
)

# Shared password for all accounts (set as repo secret RM_PASSWORD)
SHARED_PASSWORD = os.getenv("RM_PASSWORD", "")

_BASE = os.path.dirname(os.path.abspath(__file__))


# ── Config loaders ────────────────────────────────────────────────────────────

def load_bot_config() -> dict:
    """Returns {bot_name: {token_env, chat_id_env}} from bot_config.json."""
    path = os.path.join(_BASE, "bot_config.json")
    with open(path, "r") as f:
        return json.load(f)


def load_user_bot_mapping() -> dict:
    """Returns {bot_name: [R_ID, ...]} from user_bot_mapping.json.

    Skips the '_comment' key automatically.
    """
    path = os.path.join(_BASE, "user_bot_mapping.json")
    with open(path, "r") as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def load_user_name_mapping() -> dict:
    """Returns {R_ID: display_name} from user_name_mapping.json."""
    path = os.path.join(_BASE, "user_name_mapping.json")
    with open(path, "r") as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def load_user_rate_mapping() -> dict:
    """Returns {R_ID: rate} where rate is 91 or 97.
    Accounts in rate_91 list get 91, all others get 97.
    """
    path = os.path.join(_BASE, "user_rate_mapping.json")
    with open(path, "r") as f:
        raw = json.load(f)
    rate_91_set = set(raw.get("rate_91", []))
    return rate_91_set  # caller checks membership


def investment_inr(usd: float, is_91: bool) -> float:
    """Investment conversion — no tax.
    91rs accounts: 94 INR/USD  ($1000 → ₹94,000)
    97rs accounts: 100 INR/USD ($1000 → ₹1,00,000)
    """
    rate = 94 if is_91 else 100
    return round(usd * rate, 2)


def returns_inr(usd: float, is_91: bool) -> float:
    """Returns/income conversion — 7% tax deducted.
    91rs accounts: $10 → 10 * 91 * 0.93 = ₹846.30
    97rs accounts: $10 → 10 * 97 * 0.93 = ₹902.10
    """
    rate = 91 if is_91 else 97
    return round(usd * rate * 0.93, 2)


def inr_bracket(amount_inr: float) -> str:
    """Returns formatted INR string in brackets, e.g. (₹846.30)"""
    return f"(₹{amount_inr:,.2f})"


def get_accounts(bot_user_mapping: dict) -> list[str]:
    """Derives the unique set of R-ID usernames to scrape across all bots."""
    seen = set()
    accounts = []
    for user_ids in bot_user_mapping.values():
        for uid in user_ids:
            if uid not in seen:
                seen.add(uid)
                accounts.append(uid)
    return accounts


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_to_bot(bot_name: str, bot_config: dict, message: str) -> None:
    """Sends *message* via the named bot using its env-var credentials."""
    cfg = bot_config.get(bot_name)
    if not cfg:
        print(f"[WARN] No config for bot '{bot_name}'. Skipping.")
        return

    token = os.getenv(cfg["token_env"])
    chat_id = os.getenv(cfg["chat_id_env"])

    if not token or not chat_id:
        if bot_name != "rm_daily_txns_others_bot":
            print(
                f"[WARN] Missing env vars for '{bot_name}'. "
                "Falling back to rm_daily_txns_others_bot."
            )
            send_to_bot("rm_daily_txns_others_bot", bot_config, message)
        else:
            print(
                f"[ERROR] Missing env vars '{cfg['token_env']}' or '{cfg['chat_id_env']}' "
                "for others_bot. Cannot deliver message."
            )
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        res.raise_for_status()
        print(f"[OK] Sent to {bot_name}")
    except Exception as e:
        print(f"[ERROR] Failed to send to {bot_name}: {e}")


# ── Scraping ──────────────────────────────────────────────────────────────────

def parse_dashboard_html(html_content: str) -> dict:
    """Parses extracted HTML content using BeautifulSoup."""
    soup = BeautifulSoup(html_content, "html.parser")
    details = {}

    # 1. User ID
    user_name_elem = soup.find(class_="user-name")
    if user_name_elem:
        match = re.search(r"R\d+", user_name_elem.get_text())
        if match:
            details["user_id"] = match.group(0)

    # 2. Current Rank
    rank_elem = soup.find(class_="rank-badge")
    if rank_elem:
        details["rank"] = rank_elem.get_text(strip=True)

    # 3. Active Investment
    active_inv_elem = soup.find(string=re.compile("Active Investment"))
    if active_inv_elem:
        parent = active_inv_elem.find_parent("div", class_="ms-3")
        if parent:
            match = re.search(r"\$\s*([\d,]+\.?\d*)", parent.get_text())
            if match:
                details["active_investment"] = float(match.group(1).replace(",", ""))

    # 4. Wallet Balances
    wallet_containers = soup.find_all("div", class_="progress")
    for progress in wallet_containers:
        parent_div = progress.find_parent("div")
        if parent_div:
            label_elem = parent_div.find("p")
            val_elem = parent_div.find("h5")
            if label_elem and val_elem:
                label = label_elem.get_text(strip=True).lower()
                val_match = re.search(r"\$\s*([\d,]+\.?\d*)", val_elem.get_text())
                if val_match:
                    amount = float(val_match.group(1).replace(",", ""))
                    if "e-wallet" in label:
                        details["e_wallet_balance"] = amount
                    elif "a-wallet" in label:
                        details["a_wallet_balance"] = amount

    # 5. Reward / Bonus Cards (Revenue Reward, Direct Reward, Level Bonus, Remaining, etc.)
    cards = soup.find_all("div", class_="card")
    for card in cards:
        h5_elem = card.find("h5")
        p_elem = card.find("p")
        if h5_elem and p_elem:
            label = p_elem.get_text(strip=True)
            val_match = re.search(r"\$\s*([\d,]+\.?\d*)", h5_elem.get_text())
            if val_match:
                amount = float(val_match.group(1).replace(",", ""))
                key = label.lower().replace(" ", "_").rstrip("_")
                details[key] = amount

    # 6. Most recent Credit transaction (E-wallet only)
    txn_table = soup.find("h5", string=re.compile("Recent Transaction"))
    if txn_table:
        table = txn_table.find_parent("div", class_="card-body")
        if table:
            rows = table.find_all("tr")
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
                        details["recent_credit_amount"] = float(amt_match.group(1).replace(",", ""))
                        # Keep only the date portion: "07-Sep-2026 12:00 AM" → "07-Sep-2026"
                        details["recent_credit_date"] = cells[0].get_text(strip=True).split(" ")[0]
                    break  # only the most recent

    return details


def fetch_data_for_account(page, username: str) -> dict:
    """Navigates, logs in, and extracts dashboard metrics for one account."""
    page.goto(LOGIN_URL)
    page.fill("input[name='user_id']", username)
    page.fill("input[name='password']", SHARED_PASSWORD)
    page.click("button[type='submit']")
    page.wait_for_url(DASHBOARD_URL)
#     page.wait_for_selector(".user-name")
    return parse_dashboard_html(page.content())


def format_account_report(username: str, data: dict, display_name: str = "", is_91: bool = False) -> str:
    """Formats dashboard data into Markdown for Telegram with INR conversion.

    Active Investment: no tax (94 or 100 INR/USD)
    E-Wallet / Remaining / Recent Credit: after 7% tax (91 or 97 INR/USD)
    """
    label = f"{display_name} ({username})" if display_name else username
    rate_tag = "@91rs" if is_91 else "@97rs"
    lines = [f"👤 *{label}* _{rate_tag}_"]
    if "user_id" in data:
        lines.append(f"  • *User ID:* `{data['user_id']}`")
    if "rank" in data:
        lines.append(f"  • *Rank:* {data['rank']}")
    if "active_investment" in data:
        v = data["active_investment"]
        lines.append(f"  • *Active Inv:* ${v:,.2f} {inr_bracket(investment_inr(v, is_91))}")
    if "e_wallet_balance" in data:
        v = data["e_wallet_balance"]
        lines.append(f"  • *E-Wallet:* ${v:,.2f} {inr_bracket(returns_inr(v, is_91))}")
    if "remaining" in data:
        v = data["remaining"]
        lines.append(f"  • *Remaining:* ${v:,.2f} {inr_bracket(returns_inr(v, is_91))}")
    if "recent_credit_amount" in data:
        v = data["recent_credit_amount"]
        date_str = data.get("recent_credit_date", "")
        lines.append(f"  • *Recent Credit:* ${v:,.2f} {inr_bracket(returns_inr(v, is_91))} on {date_str}")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main(only_bot: str = None):
    if not PLAYWRIGHT_AVAILABLE:
        print("Error: playwright is not installed. Run: pip install playwright && playwright install chromium")
        return

    if not SHARED_PASSWORD:
        print("Error: RM_PASSWORD secret is not set.")
        return

    bot_config = load_bot_config()
    bot_user_mapping = load_user_bot_mapping()
    name_mapping = load_user_name_mapping()
    rate_91_set = load_user_rate_mapping()
    filtered_mapping = {k: v for k, v in bot_user_mapping.items() if not only_bot or k == only_bot}
    usernames = get_accounts(filtered_mapping)

    if not usernames:
        print("Error: user_bot_mapping.json has no user entries.")
        return

    # Scrape each unique account once — cache both formatted text and raw data
    scraped_text: dict[str, str] = {}
    scraped_data: dict[str, dict] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for username in usernames:
            context = browser.new_context()
            page = context.new_page()
            display_name = name_mapping.get(username, "")
            is_91 = username in rate_91_set
            try:
                data = fetch_data_for_account(page, username)
                scraped_data[username] = data
                scraped_text[username] = f"✅ {format_account_report(username, data, display_name, is_91)}"
            except Exception as e:
                label = f"{display_name} ({username})" if display_name else username
                scraped_data[username] = {}
                scraped_text[username] = f"❌ *{label}*: Failed (`{str(e)}`)"
            finally:
                context.close()
            print(scraped_text[username])

        browser.close()

    # Send per-bot consolidated messages using the bot → [user_ids] mapping
    for bot_name, user_ids in bot_user_mapping.items():
        if only_bot and bot_name != only_bot:
            continue
        lines = [scraped_text[uid] for uid in user_ids if uid in scraped_text]
        if not lines:
            continue

        # Compute bot-level totals (USD and INR per account, then summed)
        total_ewallet_usd = 0.0
        total_ewallet_inr = 0.0
        total_credit_usd = 0.0
        total_credit_inr = 0.0

        for uid in user_ids:
            if uid not in scraped_data:
                continue
            d = scraped_data[uid]
            is_91 = uid in rate_91_set
            ew = d.get("e_wallet_balance", 0.0)
            cr = d.get("recent_credit_amount", 0.0)
            total_ewallet_usd += ew
            total_ewallet_inr += returns_inr(ew, is_91)
            total_credit_usd += cr
            total_credit_inr += returns_inr(cr, is_91)

        summary_footer = (
            "\n\n━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 *Total E-Wallet:* ${total_ewallet_usd:,.2f} (₹{total_ewallet_inr:,.2f})\n"
            f"📈 *Total Daily Credit:* ${total_credit_usd:,.2f} (₹{total_credit_inr:,.2f})"
        )

        message = "📊 *Daily Automated Summary*\n\n" + "\n\n".join(lines) + summary_footer
        send_to_bot(bot_name, bot_config, message)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", help="Run only this bot (e.g. rm_daily_txns_pavana_bot)")
    args = parser.parse_args()
    main(only_bot=args.bot)
