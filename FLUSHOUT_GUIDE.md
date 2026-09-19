# Flushout Check & Daily Enhancements

## Summary of Changes

### 1. ✅ New Command: `flushout_check.py`
Identifies accounts where remaining balance will run out in <15 days based on daily credit rate.

**Key Features:**
- Analyzes remaining balance vs. average daily credit
- Detects false positives (e.g., one-time referral bonuses)
- Flags with caveat if credit spike ≥$50 detected
- Shows only alerts (accounts needing action)
- Per-group Telegram reporting

**Usage:**
```bash
# Check all accounts (except manjula)
python3 flushout_check.py --group all

# Check specific group
python3 flushout_check.py --group pavana
python3 flushout_check.py --group manav

# Check single account
python3 flushout_check.py --account R523341

# Check all accounts including manjula
python3 flushout_check.py
```

**Output Example:**
```
⚠️ Flushout Alert — Pavana
The following account(s) have <15 days of remaining balance:

🚨 Pavana User (R615541) @97rs
   Remaining: $50.00 (₹4,650.00)
   Daily Credit: $5.00 (₹465.00)
   Days Until Flushout: 10.0 days

🚨 Another User (R524656) @97rs
   Remaining: $75.00 (₹6,975.00)
   Daily Credit: $3.00 (₹279.00)
   Days Until Flushout: 25.0 days
   ⚠️ POSSIBLE FALSE POSITIVE (credit history: $3.00 | $3.00 | $50.00)
   Check if new ID activation or referral bonus applied
```

### 2. ✅ Enhanced Daily Command (main.py)

**New Features:**
- Now captures and displays `Total Rewards`
- Scrapes last 3 credit transactions (for history tracking)
- Supports `--exclude-manjula` flag

**Usage:**
```bash
# Standard daily report (all groups)
python3 main.py

# All groups except manjula
python3 main.py --exclude-manjula

# Specific bot only
python3 main.py --bot rm_daily_txns_pavana_bot
```

**What Changed:**
- Added `total_rewards` to account details
- Added `credit_history` (list of last 3 credits) for flushout calculations
- New Total Rewards line in Telegram report

### 3. ✅ New Command: `rich_status_check.py`
Analyzes Power Leg structure and suggests actions to reach the next Rich status.

**Key Features:**
- Extracts Power Leg, 2nd Power Leg, Other Legs from Income Reports
- Calculates total business value and current Rich status
- Shows multiple suggestions to reach next Rich status:
  - **Minimum approach**: Add to weakest leg only
  - **Balanced approach**: Maintain 40-30-30 ratio while growing
- Per-group Telegram reporting
- Excludes manjula from "all" by default

**Rich Status Levels:**
| Level | Threshold | Business Value |
|-------|-----------|-----------------|
| Rich1 | $10,000 | 10 Lakhs |
| Rich2 | $30,000 | 30 Lakhs |
| Rich3 | $90,000 | 90 Lakhs |
| Rich4 | $250,000 | 2.5 CR |
| Rich5 | $750,000 | 7.5 CR |
| Rich6 | $2,500,000 | 25 CR |
| Rich7 | $7,500,000 | 75 CR |

**Leg Requirements (must be met proportionally):**
- Power Leg: ≥ 40% of total business
- 2nd Power Leg: ≥ 30% of total business
- Other Legs: ≥ 30% of total business (combined from 3rd leg onwards)

**Usage:**
```bash
# Check all accounts (except manjula)
python3 rich_status_check.py --group all

# Check specific group
python3 rich_status_check.py --group pavana
python3 rich_status_check.py --group manav

# Check single account
python3 rich_status_check.py --account R523341

# Check all accounts including manjula
python3 rich_status_check.py
```

**Output Example:**
```
💎 Rich Status Report — Pavana
18 Sep 2026, 10:30 AM IST

💎 Pavana User (R615541) @97rs
  Status: Rich1 → Rich2
  Total Business: $25,000.00 (₹2,325,000.00)

  📊 Leg Breakdown:
    • Power Leg (40%): $12,000.00 (₹1,116,000.00)
    • 2nd Power Leg (30%): $8,000.00 (₹744,000.00)
    • Other Legs (30%): $5,000.00 (₹465,000.00)

  🎯 To reach Rich2 ($30,000.00):
  ⚡ Minimum Approach  (Total needed: $5,000.00)
    Power Leg: +$0.00
    2nd Power Leg: +$0.00
    Other Legs: +$5,000.00

  ⚖️ Balanced Approach (40-30-30)  (Total needed: $5,000.00)
    Power Leg: +$1,200.00 (Target: $12,000.00)
    2nd Power Leg: +$900.00 (Target: $9,000.00)
    Other Legs: +$2,900.00 (Target: $9,000.00)
```

### 4. ✅ Enhanced Withdraw Command (withdraw.py)

**New Features:**
- Supports `--group all` (all accounts except manjula)
- Supports `--exclude-manjula` flag

**Usage:**
```bash
# Weekly withdrawal for all groups (except manjula)
python3 withdraw.py --group all

# Single group
python3 withdraw.py --group pavana

# All groups including manjula
python3 withdraw.py

# Dry run
python3 withdraw.py --group all --dry-run
```

### 5. ✅ Manjula Group Handling

**Default Behavior:**
- `daily all` → excludes manjula (but you must use `--exclude-manjula` or `--group all` syntax)
- `flushout_check --group all` → excludes manjula
- `withdraw.py --group all` → excludes manjula
- Manjula can be checked separately: `flushout_check.py` with no flags includes everyone

**Individual Manjula Checks:**
```bash
# Check only manjula flushout
python3 flushout_check.py --group manjula

# Manjula daily report
python3 main.py --bot rm_daily_txns_manjula_bot
```

## False Positive Detection Logic

When a flushout alert is triggered, the system checks if it's a false positive:

```
False Positive = any credit transaction differs from others by ≥ $50
```

**Example:**
- Credit history: [$5.00, $5.00, $55.00]
- Difference: $55 - $5 = $50 ✓ Triggers caveat
- Shows: "⚠️ POSSIBLE FALSE POSITIVE (credit history: $5.00 | $5.00 | $55.00)"
- Action: User should verify if new ID activation or referral bonus was applied

## Configuration

All scripts use these environment variables (set as repo secrets):
- `RM_LOGIN_URL`: Login endpoint (default: `https://app.richmakers.space`)
- `RM_DASHBOARD_URL`: Dashboard endpoint (for token extraction)
- `RM_PASSWORD`: Shared password for all accounts

**Account Name Resolution:**
- Scripts fetch account names dynamically from the website
- URL: `https://app.richmakers.space/member/70724b7875394773/70724b347264476365715761644b79576f3559253344`
- Section: "Your Personal Information" → "Your Name"
- Fallback: Uses `user_name_mapping.json` if website fetch fails
- This means `user_name_mapping.json` is now optional (but kept for backup)

## JSON Mapping Files

Scripts use these configuration files:
- `user_bot_mapping.json`: Maps bot names to R-ID lists (required)
- `user_name_mapping.json`: Maps R-ID to display names (optional, used as fallback)
- `user_rate_mapping.json`: Maps R-ID to rate (91 or 97) (required)
- `bot_config.json`: Telegram bot credentials (required)

**Note:** Account names are now fetched directly from the website. The `user_name_mapping.json` file is kept as a fallback in case the website fetch fails.

**Groups Defined:**
- `ranjitha_bot`
- `poornima_bot`
- `pavana_bot`
- `manav_bot`
- `vasu_bot`
- `manjula_bot` (separate, not in "all" group)
- `others_bot` (fallback)

## Data Scraped per Account

| Field | Source | Used By |
|-------|--------|---------|
| `user_id` | Dashboard | All |
| `rank` | Dashboard | Daily |
| `active_investment` | Card | Daily |
| `total_rewards` | Card | Daily, Flushout |
| `e_wallet_balance` | Card | Daily |
| `remaining` | Card | Daily, Flushout |
| `recent_credit_amount` | Transaction row 1 | Daily, Flushout |
| `credit_history` | Transaction rows 1-3 | Flushout (false positive detection) |

## Telegram Bot Message Format

**Daily Report:**
- Per-account details with INR conversions
- Totals per group
- Sent to group bot + others_bot

**Flushout Alert:**
- Only accounts with <15 days remaining
- Daily credit and remaining balance
- Days until flushout
- False positive caveat if detected
- Sent to group bot + others_bot

**Withdrawal Report:**
- Per-account success/error/skip status
- Amount withdrawn with INR
- Sent to group bot + others_bot

## Error Handling

| Error | Behavior |
|-------|----------|
| Cannot read remaining | Flushout shows ❌ error |
| Cannot read credit history | Flushout shows ❌ error |
| Daily credit = $0 | Flushout shows ❌ error |
| Login fails | All three scripts handle gracefully |
| Network timeout | Retries with exponential backoff |

## Examples

### Check all accounts for flushout (pavana group only)
```bash
python3 flushout_check.py --group pavana
```
Output: Only alerts, sent to pavana_bot and others_bot

### Check manav group daily status
```bash
python3 main.py --bot rm_daily_txns_manav_bot
```
Output: Full daily report with total rewards

### Check all groups for withdrawal (except manjula)
```bash
python3 withdraw.py --group all --dry-run
```
Output: Preview amounts without submitting

### Check Rich status progression (all groups except manjula)
```bash
python3 rich_status_check.py --group all
```
Output: Shows current Rich status and multiple suggestions to reach next level

### Check Rich status for specific group
```bash
python3 rich_status_check.py --group pavana
```
Output: Pavana members' status with Power Leg analysis

### Check manjula separately
```bash
# Manjula flushout check
python3 flushout_check.py --group manjula

# Manjula daily report
python3 main.py --bot rm_daily_txns_manjula_bot

# Manjula withdrawal
python3 withdraw.py --group manjula

# Manjula rich status
python3 rich_status_check.py --group manjula
```

## Scheduling

Suggested automation (GitHub Actions):
- **Daily Report:** Every day at 8:00 AM IST
  ```bash
  python3 main.py --exclude-manjula
  python3 main.py --bot rm_daily_txns_manjula_bot  # Run separately
  ```
- **Weekly Withdrawal:** Every Saturday at 8:00 AM IST
  ```bash
  python3 withdraw.py --group all
  python3 withdraw.py --group manjula  # Run separately
  ```
- **Flushout Check:** Every Monday at 10:00 AM IST (or on-demand)
  ```bash
  python3 flushout_check.py --group all
  python3 flushout_check.py --group manjula  # Run separately
  ```
- **Rich Status Check:** Every week (e.g., Wednesday at 10:00 AM IST) or on-demand
  ```bash
  python3 rich_status_check.py --group all
  python3 rich_status_check.py --group manjula  # Run separately
  ```
