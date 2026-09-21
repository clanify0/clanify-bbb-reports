import argparse
import html
import os
import smtplib
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import requests


API_BASE = "https://api.builtbybit.com/v1"
PAGE_SIZE = 20
LOCAL_TZ = ZoneInfo("Europe/Stockholm")

BBB_API_TOKEN = os.environ["BBB_API_TOKEN"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO = os.environ["EMAIL_TO"]
REPORT_START_DATE = date.fromisoformat(os.environ["REPORT_START_DATE"])

# Purchases with clearly unsuccessful/refunded states are excluded.
EXCLUDED_STATUS_WORDS = {
    "pending",
    "refunded",
    "refund",
    "chargeback",
    "charged_back",
    "cancelled",
    "canceled",
    "invalid",
    "reversed",
    "voided",
    "failed",
    "declined",
}

session = requests.Session()
session.headers.update({
    "Authorization": f"Private {BBB_API_TOKEN}",
    "Accept": "application/json",
    "User-Agent": "Clanify-BBB-Reports/1.0",
})


def api_get(path, params=None, retries=5):
    url = f"{API_BASE}{path}"

    for _ in range(retries):
        response = session.get(url, params=params, timeout=30)

        if response.status_code == 429:
            retry_ms = response.headers.get("Retry-After", "1000")
            try:
                wait_seconds = max(float(retry_ms) / 1000.0, 1.0)
            except ValueError:
                wait_seconds = 2.0
            print(f"Rate limited. Waiting {wait_seconds:.1f}s...")
            time.sleep(wait_seconds)
            continue

        response.raise_for_status()
        payload = response.json()

        if payload.get("result") != "success":
            raise RuntimeError(f"BuiltByBit API error: {payload.get('error')}")

        return payload.get("data")

    raise RuntimeError("BuiltByBit API kept rate-limiting the request.")


def paginated_get(path, sort=None, order="desc", stop_before_timestamp=None):
    results = []
    page = 1

    while True:
        params = {"page": page}

        if sort:
            params["sort"] = sort
            params["order"] = order

        batch = api_get(path, params=params)

        if not isinstance(batch, list):
            raise RuntimeError(f"Expected a list from {path}, got {type(batch).__name__}")

        if not batch:
            break

        results.extend(batch)

        if stop_before_timestamp is not None and sort == "purchase_date" and order == "desc":
            timestamps = [int(x.get("purchase_date", 0) or 0) for x in batch]
            if timestamps and min(timestamps) < stop_before_timestamp:
                break

        if len(batch) < PAGE_SIZE:
            break

        page += 1

    return results


def counts_as_sale(purchase):
    status = str(purchase.get("status", "")).strip().lower()
    return status not in EXCLUDED_STATUS_WORDS


def money_by_currency(purchases):
    totals = defaultdict(float)

    for purchase in purchases:
        currency = str(purchase.get("currency") or "USD").upper()
        totals[currency] += float(purchase.get("price") or 0)

    return dict(totals)


def format_money(amount, currency):
    if currency == "USD":
        return f"${amount:,.2f}"
    if currency == "EUR":
        return f"€{amount:,.2f}"
    if currency == "GBP":
        return f"£{amount:,.2f}"
    if currency == "SEK":
        return f"{amount:,.2f} kr"
    return f"{amount:,.2f} {currency}"


def format_money_map(values):
    if not values:
        return "$0.00"
    return " + ".join(format_money(values[c], c) for c in sorted(values))


def pct_change(current, previous):
    if previous == 0:
        return "0%" if current == 0 else "NEW"

    change = ((current - previous) / previous) * 100
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:.1f}%"


def get_period_purchases(resource_data, start_ts, end_ts):
    return [
        p for p in resource_data["purchases"]
        if counts_as_sale(p)
        and start_ts <= int(p.get("purchase_date", 0) or 0) < end_ts
    ]


def collect_data(max_period_days):
    now = datetime.now(timezone.utc)

    # Fetch two report periods so we can compare against the previous period.
    earliest = now - timedelta(days=max_period_days * 2)
    earliest_ts = int(earliest.timestamp())

    basic_resources = paginated_get("/resources/owned", sort="title", order="asc")
    print(f"Found {len(basic_resources)} owned resources.")

    resources = []
    seen_statuses = Counter()

    for index, basic in enumerate(basic_resources, start=1):
        resource_id = int(basic["resource_id"])
        print(f"[{index}/{len(basic_resources)}] Fetching {basic.get('title', resource_id)}")

        detail = api_get(f"/resources/{resource_id}")

        purchases = paginated_get(
            f"/resources/{resource_id}/purchases",
            sort="purchase_date",
            order="desc",
            stop_before_timestamp=earliest_ts,
        )

        purchases = [
            p for p in purchases
            if int(p.get("purchase_date", 0) or 0) >= earliest_ts
        ]

        for purchase in purchases:
            seen_statuses[str(purchase.get("status", "")).lower()] += 1

        resources.append({
            "resource_id": resource_id,
            "title": detail.get("title") or basic.get("title") or f"Resource {resource_id}",
            "release_date": int(detail.get("release_date", 0) or 0),
            "current_price": float(detail.get("price", 0) or 0),
            "currency": str(detail.get("currency") or basic.get("currency") or "USD").upper(),
            "lifetime_purchases": int(detail.get("purchase_count", 0) or 0),
            "downloads": int(detail.get("download_count", 0) or 0),
            "review_count": int(detail.get("review_count", 0) or 0),
            "review_average": float(detail.get("review_average", 0) or 0),
            "purchases": purchases,
        })

    print("Purchase statuses seen:", dict(seen_statuses))
    return now, resources


def build_report(period_days, now, resources):
    end = now
    current_start = now - timedelta(days=period_days)
    previous_start = now - timedelta(days=period_days * 2)

    current_start_ts = int(current_start.timestamp())
    previous_start_ts = int(previous_start.timestamp())
    end_ts = int(end.timestamp())

    rows = []
    all_current = []
    all_previous = []

    for resource in resources:
        current = get_period_purchases(resource, current_start_ts, end_ts)
        previous = get_period_purchases(resource, previous_start_ts, current_start_ts)

        all_current.extend(current)
        all_previous.extend(previous)

        rows.append({
            **resource,
            "period_sales": len(current),
            "previous_sales": len(previous),
            "period_money": money_by_currency(current),
            "previous_money": money_by_currency(previous),
            "unique_buyers": len({
                p.get("purchaser_id")
                for p in current
                if p.get("purchaser_id") is not None
            }),
        })

    current_money_total = money_by_currency(all_current)
    previous_money_total = money_by_currency(all_previous)
    currencies = set(current_money_total) | set(previous_money_total)

    # Rank by revenue when all purchases use one currency.
    # Otherwise rank by sales count so currencies are never incorrectly mixed.
    if len(currencies) <= 1:
        currency = next(iter(currencies), "USD")
        rows.sort(
            key=lambda r: (
                r["period_money"].get(currency, 0),
                r["period_sales"],
                r["lifetime_purchases"],
            ),
            reverse=True,
        )
        ranking_note = "Ranked by revenue for this period."
    else:
        rows.sort(
            key=lambda r: (r["period_sales"], r["lifetime_purchases"]),
            reverse=True,
        )
        ranking_note = (
            "Multiple currencies detected, so ranking uses sales count "
            "to avoid incorrectly combining different currencies."
        )

    unique_buyers = len({
        p.get("purchaser_id")
        for p in all_current
        if p.get("purchaser_id") is not None
    })

    previous_unique_buyers = len({
        p.get("purchaser_id")
        for p in all_previous
        if p.get("purchaser_id") is not None
    })

    new_releases = [
        r for r in resources
        if r["release_date"] and current_start_ts <= r["release_date"] < end_ts
    ]

    lifetime_purchases = sum(r["lifetime_purchases"] for r in resources)
    lifetime_downloads = sum(r["downloads"] for r in resources)

    sales_change = pct_change(len(all_current), len(all_previous))
    buyer_change = pct_change(unique_buyers, previous_unique_buyers)

    if len(currencies) == 1:
        currency = next(iter(currencies))
        revenue_change = pct_change(
            current_money_total.get(currency, 0),
            previous_money_total.get(currency, 0),
        )
    else:
        revenue_change = "—"

    row_html = []

    for rank, row in enumerate(rows, start=1):
        if row["review_count"] > 0:
            rating = f"{row['review_average']:.2f}/5 ({row['review_count']})"
        else:
            rating = "No reviews"

        row_html.append(
            "<tr>"
            f"<td style='padding:8px;border-bottom:1px solid #e5e7eb'>{rank}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #e5e7eb'>"
            f"<strong>{html.escape(row['title'])}</strong></td>"
            f"<td style='padding:8px;border-bottom:1px solid #e5e7eb;text-align:right'>"
            f"{row['period_sales']}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #e5e7eb;text-align:right'>"
            f"{html.escape(format_money_map(row['period_money']))}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #e5e7eb;text-align:right'>"
            f"{row['unique_buyers']}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #e5e7eb;text-align:right'>"
            f"{row['lifetime_purchases']}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #e5e7eb;text-align:right'>"
            f"{row['downloads']}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #e5e7eb;text-align:right'>"
            f"{html.escape(rating)}</td>"
            "</tr>"
        )

    release_names = ", ".join(html.escape(r["title"]) for r in new_releases[:10]) or "None"
    if len(new_releases) > 10:
        release_names += f" + {len(new_releases) - 10} more"

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:1050px;margin:auto;color:#111827">
      <h1 style="margin-bottom:4px">Clanify BuiltByBit — {period_days}-Day Report</h1>
      <p style="color:#6b7280;margin-top:0">
        {current_start.astimezone(LOCAL_TZ).strftime('%d %b %Y')}
        →
        {end.astimezone(LOCAL_TZ).strftime('%d %b %Y')}
      </p>

      <table style="border-collapse:collapse;width:100%;margin:20px 0">
        <tr>
          <td style="padding:14px;border:1px solid #e5e7eb">
            <strong>Revenue</strong><br>
            {html.escape(format_money_map(current_money_total))}
            <br><small>vs previous: {revenue_change}</small>
          </td>
          <td style="padding:14px;border:1px solid #e5e7eb">
            <strong>Sales</strong><br>
            {len(all_current)}
            <br><small>vs previous: {sales_change}</small>
          </td>
          <td style="padding:14px;border:1px solid #e5e7eb">
            <strong>Unique buyers</strong><br>
            {unique_buyers}
            <br><small>vs previous: {buyer_change}</small>
          </td>
          <td style="padding:14px;border:1px solid #e5e7eb">
            <strong>New releases</strong><br>
            {len(new_releases)}
          </td>
        </tr>
      </table>

      <p><strong>New releases this period:</strong> {release_names}</p>

      <p>
        <strong>Catalog:</strong>
        {len(resources)} resources ·
        {lifetime_purchases:,} lifetime purchases ·
        {lifetime_downloads:,} lifetime downloads
      </p>

      <h2>All resources — best to worst</h2>
      <p style="color:#6b7280">{ranking_note}</p>

      <div style="overflow-x:auto">
        <table style="border-collapse:collapse;width:100%;font-size:13px">
          <thead>
            <tr style="background:#f3f4f6">
              <th style="padding:8px;text-align:left">#</th>
              <th style="padding:8px;text-align:left">Resource</th>
              <th style="padding:8px;text-align:right">{period_days}d Sales</th>
              <th style="padding:8px;text-align:right">{period_days}d Revenue</th>
              <th style="padding:8px;text-align:right">Buyers</th>
              <th style="padding:8px;text-align:right">Lifetime Sales</th>
              <th style="padding:8px;text-align:right">Downloads</th>
              <th style="padding:8px;text-align:right">Rating</th>
            </tr>
          </thead>
          <tbody>{''.join(row_html)}</tbody>
        </table>
      </div>

      <p style="margin-top:24px;color:#6b7280;font-size:12px">
        Generated automatically from the BuiltByBit API.
        Obvious pending/refunded/chargeback/cancelled/invalid/failed purchases are excluded.
      </p>
    </div>
    """

    plain_body = (
        f"Clanify BuiltByBit — {period_days}-Day Report\n\n"
        f"Revenue: {format_money_map(current_money_total)}\n"
        f"Sales: {len(all_current)} ({sales_change} vs previous period)\n"
        f"Unique buyers: {unique_buyers} ({buyer_change} vs previous period)\n"
        f"New releases: {len(new_releases)}\n"
        f"Catalog: {len(resources)} resources, "
        f"{lifetime_purchases} lifetime purchases, "
        f"{lifetime_downloads} lifetime downloads\n\n"
        "Open this email in HTML view to see the complete resource ranking."
    )

    return plain_body, html_body


def send_email(period_days, plain_body, html_body):
    today_local = datetime.now(LOCAL_TZ).date().isoformat()

    msg = EmailMessage()
    msg["Subject"] = f"Clanify BuiltByBit — {period_days}-Day Report — {today_local}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = EMAIL_TO

    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)

    print(f"Sent {period_days}-day report to {EMAIL_TO}")


def scheduled_periods():
    today = datetime.now(LOCAL_TZ).date()
    elapsed = (today - REPORT_START_DATE).days

    if elapsed <= 0:
        return []

    return [
        period for period in (7, 30, 90, 365)
        if elapsed % period == 0
    ]


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--period",
        type=int,
        choices=[7, 30, 90, 365],
    )

    parser.add_argument(
        "--scheduled",
        action="store_true",
    )

    args = parser.parse_args()

    if args.period:
        periods = [args.period]
    elif args.scheduled:
        periods = scheduled_periods()
    else:
        raise SystemExit("Use --period 7|30|90|365 or --scheduled")

    if not periods:
        print("No report is due today.")
        return

    print("Reports due:", periods)

    max_period = max(periods)
    now, resources = collect_data(max_period)

    if not resources:
        raise RuntimeError("BuiltByBit returned no owned resources.")

    for period in periods:
        plain_body, html_body = build_report(period, now, resources)
        send_email(period, plain_body, html_body)


if __name__ == "__main__":
    main()
