# abebooks-listings

Source of truth for AbeBooks seller inventory listings and their photos, submitted
via the [AbeBooks Inventory Update API](https://www.abebooks.com/developer/inventory-update-api/overview).

## Why this repo exists

The Inventory Update API takes **picture URLs, not uploaded image files** — up to 20
per book. The images have to live somewhere publicly fetchable. This repo is that
place: photos are committed under `photos/`, and AbeBooks pulls them over their
`raw.githubusercontent.com` URLs.

That is also why this repo is **public**. Keep that in mind: anything committed here
is world-readable. See [Secrets](#secrets) below.

## Layout

```
listings/                 one YAML file per book, named <vendorBookID>.yaml
photos/<vendorBookID>/    that book's images, referenced by raw URL
schema/listing.schema.json  JSON Schema the listing files validate against
docs/abebooks-api-notes.md  field reference and gotchas
```

### vendorBookID

The filename stem is the `vendorBookID` you send to AbeBooks. It must be **unique
across your whole inventory** and **max 40 characters**. Once a book is listed,
never reuse or renumber its ID — AbeBooks keys `update` and `delete` transactions
off it.

## Required fields

Every listing needs, at minimum:

- `transactionType` — `add`, `update`, or `delete`
- `vendorBookID`
- `price` — `#.##` plus a currency ISO code
- at least one of `author`, `title`, `publisher`

## The full-record rule

This one bites. From the API docs: on `add` or `update` you must send **all** book
information, because the server overwrites every field on each request. Omitted or
empty fields are **cleared**, not left alone. There is no partial update.

So a listing file here should always be the complete record, and the submission step
should send the whole thing — never a diff.

## Batch size

`AbebookList` holds 1–100 `Abebook` elements per request. Inventory larger than that
has to be chunked.

## Secrets

Credentials (client PIN, API key, HMAC secret) **never** go in this repo. They belong
in a local `.env`, which `.gitignore` excludes. Treat any key that does get committed
as compromised and rotate it.

## Status

Scaffold only — no submission code yet. The `/abebooks-lister` skill is intended to
generate and validate listing files against `schema/listing.schema.json` and build the
XML payload.
