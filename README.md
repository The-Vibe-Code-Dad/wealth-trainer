# Wealth Trainer — Work in Progress

**Status: work in progress / experimental.** Built by The Vibe Code Dad. Features, screens, and storage migrations are still evolving. Keep backups and use for a private family learning environment; this is not a real bank, brokerage, or payment service.

A private family economy, packaged for Docker. A parent runs the bank; a child earns, saves, invests, and requests rewards. **100 Coins = $1.00 USD.** No real brokerage trades or payment processing take place.

## Start with Docker

Create your local configuration (secrets and family data are not included in this repository):

1. Copy `.env.example` to `.env`.
2. Generate a random setup token with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and set `BOOTSTRAP_TOKEN` in `.env`.
3. Run:

```sh
docker compose up -d --build
```

Open **http://localhost:8080**, enter the setup token from `.env`, and choose your parent and child names, unique usernames, and passwords. The setup screen is available only until the first family is created. There are no default account passwords or seeded Coin balances. Usernames use 3–32 letters, numbers, dots, underscores, or hyphens. If upgrading an older database that used the role selector, existing accounts receive usernames `parent` and `child` and keep their passwords. Do not commit `.env`.

Useful commands:

```sh
docker compose ps
docker compose logs --tail 100
docker compose stop
docker compose up -d --build
```

The named `wealth-data` volume stores the SQLite database. Rebuilding, restarting, and `docker compose down` preserve it. **`docker compose down -v` destroys the database volume—do not use it for normal updates.**

## What works

- Username and password sign-in with server-enforced parent and child permissions. Usernames are case-insensitive; the account role is determined by the server, never selected at login.
- Parent bank funding and transfers to children with required reasons; payments are backed by the parent bank balance.
- Immutable transaction and entry records, derived balances, balanced entries, atomic writes, and overdraft prevention.
- Chore creation, editing, pausing, child submissions, and parent approval or decline. Approvals use the reward amount captured when submitted.
- A reward store with editable prices, purchase requests, and parent approvals. Approval deducts the captured price. Pending requests do not reserve funds, so approvals recheck the current balance.
- Savings transfers, a parent-selected annual rate, daily compounding, and recurring weekly allowances.
- Stocks, ETFs, and cryptocurrency tracked using automatically fetched Yahoo quotes, simulated purchases, whole-position sales, performance, and net worth.
- Cash-outs with ledger-backed holds, parent approval, decline, and child cancellation.
- Parent management of usernames and passwords from the app.
- Investment committees with weighted allocations, including cash. Each purchase snapshots its mix; editing a committee only changes future purchases. These are buy-and-hold baskets, not automatically rebalanced funds.
- Searchable transaction history, CSV export, and compensating reversals for adjustments, transfers, interest, and allowances. Chore payouts and purchases can be corrected with explicitly labeled bank adjustments; investment positions are closed by selling.
- Responsive layout, confirmation dialogs, error feedback, and persistent server storage.

One parent and multiple children are supported. Each child has a separate wallet, savings, investment positions, ledger, chores, rewards, requests, interest rate, and allowance. The Yahoo investment catalog and committee definitions are shared across the family. Age-based feature unlocking, loans, taxes, budgeting, and an installable PWA are future extensions.

## Market data — no keys or market configuration

Yahoo Finance quotes load automatically on startup and every 15 minutes. The interface refreshes in the background every minute when you are not filling in a form. Defaults include SPY, QQQ, BND, GLD, Apple, Microsoft, Nvidia, Disney, Bitcoin, and Ethereum. A parent can optionally add USD-denominated Yahoo tickers, pause new purchases, or build committees. Existing holdings in disabled assets still receive prices and can be sold.

Prices come from Yahoo's public chart endpoint, without a Yahoo login or API key. Quotes show their actual market timestamp and may be delayed. The app caches last-good quotes and retries automatically if Yahoo is unavailable. Buying and selling pause if a quote is over five days old or a Yahoo fetch has not succeeded within one hour. Markets being closed is normal: the most recent closing quote can still be used after a successful refresh. Yahoo access is unofficial and availability is not guaranteed; the app never invents a substitute price.

Reported stock splits adjust the simulated units of holdings opened before the split. This is a price-return simulator: dividends, fees, taxes, FX, mergers, and other corporate actions are not modeled. Only USD quotes are accepted. Bitcoin and Ethereum use Yahoo's `BTC-USD` and `ETH-USD` symbols. No money is sent to Yahoo; requests include only public ticker symbols and date ranges.

## Simple parent workflow

1. Create the parent and child usernames/passwords once.
2. Choose a child using **Viewing child** at the top, then use **Add Coins** to credit earnings or rewards with a reason. The dialog names the recipient. The dropdown also switches performance, balances, history, chores, and requests.
   Add more children through **Parent bank → Family accounts → Add child**. Each child receives their own username/password and starts with zero Coins.
3. The child saves, invests, sells, and requests cash-outs independently.
4. The parent approves or declines cash-outs in **Parent bank**. Approval records the redemption; the parent pays the child outside the app.

Cash-out requests immediately hold Coins in a separate ledger account. Held Coins remain part of net worth but cannot be reinvested or spent. Approval redeems the hold; decline or child cancellation returns it to the wallet. Cash-outs use whole Coins so USD payouts have exact cents. Repeated approvals are rejected.

**Family accounts** lets the parent edit display names, usernames, or passwords after entering their current parent password. Changes revoke the edited account's sessions. Blank new-password fields keep the existing password.

Chores, store rewards, savings rates, allowances, investment availability, and committee editing remain available under **Optional controls**. They require no initial configuration to use the core bank. If you choose to use chores and reward-store requests, their approvals join the bank inbox.

## Money and scheduling rules

Storage uses integer hundredths of a Coin, never floating-point balances. One Coin is one US cent; a hundredth of a Coin is one hundredth of a US cent. Inputs allow two decimal places. Market units and prices use Decimal arithmetic; sale proceeds round to the nearest hundredth of a Coin.

Every financial operation writes offsetting ledger entries within one SQLite `BEGIN IMMEDIATE` transaction. Wallet and savings balances cannot go negative. The bank, market, and store are balancing accounts, so bank issuance is not limited by a bank reserve balance. HTTP actions require idempotency keys, and repeat approvals and sales are rejected. Database triggers prevent updating or deleting existing transactions and entries.

Interest is posted for each elapsed UTC date at `savings × APR / 365`, rounded to a hundredth of a Coin daily. The scheduler runs every minute; financial requests also settle overdue days before changing balances. This gives daily compounding and restart catch-up without duplicate payments. Interest below the rounding threshold may produce no credit. Weekly allowances are first due seven days after database creation, then every seven days. A setting of zero pauses payouts. Changing settings settles elapsed periods under the old rate first.

## Your server and Cloudflare Tunnel

The default port is bound only to `127.0.0.1`. If `cloudflared` runs on the same host, point your tunnel at `http://localhost:8080`. Set `COOKIE_SECURE=true` when using the HTTPS domain and recreate the container. With that setting, use the HTTPS address for sign-in; browsers will not send Secure cookies over ordinary HTTP LAN URLs.

If `cloudflared` runs in another container, attach it to the Compose network and use `http://wealth-trainer:8080` as the origin. `localhost` inside that container refers to the tunnel container, not this app. Configure the domain and tunnel in your Cloudflare account; this repository does not create them.

For LAN-only testing, set `BIND_ADDRESS` to the server's LAN address and leave `COOKIE_SECURE=false`. HTTP does not encrypt passwords; use an HTTPS reverse proxy for normal access. The built-in HTTP service is intended for a small private family deployment behind that proxy, not direct public exposure or a multi-tenant service. Sessions expire after 12 hours. Separate browsers, browser profiles, or devices can sign in as different accounts simultaneously. Tabs in the same browser profile share one account. Sign-in attempts are rate-limited by username and directly connected IP, so a parent login does not lock out the child. Editing an account in the parent bank clears its temporary login lockout.

The container runs as a non-root user, with a read-only application filesystem, dropped Linux capabilities, a health check, and a writable database volume. Use a local disk for SQLite, not an SMB/NFS share. Run one application replica.

## Backup and restore

Create a consistent backup while the app is running, then copy it off the Docker volume:

```sh
docker compose exec wealth-trainer python -m app.manage backup /data/backup.db
docker compose cp wealth-trainer:/data/backup.db ./wealth-backup.db
```

Keep backups private: they contain financial history and password hashes. Preserve an off-host copy. To restore, stop the service, preserve the current volume as a rollback copy, then replace its `wealth.db` with the backup using a one-off container mounting the same volume. Remove obsolete `wealth.db-wal` and `wealth.db-shm` files only while all app processes are stopped. Ensure ownership remains UID/GID 10001 before starting the app. Never copy only a live `wealth.db` file as a backup; WAL data may not yet be checkpointed.

Reset a forgotten password from the server console (this revokes that account's sessions):

```sh
docker compose exec wealth-trainer python -m app.manage reset-password parent
docker compose exec wealth-trainer python -m app.manage reset-password CHILD_USERNAME
```

## Development and tests

Python 3.14; no third-party Python or JavaScript packages are required.

```sh
python -m unittest discover -s tests -v
node --check app/static/app.js
```

The suite covers money precision, ledger immutability, balanced postings, overdrafts, role enforcement, approvals, snapshot amounts, redemptions, reversals, market gains, committee allocations, quote freshness, daily interest, recurring allowances, the mocked provider contract, authentication, CSRF, and idempotency. Tests use isolated temporary databases. Unit tests mock the market provider; live retrieval is checked separately inside Docker without keys.

For a non-Docker development run, set `BOOTSTRAP_TOKEN` and optionally `DATABASE_PATH`, then run `python -m app.server`. The default database is `data/wealth.db`. The frontend is served directly from `app/static`; reload the browser after changes.

Existing single-child databases are upgraded automatically: all existing records remain assigned to the original child without rewriting ledger entries. New children receive independent default savings settings. Child logins cannot select or modify another child’s records.


### Learning levels and family currency
In Parent bank, select a child using **Viewing child**. **Add Coins** credits that child's wallet; enter the amount and a reason. **Add child** creates another username/password account.

**Currency & learning level** lets the parent name the family currency and choose Beginner or Intermediate for the selected child. Children start on Beginner: Aggressive, Balanced (medium), Defensive, and parent-created mixes. Intermediate unlocks individual investments and SEC company-report lessons under **Learn**. Existing holdings remain sellable after switching back to Beginner. Custom mixes are available under optional investment controls.

Each purchase starts at 100 currency units per simulated share (fractional shares allowed). Its subsequent value follows the underlying market return; a 10% gain turns a starting share into 110 units. These are purchase-relative learning units, not real exchange-traded shares or a globally priced fund. Existing balances and holdings retain their value. The redemption rate remains 100 units = $1. Quotes update automatically without API keys.


### Parent monitoring and privacy
Parents open a child overview with all pages and controls directly available in the sidebar. Select a child to view or manage their account; no separate management mode is needed. Money history identifies the parent, child or system responsible. The action log records successful actions from this update onward (latest 500 displayed), including nonfinancial requests and settings changes; it excludes passwords and raw form data.

Children receive only their own financial activity, public learning/savings settings, and no administration log or family account list. Parent-entered transaction notes are replaced with neutral account-update descriptions for children. Balance changes remain visible so their account can be understood. Separate browsers/devices have separate sign-ins; tabs within the same browser share a sign-in.


### Parent bank funds
The parent sidebar includes **Chores** and **Bank funds** directly. Use **Bank funds > Add money to bank** to record money set aside, in your named currency (100 units = $1). This does not credit any child. The new bank reserve starts at zero; existing child funds are preserved.

Giving money to a child, chore rewards, allowances and savings interest deduct from this reserve. Removing money from a child returns it to the reserve. Manual payments cannot overdraw it; automatic payments wait for funding and retry. Reversals undo reserve movements for payments made under this feature. Cash-out approvals represent money paid out and do not refill the reserve. Bank history and balance are parent-only. This is bookkeeping, not a connection to a real bank account.


### Overview portfolio chart
Both overviews include an investment value chart and a per-investment breakdown (value, unrealized gain/loss, return and portfolio weight). Select a holding to isolate its chart; use Show total to reset. Ranges are 1D, 1W, 1M and All. History starts when this feature is installed; no past performance is fabricated. Observations are saved on market refresh, account actions and viewing state, at most once per 15 minutes when unchanged. Older chart data uses the last observation per day. The chart tracks invested value, so purchases and sales change the line as well as market movements.


### Goals, recurring chores and family management
- **Savings & goals:** Create a goal and move wallet funds into it. Goal funds remain in savings and earn the same interest, but cannot be withdrawn through a general transfer until released from the goal. A parent can set a 0–100% matching reward and pay it once when the goal is fully saved. Matching is paid to the wallet from the bank.
- **Chores:** Choose Whenever needed, One time, Daily or Weekly and the first due date. Scheduled chores become available on that date (America/New_York). Approval advances daily/weekly chores to the next future due date; overdue occurrences are not multiplied into multiple rewards. One-time chores pause after approval. Send back requires a note and lets the child submit the work again without being paid twice.
- **Bank funding notice:** Compares bank funds with all pending chore rewards and allowances due through the next seven days. This is a planning estimate, excluding future interest, cash-out obligations, unsubmitted chores and optional matching rewards.
- **Investment gains:** The Overview chart defaults to realized plus unrealized gains in currency units, excluding purchase/sale cash flows. Portfolio value remains available as a separate view. The percentage alongside it is total gain divided by cumulative purchase cost, not an annualized or time-weighted return.

### Automatic backup and parent recovery
Daily SQLite backups run automatically while the app is running and retain the latest 14 copies in `./backups`, mounted separately from the Docker data volume. Parent controls shows the latest successful backup, failures, and a Back up now button. Copy the folder to another device to protect against disk/computer failure. Backups contain private account data, including password hashes; keep access restricted.

To reset only the parent password locally, run `./Recover-Parent.ps1` from PowerShell in this project, or:

```powershell
docker compose exec wealth-trainer python -m app.manage recover-parent
```

Enter the new password twice. Existing parent sessions and login lockouts are cleared; usernames, balances and history remain. No recovery web endpoint or email/API setup is needed.
