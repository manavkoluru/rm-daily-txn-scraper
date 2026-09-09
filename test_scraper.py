"""
Dry-run test — scrapes the real website and previews Telegram messages.
No external test framework needed. Run with plain python3.

Usage:
    # Full run — all accounts
    RM_PASSWORD=Rich@959 python3 test_scraper.py

    # Single account only (quick smoke test)
    RM_PASSWORD=Rich@959 python3 test_scraper.py --single

Output is printed to console AND written to telegram_preview.txt.
"""

import os
import sys
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main  # noqa: E402

DIVIDER = "=" * 64
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram_preview.txt")

# ── Helpers ────────────────────────────────────────────────────────────────────

def header(title: str) -> None:
    print(f"\n{'─' * 64}")
    print(f"  {title}")
    print(f"{'─' * 64}")


def ok(msg: str) -> None:
    print(f"  ✅  {msg}")


def fail(msg: str) -> None:
    print(f"  ❌  {msg}")
    sys.exit(1)


def warn(msg: str) -> None:
    print(f"  ⚠️   {msg}")


# ── Test: config files ─────────────────────────────────────────────────────────

def test_config_files():
    header("1 · Config files")
    bot_cfg = main.load_bot_config()
    bot_map = main.load_user_bot_mapping()
    name_map = main.load_user_name_mapping()

    assert bot_cfg,  "bot_config.json is empty"
    assert bot_map,  "user_bot_mapping.json has no entries"
    assert name_map, "user_name_mapping.json is empty"

    accounts = main.get_accounts(bot_map)
    ok(f"Bots configured : {list(bot_cfg.keys())}")
    ok(f"Total accounts  : {len(accounts)}")
    ok(f"Named users     : {len(name_map)}")

    missing_names = [uid for uid in accounts if uid not in name_map]
    if missing_names:
        warn(f"Users without display names: {missing_names}")

    return bot_cfg, bot_map, name_map, accounts


# ── Test: message formatting ───────────────────────────────────────────────────

def test_formatting():
    header("2 · Message formatting (no network)")

    sample_data = {
        "user_id": "R553232",
        "rank": "Gold",
        "active_investment": 1000.00,
        "e_wallet_balance": 250.50,
        "a_wallet_balance": 100.00,
    }

    # With display name
    result = main.format_account_report("R553232", sample_data, "Ranjitha")
    assert "Ranjitha (R553232)" in result
    assert "$1,000.00" in result
    assert "$250.50" in result
    ok("Format with display name  → OK")

    # Without display name
    result2 = main.format_account_report("R000001", {"rank": "Silver"})
    assert "R000001" in result2
    ok("Format without display name → OK")

    # Partial data — must not crash
    main.format_account_report("R999999", {}, "Ghost")
    ok("Format with empty data   → OK")

    print(f"\n  Sample output:\n{DIVIDER}")
    print(main.format_account_report("R553232", sample_data, "Ranjitha"))
    print(DIVIDER)


# ── Test: single account scrape ────────────────────────────────────────────────

def test_single_account(bot_map, name_map, accounts):
    header("3 · Single account — real website")
    if not main.PLAYWRIGHT_AVAILABLE:
        warn("playwright not installed — skipping scrape test.\n"
             "  Install it outside Walmart network: pip install playwright && playwright install chromium")
        return
    from playwright.sync_api import sync_playwright

    username = accounts[0]
    display_name = name_map.get(username, "")
    label = f"{display_name} ({username})" if display_name else username
    print(f"  Account: {label}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            data = main.fetch_data_for_account(page, username)
        finally:
            context.close()
            browser.close()

    assert data, "No data parsed from dashboard"
    ok(f"Raw data: {data}")

    report = main.format_account_report(username, data, display_name)
    print(f"\n  Formatted Telegram message:\n{DIVIDER}")
    print(report)
    print(DIVIDER)


# ── Test: full dry-run ─────────────────────────────────────────────────────────

def test_full_dry_run():
    header("4 · Full dry-run — all accounts (no Telegram send)")

    if not main.PLAYWRIGHT_AVAILABLE:
        warn("playwright not installed — skipping scrape test.\n"
             "  Install it outside Walmart network: pip install playwright && playwright install chromium")
        return

    captured = []

    def fake_send(bot_name, bot_cfg, message):
        block = f"\n{DIVIDER}\n📬 BOT: {bot_name}\n{DIVIDER}\n{message}\n{DIVIDER}"
        print(block)
        captured.append({"bot": bot_name, "message": message})

    with patch.object(main, "send_to_bot", side_effect=fake_send):
        main.main()

    assert captured, "No messages generated — check credentials / site availability"

    # Write preview file
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(f"Telegram Preview — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Bots with messages: {len(captured)}\n\n")
        for item in captured:
            f.write(f"{DIVIDER}\n")
            f.write(f"BOT: {item['bot']}\n")
            f.write(f"{DIVIDER}\n")
            f.write(item["message"])
            f.write(f"\n{DIVIDER}\n\n")

    ok(f"Messages generated for {len(captured)} bot(s)")
    ok(f"Preview written to: {OUTPUT_FILE}")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    single_only = "--single" in sys.argv

    password = os.getenv("RM_PASSWORD", "")
    if not password:
        fail("RM_PASSWORD is not set.\nRun: RM_PASSWORD=yourpassword python3 test_scraper.py")

    errors = []

    try:
        bot_cfg, bot_map, name_map, accounts = test_config_files()
    except Exception as e:
        fail(f"Config test failed: {e}")

    try:
        test_formatting()
    except AssertionError as e:
        errors.append(f"Formatting: {e}")

    if single_only:
        try:
            test_single_account(bot_map, name_map, accounts)
        except Exception as e:
            errors.append(f"Single account: {e}")
    else:
        try:
            test_full_dry_run()
        except Exception as e:
            errors.append(f"Full dry-run: {e}")

    print()
    if errors:
        for err in errors:
            fail(err)
    else:
        print(f"{'─' * 64}")
        print("  All checks passed.")
        print(f"{'─' * 64}\n")
