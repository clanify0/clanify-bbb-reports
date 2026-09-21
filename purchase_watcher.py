import argparse
import html
import os
import smtplib
import time
from datetime import datetime, timezone
from email.message import EmailMessage

import requests

API_BASE = "https://api.builtbybit.com/v2"

BBB_API_TOKEN = os.environ["BBB_API_TOKEN"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO = os.environ["EMAIL_TO"]

session = requests.Session()
session.headers.update({
    "Authorization": f"Token {BBB_API_TOKEN}",
    "Accept": "application/json",
    "User-Agent": "Clanify-BBB-Purchase-Watcher/1.0",
})


def api_request(method, path, json_body=None, retries=5):
    url = f"{API_BASE}{path}"

    for _ in range(retries):
        response = session.request(method, url, json=json_body, timeout=30)

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


def get_pending_events():
    data = api_request("GET", "/events")

    if not isinstance(data, dict):
        return []

    events = data.get("events") or []
    return events if isinstance(events, list) else []


def complete_event(event_id):
    api_request(
        "POST",
        "/events/complete",
        json_body={"event_ids": [int(event_id)]},
    )


def first_value(data, *keys):
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def format_price(data):
    value = first_value(data, "final_price", "price", "purchase_price")
    currency = str(first_value(data, "currency", "purchase_currency") or "").upper()

    if value in (None, ""):
        return "Unknown"

    text = str(value).strip()

    if any(symbol in text for symbol in ("$", "€", "£")):
        return text

    try:
        amount = float(text)
    except ValueError:
        return f"{text} {currency}".strip()

    if currency == "USD" or not currency:
        return f"${amount:,.2f}"
    if currency == "EUR":
        return f"€{amount:,.2f}"
    if currency == "GBP":
        return f"£{amount:,.2f}"
    if currency == "SEK":
        return f"{amount:,.2f} kr"

    return f"{amount:,.2f} {currency}".strip()


def format_event_date(event, data):
    raw = first_value(data, "purchase_date", "date") or event.get("created_at")

    if raw in (None, ""):
        return "Unknown"

    try:
        timestamp = int(raw)
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except (TypeError, ValueError, OSError):
        return str(raw)


def send_purchase_email(event):
    data = event.get("trigger_data") or {}

    if not isinstance(data, dict):
        data = {}

    product = first_value(
        data,
        "resource_title",
        "addon_title",
        "bundle_title",
        "product_title",
        "title",
    ) or "Unknown product"

    buyer = first_value(
        data,
        "username",
        "purchaser_username",
        "buyer_username",
        "member_username",
        "purchaser_name",
    ) or "Unknown"

    price = format_price(data)
    purchase_id = first_value(data, "purchase_id", "transaction_id")
    purchase_date = format_event_date(event, data)
    resource_url = first_value(
        data,
        "resource_url",
        "addon_url",
        "bundle_url",
        "product_url",
    )

    subject = f"💰 New BuiltByBit purchase! — {product}"

    lines = [
        "💰 New BuiltByBit purchase!",
        "",
        f"Product: {product}",
        f"Price: {price}",
        f"Buyer: {buyer}",
        f"Purchase date: {purchase_date}",
    ]

    if purchase_id not in (None, ""):
        lines.append(f"Purchase ID: {purchase_id}")

    if resource_url not in (None, ""):
        lines.append(f"Product link: {resource_url}")

    plain_body = "\n".join(lines)

    product_html = html.escape(str(product))
    price_html = html.escape(str(price))
    buyer_html = html.escape(str(buyer))
    date_html = html.escape(str(purchase_date))

    extra_html = ""

    if purchase_id not in (None, ""):
        extra_html += (
            f"<p><strong>Purchase ID:</strong> "
            f"{html.escape(str(purchase_id))}</p>"
        )

    if resource_url not in (None, ""):
        safe_url = html.escape(str(resource_url), quote=True)
        extra_html += (
            f"<p><strong>Product:</strong> "
            f"<a href='{safe_url}'>Open on BuiltByBit</a></p>"
        )

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:650px;color:#111827">
      <h2 style="margin-bottom:20px">💰 New BuiltByBit purchase!</h2>
      <p><strong>Product:</strong> {product_html}</p>
      <p><strong>Price:</strong> {price_html}</p>
      <p><strong>Buyer:</strong> {buyer_html}</p>
      <p><strong>Purchase date:</strong> {date_html}</p>
      {extra_html}
    </div>
    """

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = EMAIL_TO
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)

    print(f"Sent purchase email: {product} | {price} | {buyer}")


def send_test_email():
    event = {
        "event_id": 0,
        "created_at": int(datetime.now(timezone.utc).timestamp()),
        "trigger_type": "purchase",
        "trigger_data": {
            "resource_title": "Roblox Pickaxe Pack | Drag & Drop Models",
            "final_price": "6.73",
            "currency": "USD",
            "username": "TestBuyer",
            "purchase_id": "TEST",
        },
    }

    send_purchase_email(event)
    print("Test email sent.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()

    if args.test:
        send_test_email()
        return

    events = get_pending_events()

    if not events:
        print("No pending BuiltByBit events.")
        return

    print(f"Found {len(events)} pending event(s).")

    purchase_events = 0

    for event in events:
        trigger_type = str(event.get("trigger_type") or "").lower()

        if "purchase" not in trigger_type:
            print(
                f"Ignoring non-purchase event "
                f"{event.get('event_id')}: {trigger_type}"
            )
            continue

        event_id = event.get("event_id")

        if event_id is None:
            print("Skipping purchase event without an event_id.")
            continue

        send_purchase_email(event)
        complete_event(event_id)
        purchase_events += 1

    if purchase_events == 0:
        print("No pending purchase events.")
    else:
        print(f"Processed {purchase_events} purchase event(s).")


if __name__ == "__main__":
    main()
