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
        print(
            f"[WARN] Missing env vars '{cfg['token_env']}' or '{cfg['chat_id_env']}' "
            f"for bot '{bot_name}'. Skipping."
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

    # 5. Reward / Bonus Cards
    cards = soup.find_all("div", class_="card")
    for card in cards:
        h5_elem = card.find("h5")
        p_elem = card.find("p")
        if h5_elem and p_elem:
            label = p_elem.get_text(strip=True)
            val_match = re.search(r"\$\s*([\d,]+\.?\d*)", h5_elem.get_text())
            if val_match:
                amount = float(val_match.group(1).replace(",", ""))
                key = label.lower().replace(" ", "_")
                details[key] = amount

    return details


def fetch_data_for_account(page, username: str) -> dict:
    """Navigates, logs in, and extracts dashboard metrics for one account."""
    page.goto(LOGIN_URL)
    page.fill("input[name='username']", username)
    page.fill("input[name='password']", SHARED_PASSWORD)
    page.click("button[type='submit']")
    page.wait_for_url(DASHBOARD_URL)
    page.wait_for_selector(".user-name")
    return parse_dashboard_html(page.content())


def format_account_report(username: str, data: dict, display_name: str = "") -> str:
    """Formats dashboard data into Markdown for Telegram."""
    label = f"{display_name} ({username})" if display_name else username
    lines = [f"👤 *{label}*"]
    if "user_id" in data:
        lines.append(f"  • *User ID:* `{data['user_id']}`")
    if "rank" in data:
        lines.append(f"  • *Rank:* {data['rank']}")
    if "active_investment" in data:
        lines.append(f"  • *Active Inv:* ${data['active_investment']:,.2f}")
    if "e_wallet_balance" in data:
        lines.append(f"  • *E-Wallet:* ${data['e_wallet_balance']:,.2f}")
    if "a_wallet_balance" in data:
        lines.append(f"  • *A-Wallet:* ${data['a_wallet_balance']:,.2f}")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not PLAYWRIGHT_AVAILABLE:
        print("Error: playwright is not installed. Run: pip install playwright && playwright install chromium")
        return

    if not SHARED_PASSWORD:
        print("Error: RM_PASSWORD secret is not set.")
        return

    bot_config = load_bot_config()
    bot_user_mapping = load_user_bot_mapping()
    name_mapping = load_user_name_mapping()
    usernames = get_accounts(bot_user_mapping)

    if not usernames:
        print("Error: user_bot_mapping.json has no user entries.")
        return

    # Scrape each unique account once, cache the result
    scraped: dict[str, str] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for username in usernames:
            context = browser.new_context()
            page = context.new_page()
            display_name = name_mapping.get(username, "")
            try:
                data = fetch_data_for_account(page, username)
                scraped[username] = f"✅ {format_account_report(username, data, display_name)}"
            except Exception as e:
                label = f"{display_name} ({username})" if display_name else username
                scraped[username] = f"❌ *{label}*: Failed (`{str(e)}`)"
            finally:
                context.close()
            print(scraped[username])

        browser.close()

    # Send per-bot consolidated messages using the bot → [user_ids] mapping
    for bot_name, user_ids in bot_user_mapping.items():
        lines = [scraped[uid] for uid in user_ids if uid in scraped]
        if not lines:
            continue
        message = "📊 *Daily Automated Summary*\n\n" + "\n\n".join(lines)
        send_to_bot(bot_name, bot_config, message)


if __name__ == "__main__":
    main()
