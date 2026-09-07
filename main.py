import asyncio
import os
import re
from html import escape
from typing import Dict, List

import requests
from bs4 import BeautifulSoup
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


LOGIN_URL = os.getenv(
    "LOGIN_URL",
    "https://app.richmakers.space/",
).strip()

DASHBOARD_URL = (
    "https://app.richmakers.space/member/"
    "6e4c797573632532425a6f4a77253344/"
    "6e62756c736463253344"
)

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "",
).strip()

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "",
).strip()


# ============================================================
# Helpers
# ============================================================

def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def extract_money(text: str) -> str:
    text = clean_text(text)

    match = re.search(
        r"(?:[$₹€£]\s*[\d,]+(?:\.\d{2})?|[\d,]+\.\d{2})",
        text,
    )

    return match.group(0) if match else text


def safe_filename(value: str) -> str:
    return re.sub(
        r"[^A-Za-z0-9_.-]",
        "_",
        value,
    )


# ============================================================
# Accounts
# ============================================================

def load_accounts() -> List[Dict[str, str]]:
    usernames_value = os.getenv(
        "RM_USERS",
        "",
    ).strip()

    shared_password = os.getenv(
        "RM_PASSWORD",
        "",
    )

    if not usernames_value:
        raise RuntimeError(
            "RM_USERS is missing or empty."
        )

    if not shared_password:
        raise RuntimeError(
            "RM_PASSWORD is missing or empty."
        )

    usernames = [
        username.strip()
        for username in usernames_value.split(",")
        if username.strip()
    ]

    if not usernames:
        raise RuntimeError(
            "No valid usernames found in RM_USERS."
        )

    accounts = [
        {
            "username": username,
            "password": shared_password,
        }
        for username in usernames
    ]

    print(f"Found {len(accounts)} account(s).")

    return accounts


# ============================================================
# Open Profile Settings
# ============================================================

async def open_profile_settings(page) -> None:
    """
    Opens:

        Three-line menu
        -> My Account
        -> Profile Settings
    """

    # The menu icon is:
    # <i class="material-icons-outlined">menu</i>
    menu_icon = page.locator(
        "i.material-icons-outlined"
    ).filter(
        has_text=re.compile(r"^\s*menu\s*$", re.IGNORECASE)
    ).first

    if await menu_icon.count() == 0:
        raise RuntimeError(
            "Three-line menu icon was not found."
        )

    try:
        await menu_icon.click()
    except Exception:
        # If the icon itself is not clickable,
        # click its parent element.
        menu_parent = menu_icon.locator("..")
        await menu_parent.click()

    await page.wait_for_timeout(700)

    # Click My Account
    my_account = page.get_by_text(
        "My Account",
        exact=True,
    ).first

    if await my_account.count() == 0:
        my_account = page.locator(
            "a",
            has_text="My Account",
        ).first

    if await my_account.count() == 0:
        raise RuntimeError(
            "My Account menu item was not found."
        )

    await my_account.click()
    await page.wait_for_timeout(700)

    # Click Profile Settings
    profile_settings = page.get_by_text(
        "Profile Settings",
        exact=True,
    ).first

    if await profile_settings.count() == 0:
        profile_settings = page.locator(
            "a",
            has_text="Profile Settings",
        ).first

    if await profile_settings.count() == 0:
        raise RuntimeError(
            "Profile Settings menu item was not found."
        )

    await profile_settings.click()
    await page.wait_for_timeout(1500)

    try:
        await page.get_by_text(
            "Your Personal Information",
            exact=False,
        ).wait_for(
            state="visible",
            timeout=15000,
        )
    except PlaywrightTimeoutError:
        pass


# ============================================================
# Profile Field Reader
# ============================================================

async def read_profile_field(
    page,
    field_name: str,
) -> str:
    """
    Reads a profile input by its label.

    Supported fields:

        User ID
        Your Name
    """

    wanted_label = field_name.lower().strip()

    # First, search label elements
    labels = page.locator("label")

    for index in range(await labels.count()):
        label = labels.nth(index)

        label_text = clean_text(
            await label.inner_text()
        ).lower()

        if label_text != wanted_label:
            continue

        parent = label.locator("..")
        input_element = parent.locator("input").first

        if await input_element.count() == 0:
            parent = parent.locator("..")
            input_element = parent.locator("input").first

        if await input_element.count() > 0:
            value = await input_element.input_value()

            if value.strip():
                value = clean_text(value)

                print(
                    f"{field_name} found: {value}"
                )

                return value

    # Search exact visible text and nearby input
    field_text = page.get_by_text(
        field_name,
        exact=True,
    ).first

    if await field_text.count() > 0:
        parent = field_text.locator("..")

        for _ in range(4):
            input_element = parent.locator("input").first

            if await input_element.count() > 0:
                value = await input_element.input_value()

                if value.strip():
                    value = clean_text(value)

                    print(
                        f"{field_name} found near label: "
                        f"{value}"
                    )

                    return value

            parent = parent.locator("..")

    # Fallback selectors
    selectors = {
        "user id": [
            'input[name="user_id"]',
            'input[name="userid"]',
            'input[id="user_id"]',
            'input[id="userid"]',
        ],
        "your name": [
            'input[name="name"]',
            'input[name="full_name"]',
            'input[name="fullname"]',
            'input[name="your_name"]',
            'input[id="name"]',
            'input[id="full_name"]',
            'input[id="fullname"]',
            'input[id="your_name"]',
        ],
    }

    for selector in selectors.get(wanted_label, []):
        input_element = page.locator(selector).first

        if await input_element.count() > 0:
            value = await input_element.input_value()

            if value.strip():
                value = clean_text(value)

                print(
                    f"{field_name} found by selector: "
                    f"{value}"
                )

                return value

    return "Not found"


async def fetch_profile_information(page) -> dict:
    await open_profile_settings(page)

    user_id = await read_profile_field(
        page,
        "User ID",
    )

    your_name = await read_profile_field(
        page,
        "Your Name",
    )

    if user_id == "Not found" or your_name == "Not found":
        await page.screenshot(
            path="profile-settings-debug.png",
            full_page=True,
        )

        with open(
            "profile-settings-debug.html",
            "w",
            encoding="utf-8",
        ) as debug_file:
            debug_file.write(await page.content())

    return {
        "user_id": user_id,
        "name": your_name,
    }


# ============================================================
# Dashboard Parser
# ============================================================

def parse_dashboard_html(html: str) -> dict:
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    result = {
        "active_investment": "Not found",
        "e_wallet": "Not found",
        "total_reward": "Not found",
        "remaining": "Not found",
        "recent_transactions": [],
    }

    # E-wallet
    for paragraph in soup.find_all("p"):
        label = clean_text(
            paragraph.get_text(" ", strip=True)
        ).lower()

        normalized_label = (
            label.replace("\xa0", "")
            .replace(" ", "")
            .replace("-", "")
        )

        if normalized_label != "ewallet":
            continue

        parent = paragraph.parent

        if not parent:
            continue

        value_heading = parent.find(
            "h5",
            recursive=False,
        )

        if not value_heading:
            value_heading = parent.find("h5")

        if value_heading:
            result["e_wallet"] = extract_money(
                value_heading.get_text(
                    " ",
                    strip=True,
                )
            )

            break

    # Active Investment
    for heading in soup.find_all(["h5", "h6"]):
        label = clean_text(
            heading.get_text(" ", strip=True)
        ).lower()

        if label != "active investment":
            continue

        container = heading.parent

        if not container:
            continue

        container_text = clean_text(
            container.get_text(" ", strip=True)
        )

        match = re.search(
            r"invested\s+"
            r"([$₹€£]?\s*[\d,]+(?:\.\d{2})?)",
            container_text,
            flags=re.IGNORECASE,
        )

        if match:
            result["active_investment"] = extract_money(
                match.group(1)
            )

        break

    # Total Reward and Remaining
    financial_labels = {
        "total reward": "total_reward",
        "remaining": "remaining",
    }

    for paragraph in soup.find_all("p"):
        label = clean_text(
            paragraph.get_text(" ", strip=True)
        ).lower()

        label = label.replace("\xa0", " ")

        field_name = financial_labels.get(label)

        if not field_name:
            continue

        card_body = paragraph.find_parent(
            class_="card-body"
        )

        if not card_body:
            continue

        value_heading = card_body.find("h5")

        if value_heading:
            result[field_name] = extract_money(
                value_heading.get_text(
                    " ",
                    strip=True,
                )
            )

    # Latest transaction only
    transaction_heading = None

    for heading in soup.find_all(
        ["h4", "h5", "h6"]
    ):
        heading_text = clean_text(
            heading.get_text(" ", strip=True)
        ).lower()

        if heading_text == "recent transaction":
            transaction_heading = heading
            break

    if transaction_heading:
        transaction_card = transaction_heading.find_parent(
            class_="card"
        )

        if transaction_card:
            table = transaction_card.find("table")

            if table:
                tbody = table.find("tbody")

                if tbody:
                    first_row = tbody.find("tr")

                    if first_row:
                        cells = [
                            clean_text(
                                cell.get_text(
                                    " ",
                                    strip=True,
                                )
                            )
                            for cell in first_row.find_all("td")
                        ]

                        # Date | Wallet | Txn Mode |
                        # Amount | Description
                        if len(cells) >= 5:
                            result[
                                "recent_transactions"
                            ].append(
                                {
                                    "date": cells[0],
                                    "amount": cells[3],
                                }
                            )

    # Fallback parser
    page_text = clean_text(
        soup.get_text(" ", strip=True)
    )

    fallback_fields = {
        "e_wallet": (
            r"([$₹€£]\s*[\d,]+(?:\.\d{2})?)"
            r"\s+E[-\s]?wallet"
        ),
        "total_reward": (
            r"([$₹€£]\s*[\d,]+(?:\.\d{2})?)"
            r"\s+Total\s*Reward"
        ),
        "remaining": (
            r"([$₹€£]\s*[\d,]+(?:\.\d{2})?)"
            r"\s+Remaining"
        ),
    }

    for field_name, pattern in fallback_fields.items():
        if result[field_name] != "Not found":
            continue

        match = re.search(
            pattern,
            page_text,
            flags=re.IGNORECASE,
        )

        if match:
            result[field_name] = extract_money(
                match.group(1)
            )

    return result


# ============================================================
# Login and Scrape
# ============================================================

async def login_and_scrape(
    page,
    username: str,
    password: str,
) -> dict:
    await page.goto(
        LOGIN_URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    await page.wait_for_timeout(2000)

    username_input = page.locator(
        'input[name="username"], '
        'input[name="user_name"], '
        'input[name="email"], '
        'input[type="email"], '
        'input[type="text"]'
    ).first

    password_input = page.locator(
        'input[name="password"], '
        'input[type="password"]'
    ).first

    submit_button = page.locator(
        'button[type="submit"], '
        'input[type="submit"], '
        'button:has-text("Login"), '
        'button:has-text("Log In"), '
        'button:has-text("Sign In")'
    ).first

    await username_input.wait_for(
        state="visible",
        timeout=30000,
    )

    await username_input.fill(username)
    await password_input.fill(password)
    await submit_button.click()

    await page.wait_for_timeout(2500)

    await page.goto(
        DASHBOARD_URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    try:
        await page.wait_for_load_state(
            "networkidle",
            timeout=60000,
        )
    except PlaywrightTimeoutError:
        pass

    await page.wait_for_timeout(2500)

    body_text = await page.locator("body").inner_text()

    if "Welcome back" not in body_text:
        await page.screenshot(
            path=(
                "login-failed-"
                f"{safe_filename(username)}.png"
            ),
            full_page=True,
        )

        raise RuntimeError(
            f"Login failed or dashboard did not load "
            f"for {username}"
        )

    # Click:
    # three-line menu -> My Account -> Profile Settings
    profile_information = (
        await fetch_profile_information(page)
    )

    # Return to dashboard
    await page.goto(
        DASHBOARD_URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    try:
        await page.wait_for_load_state(
            "networkidle",
            timeout=60000,
        )
    except PlaywrightTimeoutError:
        pass

    await page.wait_for_timeout(2000)

    html = await page.content()

    safe_username = safe_filename(username)

    with open(
        f"dashboard-{safe_username}.html",
        "w",
        encoding="utf-8",
    ) as debug_file:
        debug_file.write(html)

    await page.screenshot(
        path=f"dashboard-{safe_username}.png",
        full_page=True,
    )

    metrics = parse_dashboard_html(html)

    metrics["user_id"] = profile_information["user_id"]
    metrics["name"] = profile_information["name"]

    print(
        "Parsed values:",
        {
            "user_id": metrics["user_id"],
            "name": metrics["name"],
            "active_investment": (
                metrics["active_investment"]
            ),
            "e_wallet": metrics["e_wallet"],
            "total_reward": metrics["total_reward"],
            "remaining": metrics["remaining"],
            "latest_transaction": (
                metrics["recent_transactions"]
            ),
        },
    )

    return metrics


# ============================================================
# Telegram Message
# ============================================================

def format_telegram_message(metrics: dict) -> str:
    lines = [
        "📊 <b>Rich Maker Summary</b>",
        "",
        "👤 <b>Account</b>",
        f"User ID: <code>"
        f"{escape(metrics.get('user_id', 'Not found'))}"
        f"</code>",
        f"Name: <code>"
        f"{escape(metrics.get('name', 'Not found'))}"
        f"</code>",
        "",
        "💼 <b>Active Investment</b>",
        escape(
            metrics.get(
                "active_investment",
                "Not found",
            )
        ),
        "",
        "💳 <b>E-wallet</b>",
        escape(
            metrics.get(
                "e_wallet",
                "Not found",
            )
        ),
        "",
        "🎁 <b>Total Reward</b>",
        escape(
            metrics.get(
                "total_reward",
                "Not found",
            )
        ),
        "",
        "💵 <b>Remaining</b>",
        escape(
            metrics.get(
                "remaining",
                "Not found",
            )
        ),
        "",
        "🧾 <b>Recent Transaction</b>",
    ]

    transactions = metrics.get(
        "recent_transactions",
        [],
    )

    if not transactions:
        lines.append("No recent transactions found.")
    else:
        latest_transaction = transactions[0]

        lines.extend(
            [
                "",
                f"📅 <b>Date:</b> "
                f"{escape(latest_transaction.get('date', 'Not found'))}",
                f"💰 <b>Amount:</b> "
                f"{escape(latest_transaction.get('amount', 'Not found'))}",
            ]
        )

    return "\n".join(lines)[:4000]


# ============================================================
# Telegram Sender
# ============================================================

def send_telegram_message(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not configured."
        )

    if not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID is not configured."
        )

    telegram_url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    response = requests.post(
        telegram_url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        timeout=30,
    )

    response.raise_for_status()


# ============================================================
# Process Account
# ============================================================

async def process_account(
    browser,
    account: dict,
) -> bool:
    username = account["username"]
    password = account["password"]

    context = await browser.new_context()
    page = await context.new_page()

    try:
        print(f"Processing account: {username}")

        metrics = await login_and_scrape(
            page,
            username,
            password,
        )

        message = format_telegram_message(metrics)

        send_telegram_message(message)

        print(
            f"Successfully processed account: {username}"
        )

        return True

    except Exception as error:
        print(
            f"Failed to process {username}: "
            f"{type(error).__name__}: {error}"
        )

        try:
            send_telegram_message(
                "❌ <b>Rich Maker scraper failed</b>\n"
                f"Account: <code>{escape(username)}</code>\n"
                "Check the GitHub Actions logs and artifacts."
            )
        except Exception as telegram_error:
            print(
                "Telegram error notification failed: "
                f"{type(telegram_error).__name__}: "
                f"{telegram_error}"
            )

        return False

    finally:
        await context.close()


# ============================================================
# Main
# ============================================================

async def main() -> None:
    accounts = load_accounts()
    successful_accounts = 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox"],
        )

        try:
            for account in accounts:
                success = await process_account(
                    browser,
                    account,
                )

                if success:
                    successful_accounts += 1

        finally:
            await browser.close()

    if successful_accounts == 0:
        raise RuntimeError(
            "All accounts failed. "
            "Check the GitHub Actions logs."
        )


if __name__ == "__main__":
    asyncio.run(main())
