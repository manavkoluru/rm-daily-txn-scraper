import re
from bs4 import BeautifulSoup


def parse_dashboard_html(html_content: str) -> dict:
    soup = BeautifulSoup(html_content, "html.parser")

    details = {}

    # 1. Fetch User ID
    user_name_elem = soup.find(class_="user-name")
    if user_name_elem:
        match = re.search(r"R\d+", user_name_elem.get_text())
        if match:
            details["user_id"] = match.group(0)

    # 2. Fetch Current Rank
    rank_elem = soup.find(class_="rank-badge")
    if rank_elem:
        details["rank"] = rank_elem.get_text(strip=True)

    # 3. Fetch Active Investment
    active_inv_elem = soup.find(string=re.compile("Active Investment"))
    if active_inv_elem:
        parent = active_inv_elem.find_parent("div", class_="ms-3")
        if parent:
            text = parent.get_text()
            match = re.search(r"\$\s*([\d,]+\.?\d*)", text)
            if match:
                details["active_investment"] = float(
                    match.group(1).replace(",", "")
                )

    # 4. Fetch E-Wallet & A-Wallet Balances
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

    # 5. Fetch Cards (Revenue Reward, Direct Reward, Level Bonus, etc.)
    cards = soup.find_all("div", class_="card")
    for card in cards:
        h5_elem = card.find("h5")
        p_elem = card.find("p")
        if h5_elem and p_elem:
            label = p_elem.get_text(strip=True)
            val_text = h5_elem.get_text(strip=True)
            val_match = re.search(r"\$\s*([\d,]+\.?\d*)", val_text)
            if val_match:
                amount = float(val_match.group(1).replace(",", ""))
                key = label.lower().replace(" ", "_")
                details[key] = amount

    return details


if __name__ == "__main__":
    # Example usage reading the source html file
    with open("source.html", "r", encoding="utf-8") as f:
        html_data = f.read()

    dashboard_data = parse_dashboard_html(html_data)

    print("Parsed Dashboard Data:")
    for k, v in dashboard_data.items():
        print(f"  {k}: {v}")
