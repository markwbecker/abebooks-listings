# AbeBooks Inventory Update API — working notes

Sourced from the official docs on 2026-09-14. Where the docs were vague, that is
called out explicitly rather than guessed at.

- https://www.abebooks.com/developer/authentication/signing-requests
- https://www.abebooks.com/developer/inventory-update-api/overview
- https://www.abebooks.com/developer/inventory-update-api/endpoints
- https://www.abebooks.com/developer/inventory-update-api/keywords
- https://www.abebooks.com/developer/inventory-update-api/errors

## Transport

| | |
|---|---|
| URL | `https://inventoryupdate.abebooks.com:10027/` (non-standard port) |
| Methods | `PUT` or `POST` |
| Content-Type | `text/xml` or `application/xml` |
| Body | XML request, XML response |

## Request shape

```
inventoryUpdateRequest  version="1.0"
└── action  name="bookupdate"
    ├── username     ← vendor client PIN
    └── password     ← API key
└── AbebookList      ← 1 to 100 Abebook elements
    └── Abebook
        ├── transactionType   (ADD | UPDATE | DELETE)
        ├── vendorBookID      (max 40 chars, unique)
        ├── price             (#.## + currency ISO code)
        └── … at least one of author / title / publisher
```

Auth is either HMAC-SHA256 signed headers or the legacy PIN + API-key pair shown
above. The signing scheme has its own doc page.

## The rule that will bite you

> When sending add or update requests, include **all** book information — the server
> updates all fields with each request.

Omitted or empty fields get **cleared**. There is no partial update. Always send the
complete record.

## Field reference

Confirmed from the keywords page, with documented length caps:

| Element | Type | Cap | Notes |
|---|---|---|---|
| `bookCondition` | string | 30 | "New" through "Poor" |
| `jacketCondition` | string | 30 | "As New" through "No Jacket" |
| `binding` | string | 30 | hardcover / softcover / no binding |
| `printing` | string | 20 | first through fifth+ |
| `edition` | string | — | first through fifth+ |
| `inscriptionType` | string | — | author/illustrator signatures, inscriptions |
| `productType` | string | — | books, maps, manuscripts, comics, periodicals, art prints, sheet music, photos |

Additional elements appear in the endpoints page's sample request — `quantity`,
`dustJacket`, `isbn`, `description`, and picture URLs — but the keywords page does
not enumerate them with types or caps.

**Open question:** the exact element name and nesting for picture URLs is not stated
on the pages read. The docs confirm *up to 20 picture URLs* per book, but the literal
tag name needs to be lifted from the endpoints page's full sample XML before any
submission code is written. Don't guess it.

## Photos

AbeBooks **fetches URLs**; it does not accept image uploads. Images must be hosted at
a publicly reachable address — hence this public repo and the
`raw.githubusercontent.com` URLs. Limit is 20 per book.

## Batching

`AbebookList` accepts 1–100 books per request. Chunk anything larger. No published
rate limit was found on the pages read.

## Errors

Failures come back as a `requestError` element with a code and message. Per-book
results also appear as individual `Abebook` elements in the response, so a batch can
partially succeed — check each one, not just the top-level `code`.
