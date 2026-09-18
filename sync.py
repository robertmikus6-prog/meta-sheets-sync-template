#!/usr/bin/env python3
"""Meta Ads / Microsoft Ads -> Google Sheets sync (Dataslayer replacement).

Pulls insights per client (clients.json) and writes them into the client's
Google Sheet in the exact column layout the old Dataslayer query produced,
so the connected Looker Studio reports keep working unchanged.

Sources: "meta" (default, daily rows) and "bing" (Microsoft Ads, monthly rows).

Default window: previous calendar month (Europe/Bratislava).
Override with --since / --until (YYYY-MM-DD), filter with --client "Name".

Existing rows inside the window are deleted first, then fresh rows are
appended, so re-runs are idempotent and partial rows get healed.

Env: META_ACCESS_TOKEN, GOOGLE_SERVICE_ACCOUNT_JSON;
for bing clients: MS_CLIENT_ID, MS_REFRESH_TOKEN, MS_DEV_TOKEN,
optional MS_CLIENT_SECRET, GH_PAT (refresh-token rotation).
"""

import argparse
import csv
import io
import json
import os
import sys
import time
import zipfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

GRAPH = "https://graph.facebook.com/v23.0"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
SHEET_EPOCH = date(1899, 12, 30)  # Google Sheets serial date epoch
TZ = ZoneInfo("Europe/Bratislava")

INT_FIELDS = {"impressions", "inline_link_clicks", "clicks", "unique_clicks"}
FLOAT_FIELDS = {"spend", "inline_link_click_ctr", "frequency"}
NAME_FIELDS = {"campaign_name", "adset_name", "ad_name"}


def fail(msg):
    print(f"::error::{msg}")
    sys.exit(1)


def round2(v):
    f = round(float(v or 0), 2)
    return int(f) if f == int(f) else f


# ---------------------------------------------------------------- Meta API

TRANSIENT_CODES = {1, 2, 4, 17, 32, 341}  # unknown / service unavailable / rate limits


def meta_get(path, params, token, attempts=5):
    params = dict(params, access_token=token)
    for attempt in range(1, attempts + 1):
        r = requests.get(f"{GRAPH}/{path}", params=params, timeout=180)
        if r.status_code == 200:
            return r.json()
        try:
            err = r.json()["error"]
        except Exception:
            err = {"message": r.text}
        if err.get("code") == 190:
            fail("META TOKEN EXPIROVANY alebo neplatny - vygeneruj novy 60-dnovy "
                 "token a uloz ho do GitHub secretu META_TOKEN. "
                 f"Meta: {err.get('message')}")
        if err.get("code") in TRANSIENT_CODES and attempt < attempts:
            wait = 30 * attempt
            print(f"Meta transient error (code {err.get('code')}: "
                  f"{err.get('message')}), retry {attempt}/{attempts - 1} in {wait}s")
            time.sleep(wait)
            continue
        fail(f"Meta API error {r.status_code} on /{path}: {err.get('message')}")


def month_chunks(since, until):
    start = since
    while start <= until:
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield start, min(until, next_month - timedelta(days=1))
        start = next_month


def fetch_insights(client, since, until, token):
    fields = ["campaign_name", "adset_name", "impressions", "inline_link_clicks",
              "inline_link_click_ctr", "clicks", "unique_clicks", "frequency",
              "spend", "actions", "action_values"]
    if client["level"] == "ad":
        fields.append("ad_name")
    base = {
        "level": client["level"],
        "time_increment": 1,
        "fields": ",".join(fields),
        "limit": 300,
    }
    if client.get("action_report_time"):
        base["action_report_time"] = client["action_report_time"]

    # Ad-level queries over long windows overload Meta ("Service temporarily
    # unavailable"), so fetch one calendar month at a time. On long backfills
    # pause between chunks so the app-level rate limit keeps recovering.
    chunks = list(month_chunks(since, until))
    rows = []
    for i, (chunk_since, chunk_until) in enumerate(chunks):
        if i and len(chunks) > 3:
            time.sleep(20)
        params = dict(base, time_range=json.dumps(
            {"since": chunk_since.isoformat(), "until": chunk_until.isoformat()}))
        page = meta_get(f"{client['act_id']}/insights", params, token)
        while True:
            rows.extend(page.get("data", []))
            after = page.get("paging", {}).get("cursors", {}).get("after")
            if not page.get("paging", {}).get("next"):
                break
            page = meta_get(f"{client['act_id']}/insights",
                            dict(params, after=after), token)
    return rows


def action_value(row, action_type, field):
    for a in row.get(field, []):
        if a["action_type"] == action_type:
            return a["value"]
    return "0"


def build_meta_row(client, api_row, date_as_serial, ym_as_serial, ym_sep):
    d = date.fromisoformat(api_row["date_start"])
    out = []
    for col in client["columns"]:
        if col == "date":
            out.append((d - SHEET_EPOCH).days if date_as_serial else d.isoformat())
        elif col == "year_month":
            if ym_as_serial:
                out.append((d.replace(day=1) - SHEET_EPOCH).days)
            else:
                out.append(f"{d.year}{ym_sep}{d.month:02d}")
        elif col in NAME_FIELDS:
            out.append(api_row.get(col, ""))
        elif col in INT_FIELDS:
            out.append(int(api_row.get(col, 0) or 0))
        elif col in FLOAT_FIELDS:
            out.append(round2(api_row.get(col, 0)))
        elif col.startswith("action_value:"):
            out.append(round2(action_value(api_row, col.split(":", 1)[1], "action_values")))
        elif col.startswith("action:"):
            out.append(int(action_value(api_row, col.split(":", 1)[1], "actions")))
        else:
            fail(f"Unknown column spec '{col}' for {client['name']}")
    return out


# ------------------------------------------------------- Microsoft Ads API

MS_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MS_SCOPE = "https://ads.microsoft.com/msads.manage offline_access"
CC_REST = "https://clientcenter.api.bingads.microsoft.com/CustomerManagement/v13"
REPORT_REST = "https://reporting.api.bingads.microsoft.com/Reporting/v13"

_ms_cache = {}


def update_github_secret(name, value):
    pat, repo = os.environ.get("GH_PAT"), os.environ.get("GITHUB_REPOSITORY")
    if not (pat and repo):
        print(f"WARNING: {name} was rotated by Microsoft but GH_PAT is not set - "
              "store the new value manually, else it expires in ~90 days")
        return
    import base64
    from nacl import encoding, public
    hdr = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"}
    key = requests.get(f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
                       headers=hdr, timeout=60).json()
    box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder))
    enc = base64.b64encode(box.encrypt(value.encode())).decode()
    r = requests.put(f"https://api.github.com/repos/{repo}/actions/secrets/{name}",
                     headers=hdr, timeout=60,
                     json={"encrypted_value": enc, "key_id": key["key_id"]})
    print(f"Rotated GitHub secret {name} (HTTP {r.status_code})")


def ms_access_token():
    if "access" in _ms_cache:
        return _ms_cache["access"]
    cid, rt = os.environ.get("MS_CLIENT_ID"), os.environ.get("MS_REFRESH_TOKEN")
    if not (cid and rt):
        fail("Missing MS_CLIENT_ID / MS_REFRESH_TOKEN env vars (bing client in scope)")
    payload = {"client_id": cid, "grant_type": "refresh_token",
               "refresh_token": rt, "scope": MS_SCOPE}
    if os.environ.get("MS_CLIENT_SECRET"):
        payload["client_secret"] = os.environ["MS_CLIENT_SECRET"]
    r = requests.post(MS_TOKEN_URL, data=payload, timeout=60)
    if r.status_code != 200:
        fail("MS REFRESH TOKEN neplatny alebo expirovany - vygeneruj novy cez "
             "get_refresh_token.py a uloz ho do GitHub secretu MS_REFRESH_TOKEN. "
             f"MS: {r.text[:300]}")
    tok = r.json()
    _ms_cache["access"] = tok["access_token"]
    if tok.get("refresh_token") and tok["refresh_token"] != rt:
        update_github_secret("MS_REFRESH_TOKEN", tok["refresh_token"])
    return _ms_cache["access"]


def bing_headers(customer_id=None, account_id=None):
    h = {"Authorization": f"Bearer {ms_access_token()}",
         "DeveloperToken": os.environ.get("MS_DEV_TOKEN", ""),
         "Content-Type": "application/json"}
    if customer_id:
        h["CustomerId"] = str(customer_id)
    if account_id:
        h["CustomerAccountId"] = str(account_id)
    return h


def bing_customer_id(account_id):
    if account_id in _ms_cache:
        return _ms_cache[account_id]
    r = requests.post(f"{CC_REST}/Account/Query", headers=bing_headers(),
                      json={"AccountId": int(account_id)}, timeout=60)
    if r.status_code != 200:
        fail(f"Bing CustomerManagement error {r.status_code}: {r.text[:300]}")
    _ms_cache[account_id] = r.json()["Account"]["ParentCustomerId"]
    return _ms_cache[account_id]


def bing_parse_day(v):
    v = v.strip()
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return datetime.strptime(v, "%m/%d/%Y").date()


def fetch_bing(client, since, until):
    # Daily aggregation, summed to months in code: the API's Monthly
    # aggregation snaps the range to whole calendar months and would include
    # out-of-window days (e.g. today) in the current month's bucket.
    account_id = client["account_id"]
    customer_id = bing_customer_id(account_id)
    report_request = {
        "Type": "AdGroupPerformanceReportRequest",
        "ReportName": "sheets-sync",
        "Format": "Csv",
        "ExcludeReportHeader": True,
        "ExcludeReportFooter": True,
        "ExcludeColumnHeaders": False,
        "ReturnOnlyCompleteData": False,
        "Aggregation": "Daily",
        "Columns": ["TimePeriod", "CampaignName", "AdGroupName", "Impressions",
                    "Clicks", "Spend", "Conversions"],
        "Scope": {"AccountIds": [int(account_id)]},
        "Time": {
            "CustomDateRangeStart": {"Year": since.year, "Month": since.month,
                                     "Day": since.day},
            "CustomDateRangeEnd": {"Year": until.year, "Month": until.month,
                                   "Day": until.day},
            "ReportTimeZone": "BrusselsCopenhagenMadridParis",
        },
    }
    hdr = bing_headers(customer_id, account_id)
    r = requests.post(f"{REPORT_REST}/GenerateReport/Submit", headers=hdr,
                      json={"ReportRequest": report_request}, timeout=120)
    if r.status_code != 200:
        fail(f"Bing report submit error {r.status_code}: {r.text[:500]}")
    request_id = r.json()["ReportRequestId"]

    download_url = None
    for _ in range(120):
        r = requests.post(f"{REPORT_REST}/GenerateReport/Poll", headers=hdr,
                          json={"ReportRequestId": request_id}, timeout=60)
        if r.status_code != 200:
            fail(f"Bing report poll error {r.status_code}: {r.text[:300]}")
        status = r.json()["ReportRequestStatus"]
        if status["Status"] == "Success":
            download_url = status.get("ReportDownloadUrl")
            break
        if status["Status"] == "Error":
            fail("Bing report generation failed")
        time.sleep(5)
    else:
        fail("Bing report generation timed out")

    if not download_url:  # no data in range
        return []
    blob = requests.get(download_url, timeout=120).content
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        raw = z.read(z.namelist()[0]).decode("utf-8-sig")

    # Sum daily rows into (month, campaign, adgroup) buckets; the daily rows
    # are filtered to the exact window as an extra guard.
    buckets = {}
    for rec in csv.DictReader(io.StringIO(raw)):
        if not rec.get("TimePeriod"):
            continue
        d = bing_parse_day(rec["TimePeriod"])
        if not (since <= d <= until):
            continue
        key = (f"{d.year}-{d.month:02d}", rec["CampaignName"], rec["AdGroupName"])
        b = buckets.setdefault(key, {"impressions": 0, "clicks": 0,
                                     "spend": 0.0, "conversions": 0})
        b["impressions"] += int(float(rec.get("Impressions") or 0))
        b["clicks"] += int(float(rec.get("Clicks") or 0))
        b["spend"] += float(rec.get("Spend") or 0)
        b["conversions"] += int(float(rec.get("Conversions") or 0))

    rows = []
    for (ym, campaign, adgroup), b in buckets.items():
        rows.append({
            "year_month": ym,
            "campaign_name": campaign,
            "adgroup_name": adgroup,
            "impressions": b["impressions"],
            "clicks": b["clicks"],
            "spend": round2(b["spend"]),
            "ctr": round2(b["clicks"] / b["impressions"] * 100) if b["impressions"] else 0,
            "cpc": round2(b["spend"] / b["clicks"]) if b["clicks"] else 0,
            "conversions": b["conversions"],
            "conversion_rate": round2(b["conversions"] / b["clicks"] * 100) if b["clicks"] else 0,
        })
    return rows


def build_bing_row(client, api_row, ym_as_serial, ym_sep):
    out = []
    for col in client["columns"]:
        v = api_row[col]
        if col == "year_month":
            y, m = int(v[:4]), int(v[5:7])
            if ym_as_serial:
                out.append((date(y, m, 1) - SHEET_EPOCH).days)
            else:
                out.append(f"{y}{ym_sep}{m:02d}")
        else:
            out.append(v)
    return out


# ------------------------------------------------------------- Google Sheets

def sheets_session():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        fail("Missing GOOGLE_SERVICE_ACCOUNT_JSON env var")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    creds.refresh(GoogleAuthRequest())
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {creds.token}"
    return s


def gs(session, method, url, attempts=5, **kw):
    for attempt in range(1, attempts + 1):
        r = session.request(method, url, timeout=120, **kw)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504) and attempt < attempts:
            wait = 30 * attempt
            print(f"Sheets transient error {r.status_code}, "
                  f"retry {attempt}/{attempts - 1} in {wait}s")
            time.sleep(wait)
            continue
        fail(f"Sheets API error {r.status_code}: {r.text[:500]}")


def cell_to_date(v):
    if isinstance(v, (int, float)):
        return SHEET_EPOCH + timedelta(days=int(v))
    try:
        return date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        return None


def cell_to_month(v):
    if isinstance(v, (int, float)):
        return (SHEET_EPOCH + timedelta(days=int(v))).replace(day=1)
    parts = str(v).strip().replace("|", "-").split("-")
    try:
        return date(int(parts[0]), int(parts[1]), 1)
    except (ValueError, IndexError):
        return None


def delete_rows(session, sid, tab_id, doomed):
    requests_body = []
    start = prev = doomed[0]
    for i in doomed[1:] + [None]:
        if i != (prev or 0) + 1:
            requests_body.append({"deleteDimension": {"range": {
                "sheetId": tab_id, "dimension": "ROWS",
                "startIndex": start, "endIndex": prev + 1}}})
            start = i
        prev = i
    gs(session, "POST", f"{SHEETS}/{sid}:batchUpdate",
       json={"requests": list(reversed(requests_body))})


def sync_client(client, since, until, meta_token, session, summary):
    source = client.get("source", "meta")
    ident = client.get("act_id") or client.get("account_id")
    print(f"\n=== {client['name']} ({ident}) {since} .. {until}")

    if source == "bing":
        api_rows = fetch_bing(client, since, until)
        print(f"Bing API: {len(api_rows)} monthly rows")
    else:
        api_rows = fetch_insights(client, since, until, meta_token)
        print(f"Meta API: {len(api_rows)} daily rows")

    sid, tab = client["sheet_id"], client["tab"]
    meta = gs(session, "GET", f"{SHEETS}/{sid}", params={
        "fields": "sheets(properties(sheetId,title))"})
    tab_id = next((s["properties"]["sheetId"] for s in meta["sheets"]
                   if s["properties"]["title"] == tab), None)
    if tab_id is None:
        fail(f"Tab '{tab}' not found in sheet {sid}")

    grid = gs(session, "GET", f"{SHEETS}/{sid}/values/{tab}", params={
        "valueRenderOption": "UNFORMATTED_VALUE"})["values"]
    data = grid[1:]
    ym_idx = client["columns"].index("year_month")
    date_idx = client["columns"].index("date") if "date" in client["columns"] else None

    # Detect how the existing sheet stores Date / Year-Month (date serials vs text)
    probe_idx = date_idx if date_idx is not None else ym_idx
    date_as_serial, ym_as_serial, ym_sep = True, True, "-"
    for row in data:
        if len(row) > max(probe_idx, ym_idx) and row[probe_idx] != "":
            if date_idx is not None:
                date_as_serial = isinstance(row[date_idx], (int, float))
            ym_as_serial = isinstance(row[ym_idx], (int, float))
            if not ym_as_serial and "|" in str(row[ym_idx]):
                ym_sep = "|"
            break

    # Delete existing rows inside the window (idempotent re-runs, partial healing)
    if source == "bing":
        months = {chunk[0].replace(day=1) for chunk in month_chunks(since, until)}
        doomed = [i + 1 for i, row in enumerate(data)
                  if len(row) > ym_idx and cell_to_month(row[ym_idx]) in months]
    else:
        doomed = [i + 1 for i, row in enumerate(data)
                  if len(row) > date_idx and (dt := cell_to_date(row[date_idx]))
                  and since <= dt <= until]
    if doomed:
        delete_rows(session, sid, tab_id, doomed)
        print(f"Deleted {len(doomed)} existing rows in window")

    if source == "bing":
        new_rows = [build_bing_row(client, r, ym_as_serial, ym_sep) for r in api_rows]
        sort_idx = ym_idx
    else:
        new_rows = [build_meta_row(client, r, date_as_serial, ym_as_serial, ym_sep)
                    for r in api_rows]
        sort_idx = date_idx
    new_rows.sort(key=lambda r: (r[sort_idx] if isinstance(r[sort_idx], int)
                                 else str(r[sort_idx]), str(r[2]), str(r[3])))
    if not new_rows:
        print("No delivery in window, nothing to write")
        summary.append((client["name"], 0, 0.0))
        return

    resp = gs(session, "POST", f"{SHEETS}/{sid}/values/{tab}!A1:append",
              params={"valueInputOption": "RAW",
                      "insertDataOption": "INSERT_ROWS"},
              json={"values": new_rows})
    updated = resp["updates"]["updatedRange"]
    print(f"Appended {len(new_rows)} rows -> {updated}")

    # Re-apply date number formats on the appended block (serial-mode columns only)
    first_row = int("".join(c for c in updated.split("!")[1].split(":")[0]
                            if c.isdigit())) - 1
    fmt_targets = [(ym_idx, ym_as_serial, "yyyy-mm")]
    if date_idx is not None:
        fmt_targets.append((date_idx, date_as_serial, "yyyy-mm-dd"))
    fmt_reqs = [{"repeatCell": {
        "range": {"sheetId": tab_id, "startRowIndex": first_row,
                  "endRowIndex": first_row + len(new_rows),
                  "startColumnIndex": idx, "endColumnIndex": idx + 1},
        "cell": {"userEnteredFormat": {"numberFormat": {
            "type": "DATE", "pattern": pattern}}},
        "fields": "userEnteredFormat.numberFormat"}}
        for idx, serial, pattern in fmt_targets if serial]
    if fmt_reqs:
        gs(session, "POST", f"{SHEETS}/{sid}:batchUpdate", json={"requests": fmt_reqs})

    spend_idx = client["columns"].index("spend")
    total = round(sum(float(r[spend_idx]) for r in new_rows), 2)
    print(f"Spend written: {total}")
    summary.append((client["name"], len(new_rows), total))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--client")
    args = p.parse_args()

    today = datetime.now(TZ).date()
    if args.since:
        since = date.fromisoformat(args.since)
        until = date.fromisoformat(args.until) if args.until else today - timedelta(days=1)
    else:
        first_this = today.replace(day=1)
        until = first_this - timedelta(days=1)
        since = until.replace(day=1)

    clients = json.load(open(os.path.join(os.path.dirname(__file__), "clients.json")))
    if args.client:
        clients = [c for c in clients if c["name"].lower() == args.client.lower()]
        if not clients:
            fail(f"Unknown client '{args.client}'")

    meta_token = os.environ.get("META_ACCESS_TOKEN")
    if not meta_token and any(c.get("source", "meta") == "meta" for c in clients):
        fail("Missing META_ACCESS_TOKEN env var")

    session = sheets_session()
    summary = []
    for client in clients:
        sync_client(client, since, until, meta_token, session, summary)

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as f:
            f.write(f"## Ads -> Sheets sync {since} .. {until}\n\n")
            f.write("| Klient | Riadky | Spend |\n|---|---|---|\n")
            for name, rows, spend in summary:
                f.write(f"| {name} | {rows} | {spend} |\n")
    print("\nDone.")


if __name__ == "__main__":
    main()
