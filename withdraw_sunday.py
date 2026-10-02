"""
Sunday automated withdrawal script for richmakers.space.
Runs every Sunday at 08:00 IST via GitHub Actions (only if Saturday had accounts with > $50 remaining).

Only processes accounts flagged by Saturday's withdrawal script.

For each flagged account:
  1. Logs in via Playwright
  2. Navigates directly to the withdrawal page
  3. Reads current balance
  4. Skips if floor(balance) < MIN_WITHDRAWAL_USD (default $50 for Sunday)
  5. Submits withdrawal of floor(balance) to the shared wallet address
  6. Captures exact UI message (alert/toast) as the result reason
  7. Sends per-bot Telegram summary

Usage:
    python withdraw_sunday.py           # process pending accounts
    python withdraw_sunday.py --dry-run # preview only, no form submission
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
PERSONAL_INFO_URL   = "https://app.richmakers.space/member/70724b7875394773/70724b347264476365715761644b79576f3559253344"
SHARED_PASSWORD     = os.getenv("RM_PASSWORD", "")
MIN_WITHDRAWAL_USD  = 50   # skip if floor(balance) < this (Sunday minimum)
MAX_WITHDRAWAL_USD  = 1000 # cap single withdrawal at this amount

_BASE = os.path.dirname(os.path.abspath(__file__))

W = 64   # console width


# ── INR conversion (mirrors main.py & withdraw.py) ────────────────────────────

def returns_inr(usd: float, is_91: bool) -> float:
    """E-wallet withdrawals use the returns rate (7% tax deducted)."""
    return round(usd * (91 if is_91 else 97) * 0.93, 2)


# ── JSON loaders ──────────────────────────────────────────────────────────────

def _load(filename) -> dict:
    path = os.path.join(_BASE, filename)
    with open(path) as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


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
    """Sends *message* via the named bot using its env-var credentials."""
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


# ── Withdrawal logic ──────────────────────────────────────────────────────────

def do_withdrawal(page, username: str, remaining_balance: float, dry_run: bool = False) -> dict:
    """Performs withdrawal for a specific account.

    Sunday withdrawal logic:
    - Amount = remaining_balance - recent_day_credit
    - This preserves the daily credit for next Saturday's accumulation
    - Only withdraw if amount >= MIN ($50)
    """
    # Login
    page.goto(LOGIN_URL)
    page.fill("input[name='user_id']", username)
    page.fill("input[name='password']", SHARED_PASSWORD)
    page.click("button[type='submit']")
    page.wait_for_url("https://app.richmakers.space/member/6e4c797573632532425a6f4a77253344/6e62756c736463253344")
    time.sleep(1)

    # Navigate to withdrawal page
    page.goto(WITHDRAWAL_URL)
    page.wait_for_load_state("domcontentloaded", timeout=15_000)
    time.sleep(1)

    # Get current balance and recent credit from page
    try:
        balance_text = page.text_content(".balance-display, .wallet-balance, [data-balance]") or ""
        balance_match = re.search(r"\$?([\d,]+\.?\d*)", balance_text)
        current_balance = float(balance_match.group(1).replace(",", "")) if balance_match else remaining_balance
    except:
        current_balance = remaining_balance

    # Extract recent daily credit (for preserving next Saturday's accumulation)
    recent_credit = 0.0
    try:
        # Try to find recent credit amount on the page
        credit_text = page.text_content(".credit-display, .daily-credit, [data-credit]") or ""
        credit_match = re.search(r"\$?([\d,]+\.?\d*)", credit_text)
        if credit_match:
            recent_credit = float(credit_match.group(1).replace(",", ""))
    except:
        recent_credit = 0.0

    # Sunday withdrawal logic: remaining - daily_credit (preserve credit for next Saturday)
    withdrawal_amount = current_balance - recent_credit

    # Determine final withdrawal amount
    if withdrawal_amount < MIN_WITHDRAWAL_USD:
        return {
            "status": "skipped",
            "balance": current_balance,
            "amount": 0,
            "reason": f"Amount to withdraw ${withdrawal_amount:.2f} < min ${MIN_WITHDRAWAL_USD} (after preserving ${recent_credit:.2f} daily credit)",
            "ui_msg": "",
            "time_blocked": False,
            "recent_credit": recent_credit,
        }

    # Amount to withdraw: min of (adjusted_amount, max cap)
    final_withdraw_amount = min(math.floor(withdrawal_amount), MAX_WITHDRAWAL_USD)

    if dry_run:
        return {
            "status": "success",
            "balance": current_balance,
            "amount": final_withdraw_amount,
            "reason": "[DRY RUN] Would withdraw",
            "ui_msg": "[DRY RUN] Form not submitted",
            "time_blocked": False,
            "recent_credit": recent_credit,
        }

    # Fill and submit withdrawal form
    try:
        page.fill("input[name='amount']", str(int(final_withdraw_amount)))
        page.fill("input[name='wallet_address']", WALLET_ADDRESS)
        page.click("button[type='submit'], button:has-text('Withdraw')")

        # Wait for response/alert
        try:
            page.wait_for_selector(".alert, [role='alert']", timeout=5_000)
            ui_msg = page.text_content(".alert, [role='alert']") or ""
        except:
            ui_msg = "Withdrawal submitted (no confirmation detected)"

        # Assume success if no explicit error
        if "error" not in ui_msg.lower() and "failed" not in ui_msg.lower():
            return {
                "status": "success",
                "balance": current_balance,
                "amount": final_withdraw_amount,
                "reason": "Success",
                "ui_msg": ui_msg,
                "time_blocked": False,
                "recent_credit": recent_credit,
            }
        else:
            return {
                "status": "skipped",
                "balance": current_balance,
                "amount": 0,
                "reason": "Site error/unavailable",
                "ui_msg": ui_msg,
                "time_blocked": "outside" in ui_msg.lower() or "window" in ui_msg.lower(),
                "recent_credit": recent_credit,
            }

    except Exception as e:
        return {
            "status": "error",
            "balance": current_balance,
            "amount": 0,
            "reason": str(e),
            "ui_msg": "",
            "time_blocked": False,
            "recent_credit": recent_credit,
        }


# ── Main ──────────────────────────────────────────────────────────────────────

def main(dry_run: bool = False):
    if not PLAYWRIGHT_AVAILABLE:
        print("Error: playwright is not installed. Run: pip install playwright && playwright install chromium")
        return

    if not SHARED_PASSWORD:
        print("Error: RM_PASSWORD secret is not set.")
        return

    # Load pending Sunday withdrawals
    pending_file = os.path.join(_BASE, "pending_sunday_withdrawals.json")
    if not os.path.exists(pending_file):
        print("No pending Sunday withdrawals found.")
        return

    with open(pending_file) as f:
        pending_accounts = json.load(f)

    if not pending_accounts:
        print("Pending Sunday withdrawals file is empty.")
        return

    bot_cfg = _load("bot_config.json")
    name_map = _load("user_name_mapping.json")

    print(f"\n{'═'*W}")
    print(f"  💸 Sunday Withdrawal — {len(pending_accounts)} account(s) pending")
    print(f"  🕗 {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"{'═'*W}\n")

    results = []
    time_block_msg = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for i, acc in enumerate(pending_accounts, 1):
            username = acc["username"]
            display = acc["display_name"]
            remaining = acc["remaining_balance"]

            label = f"{display} ({username})"
            print(f"  [{i:02d}/{len(pending_accounts):02d}] {label}")
            print(f"         ", end="", flush=True)

            context = browser.new_context()
            page = context.new_page()

            try:
                r = do_withdrawal(page, username, remaining, dry_run=dry_run)
            except Exception as e:
                r = {"status": "error", "amount": 0, "balance": 0.0,
                     "reason": "Unhandled exception", "ui_msg": str(e), "time_blocked": False}
                print(f"❌  {e}")
            finally:
                context.close()

            r["username"] = username
            r["display_name"] = display
            r["bot"] = acc.get("bot", "rm_daily_txns_others_bot")
            r["is_91"] = acc.get("is_91", False)
            results.append(r)

            # Print status
            if r["status"] == "success":
                print(f"✅  Withdrew ${r['amount']:.2f}")
            elif r["status"] == "skipped":
                print(f"⏭  {r['reason']}")
            else:
                print(f"❌  {r['reason']}")

            if r.get("time_blocked"):
                time_block_msg = r["ui_msg"]
                print(f"  ⚠  Time restriction detected. Skipping remaining accounts.\n")
                break

            print()

        browser.close()

    # ── Console summary ────────────────────────────────────────────────────────
    success_list = [r for r in results if r["status"] == "success"]
    skipped_list = [r for r in results if r["status"] == "skipped"]
    error_list = [r for r in results if r["status"] == "error"]
    total_usd = sum(r["amount"] for r in success_list)
    total_inr = sum(returns_inr(r["amount"], r["is_91"]) for r in success_list)

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

    # ── Telegram — send per-group messages ────────────────────────────────────
    def group_label(bot_key: str) -> str:
        part = bot_key.replace("rm_daily_txns_", "").replace("_bot", "")
        return part.capitalize()

    group_results: dict[str, list] = defaultdict(list)
    for r in results:
        group_results[r["bot"]].append(r)

    tg_tag = "\\[DRY RUN\\] " if dry_run else ""
    time_notice = (
        f"\n\n🚫 *Withdrawal window closed*\n_{time_block_msg}_\n"
        f"_Remaining accounts were skipped automatically._"
    ) if time_block_msg else ""

    for group_bot, group_res in group_results.items():
        success_count = len([r for r in group_res if r["status"] == "success"])
        header = f"👥 *{group_label(group_bot)}* ({success_count} account{'s' if success_count != 1 else ''})"
        account_lines = []

        for r in group_res:
            icon = {"success": "✅", "skipped": "⏭", "error": "❌"}.get(r["status"], "❓")
            name = f"{r['display_name']} ({r['username']})"
            rate = "@91rs" if r["is_91"] else "@97rs"

            if r["status"] == "success":
                inr = returns_inr(r["amount"], r["is_91"])
                recent_credit = r.get("recent_credit", 0)
                credit_note = f" (${recent_credit:.2f} daily credit preserved)" if recent_credit > 0 else ""
                account_lines.append(
                    f"  {icon} *{name}* _{rate}_\n"
                    f"     Balance: ${r['balance']:.2f} → Withdrew *${r['amount']}*{credit_note} (₹{inr:,.2f})"
                )
            elif r["status"] == "skipped":
                bal_str = f"\n     Balance ${r['balance']:.2f}" if r["balance"] else ""
                site_str = f"\n     Site: _{r['ui_msg']}_" if r["ui_msg"] else ""
                account_lines.append(
                    f"  {icon} *{name}* _{rate}_"
                    f"{bal_str}"
                    f"\n     Reason: {r['reason']}"
                    f"{site_str}"
                )
            else:
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

        group_msg = (
            f"💸 *{tg_tag}Sunday Withdrawal — {group_label(group_bot)}*\n"
            f"_{datetime.now().strftime('%d %b %Y, %I:%M %p IST')}_\n\n"
            + header + "\n" + "\n\n".join(account_lines) + subtotal
            + time_notice
        )

        # Send to group's own bot
        send_to_bot(group_bot, bot_cfg, group_msg)
        # Poornima group: also send to manav and others bots
        if group_bot == "rm_daily_txns_poornima_bot":
            send_to_bot("rm_daily_txns_manav_bot", bot_cfg, group_msg)
            send_to_bot("rm_daily_txns_others_bot", bot_cfg, group_msg)
        # Other groups: send to others bot
        elif group_bot != "rm_daily_txns_others_bot":
            send_to_bot("rm_daily_txns_others_bot", bot_cfg, group_msg)

    # ── Clean up pending file after processing ─────────────────────────────────
    try:
        os.remove(pending_file)
        print(f"Cleaned up pending_sunday_withdrawals.json\n")
    except Exception as e:
        print(f"Warning: Could not delete pending file: {e}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sunday withdrawal for pending accounts — richmakers.space")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, do not submit")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
