# drafts/

Listings that are written but **not yet approved by Mark**.

The publish workflow triggers on `listings/*.json`, so a record sitting in
`listings/` is a record on its way to AbeBooks. A draft that Mark has not said
"go" to — or that is still missing a price — lives here instead, where it can be
committed and pushed for safekeeping without going live.

On approval: `git mv drafts/<SKU>.json listings/<SKU>.json` and publish with
`python3 tools/abe_local.py publish listings/<SKU>.json --photos photos/<SKU>`.
