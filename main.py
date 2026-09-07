import os
import re
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import requests

# Load local .env file if available
load_dotenv()

# Global Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
LOGIN_URL = os.getenv("RM_LOGIN_URL", "https://app.richmakers.space")
DASHBOARD_URL = os.getenv("RM_DASHBOARD_URL", "https://app.richmakers.space/member/6e4c797573632532425a6f4a77253344/6e62756c736463253344")


def load_accounts_from_env() -> list[dict]:
    """Dynamically parses RM_USER_1, RM_PASS_1, RM_USER_2, RM_PASS_2, etc.,

    from environment variables into an ACCOUNTS array.
    """
    accounts_map = {}

    # Inspect all environment variables for numbered user/pass combinations
    for key, val in os.environ.items():
        user_match = re.match(r"^RM_USER_(\d+)$", key)
        pass_match = re.match(r"^RM_PASS_(\d+)$", key)

        if user_match:
            idx = user_match.group(1)
            accounts_map.setdefault(idx, {})["username"] = val
        elif pass_match:
            idx = pass_match.group(1)
            accounts_map.setdefault(idx, {})["password"] = val

    # Sort by index (1, 2, 3...) and filter out incomplete credentials
    accounts = []
    for idx in sorted(accounts_map.keys(), key=int):
        acc = accounts_map[idx]
        if acc.get("username") and acc.get("password"):
            accounts.append(acc)

    # Fallback to single account variables if numbered ones aren't set
    if not accounts:
        single_user = os.getenv("RM_USERNAME")
        single_pass = os.getenv("RM_PASSWORD")
        if single_user and single_pass:
            accounts.append({"username": single_user, "password": single_pass})

    return accounts


ACCOUNTS = load_accounts_from_env()


def send_telegram_notification(message: str):
    """Sends notification to Telegram if credentials exist."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing. Skipping notification.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        res.raise_for_status()
    except Exception as e:
        print(f"Failed to send Telegram notification: {e}")


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
                details["active_investment"] = float(
                    match.group(1).replace(",", "")
                )

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


def fetch_data_for_account(page, account: dict) -> dict:
    """Navigates, logs in, and extracts dashboard metrics for a single account."""
    username = str(account.get("username"))
    password = str(account.get("password"))

    # 1. Navigate to login
    page.goto(LOGIN_URL)

    # 2. Fill credentials safely
    page.fill("input[name='username']", username)
    page.fill("input[name='password']", password)
    page.click("button[type='submit']")

    # 3. Wait for dashboard elements to load
    page.wait_for_url(DASHBOARD_URL)
    page.wait_for_selector(".user-name")

    # 4. Extract HTML content and parse
    html_content = page.content()
    return parse_dashboard_html(html_content)


def format_account_report(acc_name: str, data: dict) -> str:
    """Formats dashboard data dictionary into Markdown for Telegram."""
    lines = [f"👤 *Account:* `{acc_name}`"]
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


def main():
    if not ACCOUNTS:
        print(
            "Error: No valid account credentials found in environment variables."
        )
        return

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for acc in ACCOUNTS:
            acc_label = acc.get("username", "Unknown Account")

            context = browser.new_context()
            page = context.new_page()

            try:
                data = fetch_data_for_account(page, acc)
                formatted_summary = format_account_report(acc_label, data)
                results.append(f"✅ {formatted_summary}")
            except Exception as e:
                results.append(f"❌ *{acc_label}*: Failed (`{str(e)}`)")
            finally:
                context.close()

        browser.close()

    report = "📊 *Daily Automated Summary*\n\n" + "\n\n".join(results)
    print(report)
    send_telegram_notification(report)


if __name__ == "__main__":
    main()