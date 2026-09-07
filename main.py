import json
import os

import requests
from playwright.sync_api import sync_playwright


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ACCOUNT_CREDENTIALS_JSON = os.getenv("ACCOUNT_CREDENTIALS", "[]")

ACCOUNTS = json.loads(ACCOUNT_CREDENTIALS_JSON)


def send_telegram_notification(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }
    requests.post(url, json=payload)


def fetch_data_for_account(page, account: dict) -> str:
    # Navigate to target login page.
    page.goto("https://example.com/login")

    # Fill login credentials.
    page.fill("input[name='username']", account["username"])
    page.fill("input[name='password']", account["password"])
    page.click("button[type='submit']")

    # Wait for target element to load and extract value.
    page.wait_for_selector(".dashboard-metric")
    return page.inner_text(".dashboard-metric")


def main() -> None:
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for acc in ACCOUNTS:
            context = browser.new_context()
            page = context.new_page()

            try:
                data = fetch_data_for_account(page, acc)
                results.append(f"✅ *{acc['username']}*: {data}")
            except Exception as error:
                results.append(f"❌ *{acc['username']}*: Failed ({error})")
            finally:
                context.close()

        browser.close()

    report = "📊 *Daily Account Summary*\n\n" + "\n".join(results)
    send_telegram_notification(report)


if __name__ == "__main__":
    main()
