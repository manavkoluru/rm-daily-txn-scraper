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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def extract_money(text: str) -> str:
    text = clean_text(text)

    match = re.search(
        r"(?:[$₹€£]\s*[\d,]+(?:\.\d{2})?|[\d,]+\.\d{2})",
        text,
    )

    return match.group(0) if match else text


def load_accounts() -> List[Dict[str, str]]:
    accounts = []
    index = 1

    while True:
        username = os.getenv(f"RM_USER_{index}")
        password = os.getenv(f"RM_PASS_{index}")

        if username is None and password is None:
            break

        if not username or not password:
            raise RuntimeError(
                f"RM_USER_{index} and RM_PASS_{index} "
                "must both be configured."
            )

        accounts.append(
            {
                "username": username.strip(),
                "password": password,
            }
        )

        index += 1

    if not accounts:
        raise RuntimeError(
            "No valid account credentials found. "
            "Expected RM_USER_1 and RM_PASS_1."
        )

    return accounts


def parse_dashboard_html(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    result = {
        "username": "Not found",
        "rank": "Not found",
        "e_wallet": "Not found",
        "active_investment": "Not found",
        "revenue_reward": "Not found",
        "direct_reward": "Not found",
        "level_bonus": "Not found",
        "rank_reward": "Not found",
        "royalty_reward": "Not found",
        "total_reward": "Not found",
        "total_withdraw": "Not found",
        "remaining": "Not found",
        "recent_transactions": [],
    }

    # Username
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5"]):
        text = clean_text(heading.get_text(" ", strip=True))

        if text.endswith("!") and len(text) < 100:
            result["username"] = text.replace("!", "").strip()
            break

    # Rank
    rank_badge = soup.select_one(".rank-badge")

    if rank_badge:
        result["rank"] = clean_text(
            rank_badge.get_text(" ", strip=True)
        )

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

        if parent:
            heading = parent.find("h5")

            if heading:
                result["e_wallet"] = extract_money(
                    heading.get_text(" ", strip=True)
                )
                break

        previous_heading = paragraph.find_previous("h5")

        if previous_heading:
            result["e_wallet"] = extract_money(
                previous_heading.get_text(" ", strip=True)
            )
            break

    # Active investment
    for heading in soup.find_all(["h5", "h6"]):
        label = clean_text(
            heading.get_text(" ", strip=True)
        ).lower()

        if label == "active investment":
            container = heading.parent

            if container:
                text = clean_text(
                    container.get_text(" ", strip=True)
                )

                match = re.search(
                    r"invested\s+([$₹€£]?\s*[\d,]+(?:\.\d{2})?)",
                    text,
                    re.IGNORECASE,
                )

                if match:
                    result["active_investment"] = extract_money(
                        match.group(1)
                    )

            break

    # Financial cards
    label_mapping = {
        "revenue reward": "revenue_reward",
        "direct reward": "direct_reward",
        "level bonus": "level_bonus",
        "rank reward": "rank_reward",
        "royalty reward": "royalty_reward",
        "total reward": "total_reward",
        "total withdraw": "total_withdraw",
        "remaining": "remaining",
    }

    for paragraph in soup.find_all("p"):
        label = clean_text(
            paragraph.get_text(" ", strip=True)
        ).lower()

        label = label.replace("\xa0", " ")

        if label not in label_mapping:
            continue

        field_name = label_mapping[label]
        card_body = paragraph.find_parent(class_="card-body")

        if not card_body:
            continue

        value_heading = card_body.find("h5")

        if value_heading:
            result[field_name] = extract_money(
                value_heading.get_text(" ", strip=True)
            )

    # Recent transactions
    transaction_heading = None

    for heading in soup.find_all(["h4", "h5", "h6"]):
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
                    for row in tbody.find_all("tr"):
                        cells = [
                            clean_text(
                                cell.get_text(" ", strip=True)
                            )
                            for cell in row.find_all("td")
                        ]

                        if len(cells) >= 5:
                            result["recent_transactions"].append(
                                {
                                    "date": cells[0],
                                    "wallet": cells[1],
                                    "mode": cells[2],
                                    "amount": cells[3],
                                    "description": cells[4],
                                }
                            )

    # Fallback parser using visible page text
    page_text = clean_text(
        soup.get_text(" ", strip=True)
    )

    fallback_patterns = {
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

    for field_name, pattern in fallback_patterns.items():
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

    username_locator = page.locator(
        'input[name="username"], '
        'input[name="user_name"], '
        'input[name="email"], '
        'input[type="email"], '
        'input[type="text"]'
    ).first

    password_locator = page.locator(
        'input[name="password"], '
        'input[type="password"]'
    ).first

    submit_locator = page.locator(
        'button[type="submit"], '
        'input[type="submit"], '
        'button:has-text("Login"), '
        'button:has-text("Log In"), '
        'button:has-text("Sign In")'
    ).first

    await username_locator.wait_for(
        state="visible",
        timeout=30000,
    )

    await username_locator.fill(username)
    await password_locator.fill(password)
    await submit_locator.click()

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
        safe_username = re.sub(
            r"[^A-Za-z0-9_.-]",
            "_",
            username,
        )

        await page.screenshot(
            path=f"login-failed-{safe_username}.png",
            full_page=True,
        )

        raise RuntimeError(
            f"Login failed or dashboard did not load for {username}"
        )

    html = await page.content()

    safe_username = re.sub(
        r"[^A-Za-z0-9_.-]",
        "_",
        username,
    )

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

    print(
        "Parsed values:",
        {
            "username": metrics["username"],
            "rank": metrics["rank"],
            "e_wallet": metrics["e_wallet"],
            "active_investment": metrics["active_investment"],
            "total_reward": metrics["total_reward"],
            "remaining": metrics["remaining"],
        },
    )

    important_fields = [
        "e_wallet",
        "active_investment",
        "total_reward",
        "remaining",
    ]

    missing_fields = [
        field
        for field in important_fields
        if metrics.get(field) == "Not found"
    ]

    if len(missing_fields) == len(important_fields):
        raise RuntimeError(
            "Dashboard HTML was loaded, but no financial values "
            f"could be parsed. Missing: {missing_fields}"
        )

    return metrics


def format_telegram_message(metrics: dict) -> str:
    lines = [
        "📊 <b>Rich Maker Daily Summary</b>",
        "",
        f"👤 <b>Account:</b> "
        f"<code>{escape(metrics['username'])}</code>",
        f"🏅 <b>Rank:</b> "
        f"{escape(metrics['rank'])}",
        "",
        f"💳 <b>E-wallet:</b> "
        f"{escape(metrics['e_wallet'])}",
        f"💰 <b>Active Investment:</b> "
        f"{escape(metrics['active_investment'])}",
        f"📈 <b>Revenue Reward:</b> "
        f"{escape(metrics['revenue_reward'])}",
        f"👥 <b>Direct Reward:</b> "
        f"{escape(metrics['direct_reward'])}",
        f"🎁 <b>Level Bonus:</b> "
        f"{escape(metrics['level_bonus'])}",
        f"🏆 <b>Rank Reward:</b> "
        f"{escape(metrics['rank_reward'])}",
        f"🎖 <b>Royalty Reward:</b> "
        f"{escape(metrics['royalty_reward'])}",
        "",
        f"🧮 <b>Total Reward:</b> "
        f"{escape(metrics['total_reward'])}",
        f"🏦 <b>Total Withdraw:</b> "
        f"{escape(metrics['total_withdraw'])}",
        f"💵 <b>Remaining:</b> "
        f"{escape(metrics['remaining'])}",
        "",
        "🧾 <b>Recent Transactions</b>",
    ]

    transactions = metrics.get(
        "recent_transactions",
        [],
    )

    if not transactions:
        lines.append("No recent transactions found.")
    else:
        for transaction in transactions:
            lines.extend(
                [
                    "",
                    f"<b>{escape(transaction['date'])}</b>",
                    f"Wallet: {escape(transaction['wallet'])}",
                    f"Mode: {escape(transaction['mode'])}",
                    f"Amount: {escape(transaction['amount'])}",
                    "Description: "
                    f"{escape(transaction['description'])}",
                ]
            )

    message = "\n".join(lines)

    # Telegram message limit is 4096 characters.
    return message[:4000]


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


async def process_account(browser, account: dict) -> bool:
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

        send_telegram_message(
            format_telegram_message(metrics)
        )

        print(f"Successfully processed account: {username}")
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
                "Check the GitHub Actions artifacts and logs."
            )
        except Exception as telegram_error:
            print(
                "Telegram error notification failed: "
                f"{type(telegram_error).__name__}"
            )

        return False

    finally:
        await context.close()


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
            "All accounts failed. Check the logs and uploaded artifacts."
        )


if __name__ == "__main__":
    asyncio.run(main())
