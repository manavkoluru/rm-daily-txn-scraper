"""
Weekly automated withdrawal script for richmakers.space.
Runs every Saturday at 08:00 IST via GitHub Actions.

For each account:
  1. Logs in via Playwright
  2. Navigates directly to the withdrawal page
  3. Reads current balance
  4. Skips if floor(balance) < MIN_WITHDRAWAL_USD (default $10)
  5. Submits withdrawal of floor(balance) to the shared wallet address
  6. Captures exact UI message (alert/toast) as the result reason
  7. Sends per-bot Telegram summary with all details + INR totals

Usage:
    python withdraw.py                   # run all accounts
    python withdraw.py --dry-run         # preview amounts, no form submission
    python withdraw.py --account R523341 # single account only
"""

import argparse
import json
import math
import os
import re
import time
from collections import defaultdict
from datetime import datetime

import requests

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────

WALLET_ADDRESS      = "0x6E8fD80B07BE01FD47bf3b2d47B8048e65c4A698"
LOGIN_URL           = os.getenv("RM_LOGIN_URL", "https://app.richmakers.space")
WITHDRAWAL_URL      = "https://app.richmakers.space/member/71363674754d5373/71376131744d4f716d7136586e77253344253344"
SHARED_PASSWORD     = os.getenv("RM_PASSWORD", "")
MIN_WITHDRAWAL_USD  = 10   # skip if floor(balance) < this

_BASE = os.path.dirname(os.path.abspath(__file__))

W = 64   # console width


# ── INR conversion (mirrors main.py) ─────────────────────────────────────────

def load_rate_91_set() -> set:
    path = os.path.join(_BASE, "user_rate_mapping.json")
    with open(path) as f:
        data = json.load(f)
    return set(data.get("rate_91", []))


def returns_inr(usd: float, is_91: bool) -> float:
    """E-wallet withdrawals use the returns rate (7% tax deducted)."""
    return round(usd * (91 if is_91 else 97) * 0.93, 2)


# ── JSON loaders ─────────────────────────────────────────────────────────────

def _load(filename) -> dict:
    path = os.path.join(_BASE, filename)
    with open(path) as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def get_all_accounts(mapping: dict) -> list[str]:
    seen, accounts = set(), []
    for ids in mapping.values():
        for uid in ids:
            if uid not in seen:
                seen.add(uid)
                accounts.append(uid)
    return accounts


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_to_bot(bot_name: str, bot_config: dict, message: str) -> None:
    cfg = bot_config.get(bot_name)
    if not cfg:
        return
    token   = os.getenv(cfg["token_env"])
    chat_id = os.getenv(cfg["chat_id_env"])
    if not token or not chat_id:
        if bot_name != "rm_daily_txns_others_bot":
            send_to_bot("rm_daily_txns_others_bot", bot_config, message)
        else:
            print(f"  [ERROR] No credentials for others_bot — cannot deliver message")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        res = requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        res.raise_for_status()
        print(f"  [OK] Telegram → {bot_name}")
    except Exception as e:
        print(f"  [ERROR] Telegram {bot_name}: {e}")


# ── Browser helpers ───────────────────────────────────────────────────────────

def dismiss_modal(page) -> None:
    """Force-removes the Bootstrap image modal and backdrop via JS."""
    page.evaluate("""
        const m = document.getElementById('imageModal');
        if (m) { m.classList.remove('show'); m.style.display = 'none'; }
        document.querySelectorAll('.modal-backdrop').forEach(el => el.remove());
        document.body.classList.remove('modal-open');
        document.body.style.overflow = '';
    """)


def read_ui_alerts(page) -> str:
    """
    Reads all visible alert / toast / notification text from the page.
    Returns the combined text, or '' if none found.
    """
    try:
        text = page.evaluate("""
            () => {
                const selectors = [
                    '.alert', '.toast', '[class*="alert"]', '[class*="toast"]',
                    '[class*="notification"]', '[class*="swal"]',
                    '.text-danger', '.text-success', '.text-warning'
                ];
                const seen = new Set();
                const msgs = [];
                for (const sel of selectors) {
                    for (const el of document.querySelectorAll(sel)) {
                        const t = el.innerText.trim();
                        if (t && !seen.has(t)) { seen.add(t); msgs.push(t); }
                    }
                }
                return msgs.join(' | ');
            }
        """)
        return (text or "").strip()
    except Exception:
        return ""


# ── Core withdrawal ───────────────────────────────────────────────────────────

def do_withdrawal(page, username: str, dry_run: bool = False) -> dict:
    """
    Result dict:
      status   : "success" | "skipped" | "error"
      amount   : int    — USD floor amount (0 if not attempted)
      balance  : float  — balance read from page (0.0 if not reached)
      reason   : str    — our label
      ui_msg   : str    — exact text the site showed (alert / error banner)
    """
    result = {"status": "error", "amount": 0, "balance": 0.0, "reason": "", "ui_msg": "", "time_blocked": False}

    # ── Step 1: Login ─────────────────────────────────────────────────────────
    try:
        page.goto(LOGIN_URL)
        page.wait_for_selector("input[name='user_id']", timeout=15_000)
        page.fill("input[name='user_id']", username)
        page.fill("input[name='password']", SHARED_PASSWORD)
        page.click("button[type='submit']")
        page.wait_for_url("**/member/**", timeout=20_000)
        print("Login OK  │  ", end="", flush=True)
    except Exception as e:
        print("Login FAILED")
        result["reason"] = "Login failed"
        result["ui_msg"] = str(e)
        return result

    # ── Step 2: Navigate to withdrawal page ───────────────────────────────────
    try:
        print("Loading page... ", end="", flush=True)
        page.goto(WITHDRAWAL_URL)
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
        time.sleep(2)
        dismiss_modal(page)
        time.sleep(0.5)
        page.wait_for_selector("input[placeholder='re-enter your withdrawal address']", timeout=10_000)
        print("Ready  │  ", end="", flush=True)
    except Exception as e:
        print("Page load FAILED")
        result["reason"] = "Could not load withdrawal page"
        result["ui_msg"] = str(e)
        return result

    # ── Step 3: Capture any banner shown before touching the form ─────────────
    pre_alerts = read_ui_alerts(page)
    if pre_alerts:
        print(f"\n         ⚠  Site says: \"{pre_alerts}\"")
        # Time restriction — abort flag so main() stops all remaining accounts
        if "allowed only between" in pre_alerts.lower() or "not allowed" in pre_alerts.lower():
            result["status"]       = "skipped"
            result["reason"]       = "Outside withdrawal window"
            result["ui_msg"]       = pre_alerts
            result["time_blocked"] = True
            print()
            return result

    # ── Step 4: Read balance ──────────────────────────────────────────────────
    try:
        body_text = page.inner_text("body")
        m = re.search(r"Balance\s*\$\s*([\d,]+\.?\d*)", body_text, re.IGNORECASE)
        if not m:
            # fallback: first readonly input value
            raw = page.locator("input[readonly]").first.input_value(timeout=5_000)
            m2  = re.search(r"[\d,]+\.?\d*", raw.replace("$", "").strip())
            balance = float(m2.group(0).replace(",", "")) if m2 else None
        else:
            balance = float(m.group(1).replace(",", ""))

        if balance is None:
            result["reason"] = "Could not read balance"
            result["ui_msg"] = "Balance element not found on page"
            return result
    except Exception as e:
        result["reason"] = "Balance read error"
        result["ui_msg"] = str(e)
        return result

    result["balance"] = balance
    amount = math.floor(balance)
    print(f"Balance ${balance:.2f}  │  ", end="", flush=True)

    # ── Step 5: Threshold check ───────────────────────────────────────────────
    if amount < MIN_WITHDRAWAL_USD:
        result["status"] = "skipped"
        result["reason"] = f"Balance ${balance:.2f} below minimum ${MIN_WITHDRAWAL_USD}"
        result["ui_msg"] = ""
        print(f"Below min — skipped")
        return result

    result["amount"] = amount

    if dry_run:
        result["status"] = "skipped"
        result["reason"] = "Dry run"
        result["ui_msg"] = f"Would withdraw ${amount}"
        print(f"[DRY RUN] Would withdraw ${amount}")
        return result

    # ── Step 6: Fill form ─────────────────────────────────────────────────────
    try:
        page.fill("input[placeholder='re-enter your withdrawal address']", WALLET_ADDRESS)
        page.fill("input[placeholder='Enter Amount']", str(amount))
        page.fill("input[placeholder='Enter Login Password']", SHARED_PASSWORD)
    except Exception as e:
        result["reason"] = "Form fill error"
        result["ui_msg"] = str(e)
        return result

    # ── Step 7: Submit ────────────────────────────────────────────────────────
    try:
        print(f"Submitting ${amount}... ", end="", flush=True)
        page.click("button:has-text('Submit')", timeout=10_000)
        time.sleep(3)

        post_alerts = read_ui_alerts(page)
        page_text   = page.inner_text("body")
        result["ui_msg"] = post_alerts or ""

        # Classify result by site message
        combined = (post_alerts + " " + page_text).lower()

        if any(kw in combined for kw in ["success", "submitted", "request has been", "withdrawal request"]):
            result["status"] = "success"
            result["reason"] = f"Withdrew ${amount}"
            print(f"✅  {post_alerts or 'Success'}")

        elif "allowed only between" in combined or "not allowed" in combined:
            result["status"]       = "skipped"
            result["reason"]       = "Outside withdrawal window (rejected after submit)"
            result["time_blocked"] = True
            print(f"⏭  {post_alerts}")

        elif any(kw in combined for kw in ["insufficient", "pending", "already", "invalid", "error", "failed"]):
            result["status"] = "error"
            result["reason"] = "Site rejected the request"
            print(f"❌  {post_alerts or 'Unknown error'}")

        else:
            # No recognisable signal — log raw alerts and flag as unknown
            result["status"] = "error"
            result["reason"] = "Unclear response after submit"
            print(f"❓  {post_alerts or '(no alert text found)'}")

    except Exception as e:
        result["reason"] = "Submit exception"
        result["ui_msg"] = str(e)
        print(f"❌  Exception: {e}")

    return result


# ── Main ──────────────────────────────────────────────────────────────────────

KNOWN_GROUPS = ["manav", "ranjitha", "pavana", "poornima", "others"]


def group_to_bot_key(group: str) -> str:
    """e.g. 'manav' → 'rm_daily_txns_manav_bot'"""
    return f"rm_daily_txns_{group.lower()}_bot"


def main(only_account: str = None, only_group: str = None, dry_run: bool = False):
    if not PLAYWRIGHT_AVAILABLE:
        print("playwright not installed. Run: pip install playwright && playwright install chromium")
        return
    if not SHARED_PASSWORD:
        print("RM_PASSWORD is not set.")
        return

    mapping      = _load("user_bot_mapping.json")
    name_map     = _load("user_name_mapping.json")
    rate_91_set  = load_rate_91_set()

    with open(os.path.join(_BASE, "bot_config.json")) as f:
        bot_cfg = json.load(f)

    # Filter by group (e.g. --group manav)
    if only_group:
        bot_key = group_to_bot_key(only_group)
        if bot_key not in mapping:
            print(f"Group '{only_group}' not found. Valid: {KNOWN_GROUPS}")
            return
        mapping = {bot_key: mapping[bot_key]}

    all_accounts = get_all_accounts(mapping)

    # Filter by single account (e.g. --account R553232)
    if only_account:
        all_accounts = [a for a in all_accounts if a == only_account]
        if not all_accounts:
            print(f"Account '{only_account}' not found in user_bot_mapping.json")
            return

    # username → bot_name (first bot that contains this user)
    user_to_bot: dict[str, str] = {}
    for bot_name, ids in mapping.items():
        for uid in ids:
            if uid not in user_to_bot:
                user_to_bot[uid] = bot_name

    results: list[dict] = []
    tag = "[DRY RUN] " if dry_run else ""

    scope = f"group:{only_group}" if only_group else (f"account:{only_account}" if only_account else "all accounts")
    print(f"\n{'═'*W}")
    print(f"  💸 {tag}Weekly Withdrawal — {len(all_accounts)} account(s) [{scope}]")
    print(f"  🪙  To     : {WALLET_ADDRESS}")
    print(f"  💵 Min    : ${MIN_WITHDRAWAL_USD}  |  Amount : floor(balance)")
    print(f"  🕗 Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"{'═'*W}\n")

    time_block_msg = ""   # set when site rejects due to time window

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        remaining = list(enumerate(all_accounts, 1))
        for i, username in remaining:
            display = name_map.get(username, username)
            label   = f"{display} ({username})"
            is_91   = username in rate_91_set

            # Already know we're outside the window — skip without logging in
            if time_block_msg:
                r = {
                    "status": "skipped", "amount": 0, "balance": 0.0,
                    "reason": "Skipped — withdrawal window closed",
                    "ui_msg": time_block_msg, "time_blocked": False,
                }
                r["username"]     = username
                r["display_name"] = display
                r["bot"]          = user_to_bot.get(username, "rm_daily_txns_others_bot")
                r["is_91"]        = is_91
                results.append(r)
                print(f"  [{i:02d}/{len(all_accounts):02d}] {label}  ⏭  Skipped (window closed)")
                continue

            context = browser.new_context()
            page    = context.new_page()

            print(f"  [{i:02d}/{len(all_accounts):02d}] {label}")
            print(f"         ", end="", flush=True)

            try:
                r = do_withdrawal(page, username, dry_run=dry_run)
            except Exception as e:
                r = {"status": "error", "amount": 0, "balance": 0.0,
                     "reason": "Unhandled exception", "ui_msg": str(e), "time_blocked": False}
                print(f"❌  {e}")
            finally:
                context.close()

            r["username"]     = username
            r["display_name"] = display
            r["bot"]          = user_to_bot.get(username, "rm_daily_txns_others_bot")
            r["is_91"]        = is_91
            results.append(r)

            # Print ui_msg if not already shown inline
            if r["ui_msg"] and r["status"] != "success":
                print(f"         ℹ  Site message: \"{r['ui_msg']}\"")
            print()

            # Time restriction detected — abort remaining accounts
            if r.get("time_blocked"):
                time_block_msg = r["ui_msg"] or "Site restricted: outside withdrawal window"
                print(f"  ⚠  Time restriction detected. Skipping all remaining accounts.\n")

        browser.close()

    # ── Console summary ────────────────────────────────────────────────────────
    success_list = [r for r in results if r["status"] == "success"]
    skipped_list = [r for r in results if r["status"] == "skipped"]
    error_list   = [r for r in results if r["status"] == "error"]
    total_usd    = sum(r["amount"] for r in success_list)
    total_inr    = sum(returns_inr(r["amount"], r["is_91"]) for r in success_list)

    print(f"{'═'*W}")
    print(f"  ✅ Success : {len(success_list):>3}   ⏭  Skipped : {len(skipped_list):>3}   ❌ Errors : {len(error_list):>3}")
    print(f"  💵 Total withdrawn : ${total_usd:,}  (₹{total_inr:,.2f})")
    if error_list:
        print(f"\n  ❌ Failed accounts:")
        for r in error_list:
            print(f"     • {r['display_name']} ({r['username']})")
            print(f"       Reason  : {r['reason']}")
            if r["ui_msg"]:
                print(f"       Site msg: {r['ui_msg']}")
    print(f"{'═'*W}\n")

    # ── Telegram — single withdrawal bot, grouped by user mapping ────────────
    WITHDRAW_BOT = "rm_withdraw_saturday_manav_bot"

    # Group name label from bot key, e.g. rm_daily_txns_manav_bot → Manav
    def group_label(bot_key: str) -> str:
        part = bot_key.replace("rm_daily_txns_", "").replace("_bot", "")
        return part.capitalize()

    # Group results by their original daily-bot mapping
    group_results: dict[str, list] = defaultdict(list)
    for r in results:
        group_results[r["bot"]].append(r)

    # Build one section per group
    sections = []
    for group_bot, group_res in group_results.items():
        header = f"👥 *{group_label(group_bot)}*"
        account_lines = []
        for r in group_res:
            icon = {"success": "✅", "skipped": "⏭", "error": "❌"}.get(r["status"], "❓")
            name = f"{r['display_name']} ({r['username']})"
            rate = "@91rs" if r["is_91"] else "@97rs"

            if r["status"] == "success":
                inr = returns_inr(r["amount"], r["is_91"])
                account_lines.append(
                    f"  {icon} *{name}* _{rate}_\n"
                    f"     Balance ${r['balance']:.2f} → Withdrew *${r['amount']}* (₹{inr:,.2f})"
                )
            elif r["status"] == "skipped":
                bal_str  = f"\n     Balance ${r['balance']:.2f}" if r["balance"] else ""
                site_str = f"\n     Site: _{r['ui_msg']}_"       if r["ui_msg"]  else ""
                account_lines.append(
                    f"  {icon} *{name}* _{rate}_"
                    f"{bal_str}"
                    f"\n     Reason: {r['reason']}"
                    f"{site_str}"
                )
            else:  # error
                site_str = f"\n     Site: _{r['ui_msg']}_" if r["ui_msg"] else ""
                account_lines.append(
                    f"  {icon} *{name}* _{rate}_\n"
                    f"     Balance ${r['balance']:.2f}\n"
                    f"     Error: {r['reason']}"
                    f"{site_str}"
                )

        g_usd = sum(r["amount"] for r in group_res if r["status"] == "success")
        g_inr = sum(returns_inr(r["amount"], r["is_91"]) for r in group_res if r["status"] == "success")
        subtotal = f"\n  _Subtotal: ${g_usd:,} (₹{g_inr:,.2f})_" if g_usd else ""
        sections.append(header + "\n" + "\n\n".join(account_lines) + subtotal)

    tg_tag      = "\\[DRY RUN\\] " if dry_run else ""
    time_notice = (
        f"\n\n🚫 *Withdrawal window closed*\n_{time_block_msg}_\n"
        f"_Remaining accounts were skipped automatically._"
    ) if time_block_msg else ""

    msg = (
        f"💸 *{tg_tag}Weekly Withdrawal Summary*\n"
        f"_{datetime.now().strftime('%d %b %Y, %I:%M %p IST')}_\n\n"
        + "\n\n".join(sections)
        + time_notice
        + f"\n\n━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 *Total Withdrawn:* ${total_usd:,} (₹{total_inr:,.2f})\n"
        f"✅ {len(success_list)}  ⏭ {len(skipped_list)}  ❌ {len(error_list)}"
    )
    send_to_bot(WITHDRAW_BOT, bot_cfg, msg)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Automated weekly withdrawal — richmakers.space")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, do not submit")
    parser.add_argument("--account", help="Single R-ID (e.g. R523341)")
    parser.add_argument("--group",   help=f"Group name: {KNOWN_GROUPS}")
    args = parser.parse_args()
    main(only_account=args.account, only_group=args.group, dry_run=args.dry_run)
