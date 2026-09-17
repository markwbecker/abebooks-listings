# abebooks-listings

Bridge between Claude and the AbeBooks Inventory Update API for seller **markwbecker**.

How a listing flows:

1. Claude (from a phone or desktop session) identifies the book from photos, prices it
   against comparable AbeBooks listings, and writes `listings/NGP-XXXXXXXX.json` plus the
   photos under `photos/NGP-XXXXXXXX/`.
2. Pushing the listing file triggers the **Publish to AbeBooks** workflow
   (`.github/workflows/publish.yml`), which runs `tools/abe_publish.py`. The script builds the
   API XML, adds the credentials held in this repo's *encrypted Actions secrets*, and sends it
   to `https://inventoryupdate.abebooks.com:10027/`.
3. The workflow commits the API's answer to `results/NGP-XXXXXXXX.json` (and the raw XML), and
   Claude reads that to confirm the listing is live.

Photos are served to AbeBooks from this public repo (`raw.githubusercontent.com/.../photos/...`).

## Secrets (Settings → Secrets and variables → Actions)

| Secret | Meaning |
| --- | --- |
| `ABE_USERNAME` | Seller client PIN / sign-in username (Classic key auth) |
| `ABE_API_KEY` | The hashed "Classic" API key from AbeBooks' Manage API Keys page |
| `ABE_ACCESS_KEY` / `ABE_SECRET_KEY` | Alternative: a "Signed" key pair (HMAC-SHA256 request signing) |

Optional repository *variables*: `PHOTO_URL_BASE` (default `https://raw.githubusercontent.com/<owner>/<repo>/main`),
`ABE_HTTP_METHOD` (`PUT` default, or `POST`).

## Manual operations

* Re-send one listing: Actions → Publish to AbeBooks → *Run workflow* → enter the SKU.
* Antiquarian books use `NGA-` SKUs (`sku --antiquarian`), a full bibliographical record (see
  `schema/listing.schema.json`) and `price --method comparable`; the abebooks-antiquarian skill drives them.
* Delete a listing: set `"transaction": "delete"` in its JSON and push (or ask Claude: "delete NGP-…").
* Change a price: edit `price` in the JSON, set `"transaction": "update"`, push. Updates must carry the
  full record — AbeBooks clears any field you leave out.

Folders: `listings/` (source of truth, one JSON per SKU), `photos/`, `results/`, `tools/`.
