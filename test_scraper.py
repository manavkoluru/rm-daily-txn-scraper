"""
Integration tests — drives real Chrome via CDP to hit richmakers.space.
No mocking. No playwright. Uses the system Chrome + CDP over WebSocket.

Run:
    RM_PASSWORD=Rich@959 pytest test_scraper.py -v -s

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


# ── Minimal CDP client ─────────────────────────────────────────────────────────

class CDP:
    """Thin synchronous CDP client over a websocket."""

    def __init__(self, ws_url: str):
        self._ws = websocket.create_connection(ws_url, timeout=30)
        self._id = 0

    def send(self, method: str, params: dict = None) -> dict:
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        self._ws.send(json.dumps(msg))
        # Drain events until we get our response
        while True:
            raw = self._ws.recv()
            data = json.loads(raw)
            if data.get("id") == self._id:
                return data.get("result", {})

    def wait_for_load(self, timeout: int = 20):
        """Polls document.readyState until complete."""
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
        # Kill any leftover Chrome on this port before starting
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
        # Wait for CDP to become available
        for _ in range(30):
            try:
                tabs = requests.get(f"http://localhost:{CDP_PORT}/json", timeout=2).json()
                # Pick first real page tab (type == "page")
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

    # Fill login form
    cdp.js(f"document.querySelector(\"input[name='username']\").value = {json.dumps(username)}")
    cdp.js(f"document.querySelector(\"input[name='password']\").value = {json.dumps(password)}")
    cdp.js("document.querySelector(\"button[type='submit']\").click()")

    # Wait for dashboard to load
    deadline = time.time() + 30
    while time.time() < deadline:
        url_result = cdp.js("window.location.href")
        current_url = url_result.get("result", {}).get("value", "")
        if DASHBOARD_URL in current_url:
            break
        time.sleep(1)
    else:
        html = cdp.get_html()
        raise RuntimeError(f"Dashboard not reached. Still at: {current_url[:80]}")

    cdp.wait_for_load()
    time.sleep(1)  # Let JS render fully
    html = cdp.get_html()
    return parse_dashboard_html(html)


def parse_dashboard_html(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    details = {}

    user_elem = soup.find(class_="user-name")
    if user_elem:
        m = re.search(r"R\d+", user_elem.get_text())
        if m:
            details["user_id"] = m.group(0)

    rank_elem = soup.find(class_="rank-badge")
    if rank_elem:
        details["rank"] = rank_elem.get_text(strip=True)

    active_inv = soup.find(string=re.compile("Active Investment"))
    if active_inv:
        parent = active_inv.find_parent("div", class_="ms-3")
        if parent:
            m = re.search(r"\$\s*([\d,]+\.?\d*)", parent.get_text())
            if m:
                details["active_investment"] = float(m.group(1).replace(",", ""))

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

    for card in soup.find_all("div", class_="card"):
        h5 = card.find("h5")
        p = card.find("p")
        if h5 and p:
            vm = re.search(r"\$\s*([\d,]+\.?\d*)", h5.get_text())
            if vm:
                key = p.get_text(strip=True).lower().replace(" ", "_")
                details[key] = float(vm.group(1).replace(",", ""))

    return details


def format_report(username: str, data: dict, display_name: str = "") -> str:
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


def load_json(filename):
    with open(os.path.join(BASE, filename)) as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def password():
    pwd = os.getenv("RM_PASSWORD", "")
    if not pwd:
        pytest.fail("RM_PASSWORD not set. Run: RM_PASSWORD=Rich@959 pytest test_scraper.py -v -s")
    return pwd


@pytest.fixture(scope="session")
def bot_user_mapping():
    return load_json("user_bot_mapping.json")


@pytest.fixture(scope="session")
def name_mapping():
    return load_json("user_name_mapping.json")


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

    def test_first_account(self, bot_user_mapping, name_mapping, password):
        all_users = [uid for ids in bot_user_mapping.values() for uid in ids]
        username = all_users[0]
        display_name = name_mapping.get(username, "")
        label = f"{display_name} ({username})" if display_name else username

        print(f"\n  Scraping: {label}")

        with ChromeDriver() as cdp:
            data = scrape_account(cdp, username, password)

        print(f"\n  Raw data: {data}")
        report = format_report(username, data, display_name)
        print(f"\n  Telegram message:\n{DIVIDER}\n{report}\n{DIVIDER}")
        assert data, f"No data parsed for {username}"


class TestFullRun:
    """Scrapes ALL accounts — shows exact per-bot Telegram messages."""

    def test_all_accounts(self, bot_user_mapping, name_mapping, password):
        all_users = list({uid for ids in bot_user_mapping.values() for uid in ids})
        scraped: dict[str, str] = {}
        failed = []

        print(f"\n  Scraping {len(all_users)} accounts...\n")

        with ChromeDriver() as cdp:
            for username in all_users:
                display_name = name_mapping.get(username, "")
                label = f"{display_name} ({username})" if display_name else username
                try:
                    data = scrape_account(cdp, username, password)
                    scraped[username] = f"✅ {format_report(username, data, display_name)}"
                    print(f"  ✅ {label}")
                except Exception as e:
                    scraped[username] = f"❌ *{label}*: Failed (`{e}`)"
                    failed.append(label)
                    print(f"  ❌ {label}: {e}")

        # Build per-bot messages
        bot_messages = []
        for bot_name, user_ids in bot_user_mapping.items():
            lines = [scraped[uid] for uid in user_ids if uid in scraped]
            if not lines:
                continue
            message = "📊 *Daily Automated Summary*\n\n" + "\n\n".join(lines)
            bot_messages.append({"bot": bot_name, "message": message})
            print(f"\n{DIVIDER}\n📬 BOT: {bot_name}\n{DIVIDER}")
            print(message)
            print(DIVIDER)

        # Write preview file
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(f"Telegram Preview — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Scraped: {len(scraped)}  |  Failed: {len(failed)}\n\n")
            for item in bot_messages:
                f.write(f"{DIVIDER}\nBOT: {item['bot']}\n{DIVIDER}\n")
                f.write(item["message"])
                f.write(f"\n{DIVIDER}\n\n")

        print(f"\n  Preview written to: {OUTPUT_FILE}")
        if failed:
            print(f"  ⚠ Failed: {failed}")

        assert bot_messages, "No messages generated"
        assert len(failed) < len(all_users), "All accounts failed — check password / site"
