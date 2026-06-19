import os
import time
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

app = Flask(__name__)

# --- Config ---
DANABOOKS_URL = "https://transactionhub.zerobook.shop/api/v1/transaction-history"
DANABOOKS_TOKEN = os.environ.get("DANABOOKS_TOKEN", "")
DANABOOKS_IDENTIFIER = os.environ.get("DANABOOKS_IDENTIFIER", "thirdparty@danabooks.com")

AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "app5gOqDt9aZrW5bV")
AIRTABLE_TABLE_NAME = "French Inventories"

IST = pytz.timezone("Asia/Kolkata")

# Number of SKUs processed in parallel (Dana Books rejects true parallel calls, keep at 1)
PARALLEL_WORKERS = 1
# Delay between Dana Books API calls (seconds)
DANA_REQUEST_DELAY = 1.0
# Max retries on 429
MAX_RETRIES = 3
# Wait time on 429 before retry (seconds)
RETRY_WAIT = 10

# Thread-safe counters
_progress_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Dana Books helpers
# ---------------------------------------------------------------------------

def get_latest_purchase_price(sku):
    """Fetch the latest purchase price from Dana Books for a given SKU.
    Retries up to MAX_RETRIES times on 429 rate limit errors."""
    headers = {
        "Authorization": f"Bearer {DANABOOKS_TOKEN}",
        "Identifier": DANABOOKS_IDENTIFIER,
        "Content-Type": "application/json"
    }
    payload = {
        "itemsku": sku,
        "opcode": "PUR",
        "rows": 1
    }

    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.post(DANABOOKS_URL, json=payload, headers=headers, timeout=15)

        if resp.status_code == 429:
            if attempt < MAX_RETRIES:
                print(f"[dana] 429 Rate limit on {sku}, waiting {RETRY_WAIT}s (attempt {attempt}/{MAX_RETRIES})", flush=True)
                time.sleep(RETRY_WAIT)
                continue
            else:
                resp.raise_for_status()

        resp.raise_for_status()
        data = resp.json()

        records = data.get("data", [])
        if not records:
            return None

        price = records[0].get("item_price")
        return float(price) if price is not None else None

    return None


# ---------------------------------------------------------------------------
# Airtable helpers
# ---------------------------------------------------------------------------

def get_all_airtable_skus():
    """
    Fetch ALL records from French Inventories that have a SKU.
    Returns list of dicts: [{"record_id": ..., "sku": ..., "current_cost": ...}, ...]
    Retries each page up to 3 times on timeout/connection errors.
    """
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{requests.utils.quote(AIRTABLE_TABLE_NAME)}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    params = {
        "filterByFormula": "{SKU}!=''",
        "fields[]": ["SKU", "Cost"],
        "pageSize": 100
    }

    records = []
    offset = None

    while True:
        if offset:
            params["offset"] = offset

        resp = None
        for attempt in range(1, 4):
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=30)
                resp.raise_for_status()
                break
            except requests.exceptions.RequestException as e:
                print(f"[airtable-fetch] Attempt {attempt}/3 failed: {e}", flush=True)
                if attempt < 3:
                    time.sleep(5)
                else:
                    raise

        data = resp.json()

        for rec in data.get("records", []):
            sku = rec.get("fields", {}).get("SKU")
            if sku:
                current_cost = rec.get("fields", {}).get("Cost")
                if current_cost is not None:
                    try:
                        current_cost = float(current_cost)
                    except (ValueError, TypeError):
                        current_cost = None
                records.append({
                    "record_id": rec["id"],
                    "sku": sku,
                    "current_cost": current_cost
                })

        offset = data.get("offset")
        if not offset:
            break

    return records


def update_airtable_cost(record_id, cost):
    """Update the Cost field in Airtable for a given record ID."""
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{requests.utils.quote(AIRTABLE_TABLE_NAME)}/{record_id}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {"fields": {"Cost": cost}}
    resp = requests.patch(url, json=payload, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Core sync job
# ---------------------------------------------------------------------------

def run_auto_sync():
    """
    Scheduled job:
    1. Fetch ALL SKUs from Airtable French Inventories
    2. For each, query Dana Books for latest purchase price
    3. Only update Airtable if Cost is empty OR Dana Books price has changed
    """
    try:
        _run_auto_sync_inner()
    except Exception as e:
        import traceback
        print(f"[auto-sync] FATAL UNCAUGHT ERROR: {e}", flush=True)
        print(traceback.format_exc(), flush=True)


def _process_one_sku(item, counters):
    """
    Worker function: process a single SKU end-to-end.
    Returns a status string and updates shared counters dict (thread-safe).
    """
    sku = item["sku"]
    record_id = item["record_id"]
    current_cost = item["current_cost"]

    try:
        dana_price = get_latest_purchase_price(sku)

        if dana_price is None:
            status = "skipped_no_purchase"
        elif current_cost is not None and current_cost == dana_price:
            status = "skipped_no_change"
        else:
            update_airtable_cost(record_id, dana_price)
            print(
                f"[auto-sync] UPDATED {sku} | "
                f"old={current_cost if current_cost is not None else 'empty'} → new={dana_price}",
                flush=True
            )
            status = "updated"

    except Exception as e:
        print(f"[auto-sync] ERROR {sku}: {e}", flush=True)
        status = "error"

    # Small stagger so this worker doesn't immediately fire its next call
    time.sleep(DANA_REQUEST_DELAY)

    with _progress_lock:
        counters["done"] += 1
        counters[status] += 1
        if counters["done"] % 100 == 0:
            print(f"[auto-sync] Progress: {counters['done']}/{counters['total']} checked...", flush=True)

    return status


def _run_auto_sync_inner():
    print("[auto-sync] Starting scheduled cost sync...", flush=True)

    if not DANABOOKS_TOKEN:
        print("[auto-sync] ERROR: DANABOOKS_TOKEN is not set. Aborting.", flush=True)
        return

    try:
        all_skus = get_all_airtable_skus()
    except Exception as e:
        print(f"[auto-sync] ERROR fetching Airtable SKUs: {e}", flush=True)
        return

    print(f"[auto-sync] Total SKUs to check: {len(all_skus)}", flush=True)
    print(f"[auto-sync] Running with {PARALLEL_WORKERS} parallel workers", flush=True)

    counters = {
        "done": 0,
        "total": len(all_skus),
        "updated": 0,
        "skipped_no_purchase": 0,
        "skipped_no_change": 0,
        "error": 0,
    }

    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = [executor.submit(_process_one_sku, item, counters) for item in all_skus]
        for future in as_completed(futures):
            # Exceptions are already caught inside _process_one_sku,
            # but guard here too in case something unexpected slips through.
            try:
                future.result()
            except Exception as e:
                print(f"[auto-sync] Worker crashed unexpectedly: {e}", flush=True)

    print(
        f"[auto-sync] Done. Updated={counters['updated']} | "
        f"No purchase in Dana Books={counters['skipped_no_purchase']} | "
        f"Price unchanged={counters['skipped_no_change']} | "
        f"Errors={counters['error']}",
        flush=True
    )


# ---------------------------------------------------------------------------
# Scheduler — 9 AM, 2 PM, 8 PM IST
# ---------------------------------------------------------------------------

scheduler = BackgroundScheduler(timezone=IST)

scheduler.add_job(
    run_auto_sync,
    trigger=CronTrigger(hour=9, minute=0, timezone=IST),
    id="sync_9am",
    name="Cost sync 9 AM IST"
)
scheduler.add_job(
    run_auto_sync,
    trigger=CronTrigger(hour=14, minute=0, timezone=IST),
    id="sync_2pm",
    name="Cost sync 2 PM IST"
)
scheduler.add_job(
    run_auto_sync,
    trigger=CronTrigger(hour=20, minute=0, timezone=IST),
    id="sync_8pm",
    name="Cost sync 8 PM IST"
)

scheduler.start()
print("[scheduler] Cost sync scheduled at 9 AM, 2 PM, 8 PM IST", flush=True)


# ---------------------------------------------------------------------------
# Manual trigger endpoints
# ---------------------------------------------------------------------------

@app.route("/sync-cost", methods=["POST"])
def sync_cost_manual():
    """
    Manually trigger cost sync for one or more specific SKUs.
    Body: { "skus": ["DNG1024", "DNG1025"] }
    Or single: { "sku": "DNG1024" }
    """
    body = request.get_json(force=True) or {}

    if "skus" in body:
        skus = body["skus"]
    elif "sku" in body:
        skus = [body["sku"]]
    else:
        return jsonify({"error": "Provide 'sku' or 'skus' in request body"}), 400

    results = []
    for sku in skus:
        result = {"sku": sku}
        try:
            dana_price = get_latest_purchase_price(sku)
            if dana_price is None:
                result["status"] = "skipped"
                result["reason"] = "No purchase records found in Dana Books"
                results.append(result)
                continue

            result["dana_price"] = dana_price

            url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{requests.utils.quote(AIRTABLE_TABLE_NAME)}"
            headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
            params = {
                "filterByFormula": f"{{SKU}}='{sku}'",
                "maxRecords": 1,
                "fields[]": ["SKU", "Cost"]
            }
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            resp.raise_for_status()
            records = resp.json().get("records", [])

            if not records:
                result["status"] = "skipped"
                result["reason"] = "SKU not found in Airtable French Inventories"
                results.append(result)
                continue

            record_id = records[0]["id"]
            current_cost = records[0].get("fields", {}).get("Cost")

            if current_cost is not None:
                try:
                    current_cost = float(current_cost)
                except (ValueError, TypeError):
                    current_cost = None

            result["previous_cost"] = current_cost

            if current_cost is not None and current_cost == dana_price:
                result["status"] = "skipped"
                result["reason"] = "Price unchanged"
                results.append(result)
                continue

            update_airtable_cost(record_id, dana_price)
            result["status"] = "updated"
            result["new_cost"] = dana_price

        except Exception as e:
            result["status"] = "error"
            result["reason"] = str(e)

        results.append(result)
        print(f"[manual-sync] {result}", flush=True)

    return jsonify({"results": results}), 200


@app.route("/sync-all", methods=["POST"])
def sync_all_now():
    """Manually trigger the full auto sync job immediately."""
    threading.Thread(target=run_auto_sync, daemon=True).start()
    return jsonify({"message": "Full sync started in background"}), 200


@app.route("/health", methods=["GET"])
def health():
    jobs = [
        {"id": j.id, "name": j.name, "next_run": str(j.next_run_time)}
        for j in scheduler.get_jobs()
    ]
    return jsonify({"status": "ok", "scheduled_jobs": jobs}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)