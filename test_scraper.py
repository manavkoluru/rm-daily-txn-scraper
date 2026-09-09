"""
Integration tests — drives real Chrome via CDP to hit richmakers.space.
No mocking. No playwright. Uses the system Chrome + CDP over WebSocket.

Run:
    RM_PASSWORD=<password> pytest test_scraper.py -v -s

Output also written to telegram_preview.txt.
"""

import json
import os
import re
import subprocess
import tempfile
import time
from collections import defaultdict
from datetime import datetime

import pytest
import requests
import websocket
from bs4 import BeautifulSoup

# ── Constants ──────────────────────────────────────────────────────────────────

BASE = os.path.dirname(os.path.abspath(__file__))
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CDP_PORT = 9223
LOGIN_URL = "https://app.richmakers.space"
DASHBOARD_URL = "https://app.richmakers.space/member/6e4c797573632532425a6f4a77253344/6e62756c736463253344"
OUTPUT_FILE = os.path.join(BASE, "telegram_preview.txt")
DIVIDER = "=" * 64


# ── INR conversion (mirrors main.py) ──────────────────────────────────────────

def investment_inr(usd: float, is_91: bool) -> float:
    """No tax. 91rs: 94 INR/USD. 97rs: 100 INR/USD."""
    return round(usd * (94 if is_91 else 100), 2)


def returns_inr(usd: float, is_91: bool) -> float:
    """7% tax deducted. 91rs: 91*0.93. 97rs: 97*0.93."""
    return round(usd * (91 if is_91 else 97) * 0.93, 2)


def inr_bracket(amount_inr: float) -> str:
    return f"(₹{amount_inr:,.2f})"


# ── Minimal CDP client ─────────────────────────────────────────────────────────

class CDP:
    """Thin synchronous CDP client over a websocket."""

    def __init__(self, ws_url: str):
        self._ws = websocket.create_connection(ws_url, timeout=7)
        self._id = 0

    def send(self, method: str, params: dict = None) -> dict:
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        self._ws.send(json.dumps(msg))
        while True:
            raw = self._ws.recv()
            data = json.loads(raw)
            if data.get("id") == self._id:
                return data.get("result", {})

    def wait_for_load(self, timeout: int = 20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = self.send("Runtime.evaluate", {"expression": "document.readyState"})
            if result.get("result", {}).get("value") == "complete":
                return
            time.sleep(0.5)
        raise TimeoutError("Page did not finish loading")

    def get_html(self) -> str:
        result = self.send("Runtime.evaluate", {
            "expression": "document.documentElement.outerHTML"
        })
        return result.get("result", {}).get("value", "")

    def js(self, expr: str):
        return self.send("Runtime.evaluate", {"expression": expr, "awaitPromise": True})

    def navigate(self, url: str):
        self.send("Page.navigate", {"url": url})
        self.wait_for_load()

    def close(self):
        self._ws.close()


# ── Chrome lifecycle ───────────────────────────────────────────────────────────

class ChromeDriver:
    """Launches headless Chrome, opens a tab, returns a CDP client."""

    def __init__(self):
        self._profile = tempfile.mkdtemp(prefix="chrome-rm-")
        self._proc = None
        self._cdp: CDP | None = None

    def start(self):
        subprocess.run(
            ["pkill", "-f", f"remote-debugging-port={CDP_PORT}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        time.sleep(1)

        self._proc = subprocess.Popen(
            [
                CHROME,
                f"--remote-debugging-port={CDP_PORT}",
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--remote-allow-origins=*",
                f"--user-data-dir={self._profile}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(30):
            try:
                tabs = requests.get(f"http://localhost:{CDP_PORT}/json", timeout=2).json()
                page_tabs = [t for t in tabs if t.get("type") == "page"]
                if page_tabs:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("Chrome CDP did not start in time")

        ws_url = page_tabs[0]["webSocketDebuggerUrl"]
        self._cdp = CDP(ws_url)
        self._cdp.send("Page.enable")
        return self._cdp

    def stop(self):
        if self._cdp:
            self._cdp.close()
        if self._proc:
            self._proc.terminate()
            self._proc.wait(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()


# ── Scraping ───────────────────────────────────────────────────────────────────

def scrape_account(cdp: CDP, username: str, password: str) -> dict:
    """Logs in and returns parsed dashboard data for one account."""
    cdp.navigate(LOGIN_URL)
    time.sleep(2)

    cdp.js(f"""
        (function() {{
            var u = document.querySelector("input[name='user_id']");
            var p = document.querySelector("input[name='password']");
            var nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            nativeInputValueSetter.call(u, {json.dumps(username)});
            u.dispatchEvent(new Event('input', {{ bubbles: true }}));
            u.dispatchEvent(new Event('change', {{ bubbles: true }}));
            nativeInputValueSetter.call(p, {json.dumps(password)});
            p.dispatchEvent(new Event('input', {{ bubbles: true }}));
            p.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }})()
    """)
    time.sleep(0.5)
    cdp.js("document.querySelector(\"button[type='submit']\").click()")

    deadline = time.time() + 7
    current_url = LOGIN_URL
    while time.time() < deadline:
        url_result = cdp.js("window.location.href")
        current_url = url_result.get("result", {}).get("value", "")
        print(f"    → {current_url[:90]}")
        if current_url.rstrip("/") != LOGIN_URL.rstrip("/"):
            break
        time.sleep(1.5)
    else:
        html = cdp.get_html()
        with open(os.path.join(BASE, "debug_login.html"), "w") as f:
            f.write(html)
        raise RuntimeError(
            f"Login did not redirect after 7s. Still at: {current_url}\n"
            "Page HTML saved to debug_login.html"
        )

    dash_deadline = time.time() + 20
    while time.time() < dash_deadline:
        url_result = cdp.js("window.location.href")
        current_url = url_result.get("result", {}).get("value", "")
        if "member" in current_url:
            break
        time.sleep(1)

    cdp.wait_for_load()
    time.sleep(1)
    html = cdp.get_html()
    return parse_dashboard_html(html)


def parse_dashboard_html(html: str) -> dict:
    """Mirrors main.py parse_dashboard_html — includes recent credit and remaining."""
    soup = BeautifulSoup(html, "html.parser")
    details = {}

    # 1. User ID
    user_elem = soup.find(class_="user-name")
    if user_elem:
        m = re.search(r"R\d+", user_elem.get_text())
        if m:
            details["user_id"] = m.group(0)

    # 2. Rank
    rank_elem = soup.find(class_="rank-badge")
    if rank_elem:
        details["rank"] = rank_elem.get_text(strip=True)

    # 3. Active Investment
    active_inv = soup.find(string=re.compile("Active Investment"))
    if active_inv:
        parent = active_inv.find_parent("div", class_="ms-3")
        if parent:
            m = re.search(r"\$\s*([\d,]+\.?\d*)", parent.get_text())
            if m:
                details["active_investment"] = float(m.group(1).replace(",", ""))

    # 4. Wallet balances (E-wallet only; A-wallet captured but unused in report)
    for progress in soup.find_all("div", class_="progress"):
        parent_div = progress.find_parent("div")
        if parent_div:
            label_elem = parent_div.find("p")
            val_elem = parent_div.find("h5")
            if label_elem and val_elem:
                label = label_elem.get_text(strip=True).lower()
                vm = re.search(r"\$\s*([\d,]+\.?\d*)", val_elem.get_text())
                if vm:
                    amount = float(vm.group(1).replace(",", ""))
                    if "e-wallet" in label:
                        details["e_wallet_balance"] = amount
                    elif "a-wallet" in label:
                        details["a_wallet_balance"] = amount

    # 5. Reward / Bonus cards (includes Remaining)
    for card in soup.find_all("div", class_="card"):
        h5 = card.find("h5")
        p = card.find("p")
        if h5 and p:
            vm = re.search(r"\$\s*([\d,]+\.?\d*)", h5.get_text())
            if vm:
                key = p.get_text(strip=True).lower().replace(" ", "_").rstrip("_")
                details[key] = float(vm.group(1).replace(",", ""))

    # 6. Most recent E-wallet Credit transaction
    txn_table = soup.find("h5", string=re.compile("Recent Transaction"))
    if txn_table:
        table = txn_table.find_parent("div", class_="card-body")
        if table:
            for row in table.find_all("tr"):
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
                        details["recent_credit_date"] = cells[0].get_text(strip=True).split(" ")[0]
                    break

    return details


def format_account_report(username: str, data: dict, display_name: str = "", is_91: bool = False) -> str:
    """Matches main.py format_account_report exactly."""
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


def load_json(filename):
    with open(os.path.join(BASE, filename)) as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def password():
    pwd = os.getenv("RM_PASSWORD", "")
    if not pwd:
        pytest.fail("RM_PASSWORD not set. Run: RM_PASSWORD=<password> pytest test_scraper.py -v -s")
    return pwd


@pytest.fixture(scope="session")
def bot_user_mapping():
    return load_json("user_bot_mapping.json")


@pytest.fixture(scope="session")
def name_mapping():
    return load_json("user_name_mapping.json")


@pytest.fixture(scope="session")
def rate_91_set():
    raw = load_json("user_rate_mapping.json")
    # load_json strips _comment; rate_91 is a list not a dict key — load raw
    with open(os.path.join(BASE, "user_rate_mapping.json")) as f:
        data = json.load(f)
    return set(data.get("rate_91", []))


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestConfig:
    def test_all_configs_load(self, bot_user_mapping, name_mapping):
        assert bot_user_mapping
        assert name_mapping
        all_users = {uid for ids in bot_user_mapping.values() for uid in ids}
        missing = all_users - set(name_mapping.keys())
        if missing:
            print(f"\n  ⚠ Users without display names: {missing}")
        print(f"\n  Bots    : {list(bot_user_mapping.keys())}")
        print(f"  Accounts: {len(all_users)}")


class TestSingleAccount:
    """Scrapes one account — fast smoke test."""

    def test_first_account(self, bot_user_mapping, name_mapping, rate_91_set, password):
        all_users = [uid for ids in bot_user_mapping.values() for uid in ids]
        username = all_users[0]
        display_name = name_mapping.get(username, "")
        is_91 = username in rate_91_set
        label = f"{display_name} ({username})" if display_name else username

        print(f"\n  Scraping: {label}")

        with ChromeDriver() as cdp:
            data = scrape_account(cdp, username, password)

        print(f"\n  Raw data: {data}")
        report = format_account_report(username, data, display_name, is_91)
        print(f"\n  Telegram message:\n{DIVIDER}\n{report}\n{DIVIDER}")
        assert data, f"No data parsed for {username}"


class TestFullRun:
    """Scrapes ALL accounts — shows exact per-bot Telegram messages including totals."""

    def test_all_accounts(self, bot_user_mapping, name_mapping, rate_91_set, password):
        # Unique accounts in original order
        seen = set()
        all_users = []
        for ids in bot_user_mapping.values():
            for uid in ids:
                if uid not in seen:
                    seen.add(uid)
                    all_users.append(uid)

        scraped_text: dict[str, str] = {}
        scraped_data: dict[str, dict] = {}
        failed = []

        print(f"\n  Scraping {len(all_users)} accounts...\n")

        with ChromeDriver() as cdp:
            for username in all_users:
                display_name = name_mapping.get(username, "")
                is_91 = username in rate_91_set
                label = f"{display_name} ({username})" if display_name else username
                try:
                    data = scrape_account(cdp, username, password)
                    scraped_data[username] = data
                    scraped_text[username] = f"✅ {format_account_report(username, data, display_name, is_91)}"
                    print(f"  ✅ {label}")
                except Exception as e:
                    scraped_data[username] = {}
                    scraped_text[username] = f"❌ *{label}*: Failed (`{e}`)"
                    failed.append(label)
                    print(f"  ❌ {label}: {e}")

        # Build per-bot messages with totals footer
        bot_messages = []
        for bot_name, user_ids in bot_user_mapping.items():
            lines = [scraped_text[uid] for uid in user_ids if uid in scraped_text]
            if not lines:
                continue

            total_ewallet_usd = 0.0
            total_ewallet_inr = 0.0
            total_credit_usd = 0.0
            total_credit_inr = 0.0

            for uid in user_ids:
                d = scraped_data.get(uid, {})
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
            bot_messages.append({"bot": bot_name, "message": message})
            print(f"\n{DIVIDER}\n📬 BOT: {bot_name}\n{DIVIDER}")
            print(message)
            print(DIVIDER)

        # Write preview file
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(f"Telegram Preview — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Scraped: {len(scraped_text)}  |  Failed: {len(failed)}\n\n")
            for item in bot_messages:
                f.write(f"{DIVIDER}\nBOT: {item['bot']}\n{DIVIDER}\n")
                f.write(item["message"])
                f.write(f"\n{DIVIDER}\n\n")

        print(f"\n  Preview written to: {OUTPUT_FILE}")
        if failed:
            print(f"  ⚠ Failed: {failed}")

        assert bot_messages, "No messages generated"
        assert len(failed) < len(all_users), "All accounts failed — check password / site"
