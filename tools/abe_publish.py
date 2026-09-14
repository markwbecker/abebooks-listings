#!/usr/bin/env python3
"""
abe_publish.py — runs inside GitHub Actions.

Reads one or more listing JSON files (listings/NGP-XXXXXXXX.json), builds the
AbeBooks Inventory Update API XML, authenticates (Classic username/API-key in
the XML body, or HMAC-SHA256 signed headers when a Signed key pair is present),
sends the request to https://inventoryupdate.abebooks.com:10027/ and writes the
outcome to results/<SKU>.json (+ raw XML response in results/<SKU>.xml).

Credentials come ONLY from environment variables (GitHub Actions secrets):
  Classic key:  ABE_USERNAME  (seller client PIN / sign-in username)
                ABE_API_KEY   (the hashed "Classic" API key)
  Signed key:   ABE_ACCESS_KEY, ABE_SECRET_KEY
Optional:
  PHOTO_URL_BASE  public base URL for repo-relative photo paths
                  (default: https://raw.githubusercontent.com/<owner>/<repo>/main)
  ABE_HTTP_METHOD PUT (default) or POST
  ABE_DRY_RUN     if set to "1", build everything but do not call AbeBooks
  ABE_ENDPOINT    override endpoint (used by the local mock-server test)

Usage: python tools/abe_publish.py listings/NGP-ABC12345.json [more.json ...]
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import requests

ENDPOINT = os.environ.get("ABE_ENDPOINT", "https://inventoryupdate.abebooks.com:10027/")
REPO_ROOT = Path(__file__).resolve().parent.parent

# Field limits from the Inventory Update API endpoint reference.
LIMITS = {
    "vendorBookID": 40, "author": 750, "title": 750, "publisher": 750,
    "subject": 2000, "description": 4000, "isbn": 15, "publishYear": 4,
    "publishPlace": 50, "edition": 40, "bookCondition": 30, "jacketCondition": 30,
    "bookType": 30, "inscriptionType": 50, "illustrator": 254, "size": 50,
    "shippingTemplateID": 40, "printing": 20, "booksellerCatalogue": 750, "binding": 30,
}

# Typographic characters that are outside ISO-8859-1 or render badly -> ASCII.
TRANSLIT = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "―": "-", "−": "-",
    "…": "...", "•": "*", " ": " ", " ": " ", "​": "",
    "Ł": "L", "ł": "l", "Œ": "OE", "œ": "oe", "Š": "S",
    "š": "s", "Ž": "Z", "ž": "z", "Ÿ": "Y", "€": "EUR",
    "™": "(TM)", "№": "No.",
}


def latin1_safe(text: str) -> str:
    """Return text that encodes cleanly as ISO-8859-1 (AbeBooks' XML encoding)."""
    if text is None:
        return ""
    text = str(text)
    out = []
    for ch in text:
        if ch in TRANSLIT:
            out.append(TRANSLIT[ch])
            continue
        try:
            ch.encode("iso-8859-1")
            out.append(ch)
        except UnicodeEncodeError:
            base = "".join(c for c in unicodedata.normalize("NFKD", ch)
                           if not unicodedata.combining(c))
            try:
                base.encode("iso-8859-1")
                out.append(base if base else "?")
            except UnicodeEncodeError:
                out.append("?")
    return "".join(out)


def clean(value, key: str | None = None) -> str:
    """Trim, transliterate, collapse whitespace and enforce field length."""
    if value is None:
        return ""
    s = latin1_safe(str(value)).strip()
    s = re.sub(r"[ \t]+", " ", s)
    if key and key in LIMITS and len(s) > LIMITS[key]:
        s = s[: LIMITS[key]].rstrip()
    return s


def as_bool(v) -> str:
    return "true" if v in (True, "true", "TRUE", "True", 1, "1", "yes") else "false"


def photo_url(path_or_url: str, base: str) -> str:
    if re.match(r"^https?://", path_or_url):
        return path_or_url
    return base.rstrip("/") + "/" + path_or_url.lstrip("/")


def build_book_xml(listing: dict, photo_base: str) -> str:
    """Build the <Abebook> element for one listing."""
    tx = (listing.get("transaction") or "add").lower()
    if tx not in ("add", "update", "delete"):
        raise ValueError(f"transaction must be add/update/delete, got {tx!r}")
    sku = clean(listing["sku"], "vendorBookID")
    if not sku:
        raise ValueError("sku is required")

    parts = ["    <Abebook>", f"      <transactionType>{tx}</transactionType>",
             f"      <vendorBookID>{escape(sku)}</vendorBookID>"]
    if tx == "delete":
        parts.append("    </Abebook>")
        return "\n".join(parts)

    def el(tag, value, attrs: dict | None = None):
        if value is None or value == "":
            return
        a = "".join(f' {k}="{escape(str(v))}"' for k, v in (attrs or {}).items())
        parts.append(f"      <{tag}{a}>{escape(value)}</{tag}>")

    el("author", clean(listing.get("author"), "author"))
    el("title", clean(listing.get("title"), "title"))
    el("publisher", clean(listing.get("publisher"), "publisher"))
    el("subject", clean(listing.get("subject"), "subject"))

    price = listing.get("price")
    if price is None or float(price) <= 0:
        raise ValueError("price must be a positive number for add/update")
    parts.append(f'      <price currency="{escape(str(listing.get("currency") or "USD").upper())}">'
                 f"{float(price):.2f}</price>")

    el("languageIsoCode", clean(listing.get("language") or "ENG").upper()[:3])
    if listing.get("dustJacket") is not None:
        el("dustJacket", as_bool(listing.get("dustJacket")))
    binding_text = clean(listing.get("binding"), "binding")  # 30-char keyword field
    if binding_text:
        btype = (listing.get("bindingType") or "").lower()
        if btype not in ("hard", "soft"):
            btype = "soft" if re.search(r"soft|paper|wrap|pb", binding_text, re.I) else "hard"
        el("binding", binding_text, {"type": btype})
    if listing.get("firstEdition") is not None:
        el("firstEdition", as_bool(listing.get("firstEdition")))
    if listing.get("signed") is not None:
        el("signed", as_bool(listing.get("signed")))
    el("booksellerCatalogue", clean(listing.get("catalogue"), "booksellerCatalogue"))
    el("description", clean(listing.get("description"), "description"))
    el("bookCondition", clean(listing.get("bookCondition"), "bookCondition"))
    el("size", clean(listing.get("size"), "size"))
    el("jacketCondition", clean(listing.get("jacketCondition"), "jacketCondition"))
    el("bookType", clean(listing.get("bookType"), "bookType"))
    isbn = re.sub(r"[^0-9Xx]", "", str(listing.get("isbn") or ""))
    el("isbn", isbn[:15])
    el("publishPlace", clean(listing.get("publishPlace"), "publishPlace"))
    year = re.sub(r"\D", "", str(listing.get("publishYear") or ""))[:4]
    el("publishYear", year)
    el("edition", clean(listing.get("edition"), "edition"))
    el("printing", clean(listing.get("printing"), "printing"))
    el("illustrator", clean(listing.get("illustrator"), "illustrator"))
    el("inscriptionType", clean(listing.get("inscriptionType"), "inscriptionType"))

    qty = int(listing.get("quantity", 1))
    parts.append(f'      <quantity amount="{qty}"></quantity>')
    el("shippingTemplateID", clean(listing.get("shippingTemplateID"), "shippingTemplateID"))
    w = listing.get("weight")
    if w and w.get("value"):
        parts.append(f'      <weight unit="{escape(str(w.get("unit", "OUNCES")).upper())}">'
                     f'{float(w["value"]):.2f}</weight>')

    photos = [p for p in (listing.get("photos") or []) if p][:20]
    if photos:
        parts.append("      <pictureList>")
        for p in photos:
            parts.append(f"        <pictureURL>{escape(photo_url(p, photo_base))}</pictureURL>")
        parts.append("      </pictureList>")
    parts.append("    </Abebook>")
    return "\n".join(parts)


def build_request_xml(listings: list[dict], photo_base: str, classic: tuple[str, str] | None) -> bytes:
    action = ['  <action name="bookupdate">']
    if classic:
        action.append(f"    <username>{escape(classic[0])}</username>")
        action.append(f"    <password>{escape(classic[1])}</password>")
    action.append("  </action>")
    books = "\n".join(build_book_xml(l, photo_base) for l in listings)
    xml = ('<?xml version="1.0" encoding="ISO-8859-1"?>\n'
           '<inventoryUpdateRequest version="1.0">\n' + "\n".join(action) +
           "\n  <AbebookList>\n" + books + "\n  </AbebookList>\n</inventoryUpdateRequest>\n")
    return xml.encode("iso-8859-1", errors="xmlcharrefreplace")


def signed_headers(method: str, uri: str, body: bytes, access_key: str, secret_key: str) -> dict:
    """HMAC-SHA256 request signing per abebooks.com/developer/authentication/signing-requests."""
    checksum = hashlib.sha256(body).hexdigest()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    canonical_uri = uri.lower()
    string_to_sign = "\n".join([method.upper(), canonical_uri, timestamp, checksum])
    signature = hmac.new(secret_key.encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "Abe-Date": timestamp,
        "Abe-Access-Key": access_key,
        "Abe-Signature": signature,
        "Abe-RequestId": str(uuid.uuid4()),
    }


def parse_response(text: str) -> dict:
    """Turn the API's XML response into a compact dict (never raises)."""
    out = {"code": None, "message": None, "books": []}
    try:
        root = ET.fromstring(text.strip())
    except ET.ParseError:
        out["message"] = "Non-XML response: " + text.strip()[:300]
        return out
    out["root"] = root.tag
    out["code"] = (root.findtext("code") or "").strip() or None
    out["message"] = (root.findtext("message") or "").strip() or None
    for b in root.iter("Abebook"):
        out["books"].append({
            "vendorBookID": (b.findtext("vendorBookID") or "").strip(),
            "code": (b.findtext("code") or "").strip(),
            "message": (b.findtext("message") or "").strip(),
            "transactionType": (b.findtext("transactionType") or "").strip(),
        })
    return out


ERROR_HINTS = {
    "600": "Successful transaction",
    "601": "Book id is not valid (vendorBookID must be <= 40 chars)",
    "602": "Book list too large (max 100 books per request)",
    "603": "Illegal book transaction (use ADD, UPDATE or DELETE)",
    "604": "Bad price (must be a positive number, format #.##)",
    "606": "Bad book data (author, title or publisher is required)",
    "607": "ShippingTemplate ID is not valid",
    "100": "Database unavailable — contact AbeBooks support",
    "103": "Network error — retry later",
    "104": "Invalid XML",
    "105": "Unhandled exception — retry later",
    "106": "External server unavailable — retry later",
    "108": "Unauthorized action — the key is not authorized for the Inventory Update API",
    "109": "Unknown action",
    "110": "User is invalid — check ABE_USERNAME (client PIN) / ABE_API_KEY",
    "111": "Error validating the user — retry later",
}


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv if a.strip()]
    if not files:
        print("No listing files given; nothing to do.")
        return 0

    owner_repo = os.environ.get("GITHUB_REPOSITORY", "OWNER/REPO")
    photo_base = os.environ.get("PHOTO_URL_BASE") or f"https://raw.githubusercontent.com/{owner_repo}/main"
    method = (os.environ.get("ABE_HTTP_METHOD") or "PUT").upper()
    dry_run = os.environ.get("ABE_DRY_RUN") == "1"

    access_key, secret_key = os.environ.get("ABE_ACCESS_KEY"), os.environ.get("ABE_SECRET_KEY")
    username, api_key = os.environ.get("ABE_USERNAME"), os.environ.get("ABE_API_KEY")
    classic = (username, api_key) if (username and api_key) and not (access_key and secret_key) else None
    if not classic and not (access_key and secret_key) and not dry_run:
        print("ERROR: no credentials. Set ABE_USERNAME + ABE_API_KEY (Classic) or ABE_ACCESS_KEY + ABE_SECRET_KEY (Signed).")
        return 2
    auth_mode = "classic" if classic else ("signed" if access_key else "none")

    results_dir = REPO_ROOT / "results"
    results_dir.mkdir(exist_ok=True)
    exit_code = 0

    for f in files:
        if not f.exists():
            print(f"skip {f}: not found (deleted listing file?)")
            continue
        listing = json.loads(f.read_text(encoding="utf-8"))
        sku = listing.get("sku") or f.stem
        result = {"sku": sku, "transaction": listing.get("transaction", "add"), "auth_mode": auth_mode,
                  "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "trigger_sha": os.environ.get("GITHUB_SHA"), "run_id": os.environ.get("GITHUB_RUN_ID"),
                  "endpoint": ENDPOINT, "ok": False}
        try:
            body = build_request_xml([listing], photo_base, classic)
        except Exception as e:  # validation problem — record it and continue
            result.update(error=f"listing invalid: {e}")
            (results_dir / f"{sku}.json").write_text(json.dumps(result, indent=2))
            print(f"[{sku}] INVALID: {e}")
            exit_code = 1
            continue

        redacted = body.decode("iso-8859-1")
        if classic:
            redacted = redacted.replace(classic[1], "***").replace(f"<username>{escape(classic[0])}</username>", "<username>***</username>")
        (results_dir / f"{sku}.request.xml").write_text(redacted, encoding="utf-8")

        if dry_run:
            result.update(ok=True, dry_run=True, message="dry run — request built, not sent")
            (results_dir / f"{sku}.json").write_text(json.dumps(result, indent=2))
            print(f"[{sku}] DRY RUN ok ({len(body)} bytes)")
            continue

        headers = {"Content-Type": "text/xml; charset=ISO-8859-1", "Accept": "text/xml"}
        if not classic:
            headers.update(signed_headers(method, ENDPOINT, body, access_key, secret_key))
        try:
            resp = requests.request(method, ENDPOINT, data=body, headers=headers, timeout=90)
            result["http_status"] = resp.status_code
            raw = resp.content.decode(resp.encoding or "iso-8859-1", errors="replace")
            (results_dir / f"{sku}.xml").write_text(raw, encoding="utf-8")
            parsed = parse_response(raw)
            result.update(code=parsed.get("code"), message=parsed.get("message"), books=parsed.get("books"))
            book = next((b for b in parsed.get("books", []) if b["vendorBookID"].upper() == sku.upper()), None)
            if book is None and parsed.get("books"):
                book = parsed["books"][0]
            if book:
                result["book_code"], result["book_message"] = book["code"], book["message"]
            code = (book or {}).get("code") or parsed.get("code")
            result["ok"] = (code == "600")
            result["hint"] = ERROR_HINTS.get(str(code), "")
        except requests.RequestException as e:
            result.update(error=f"HTTP request failed: {e}")

        (results_dir / f"{sku}.json").write_text(json.dumps(result, indent=2))
        status = "OK (600)" if result["ok"] else f"FAILED code={result.get('book_code') or result.get('code')} {result.get('hint') or result.get('error') or ''}"
        print(f"[{sku}] {result['transaction'].upper()} -> {status}")
        if not result["ok"]:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
