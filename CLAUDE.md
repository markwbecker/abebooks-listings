# abebooks-listings — instructions for Claude sessions in this repo

This repo publishes Mark's used-book listings to AbeBooks (seller `markwbecker`). When Mark
attaches photos of a book and gives a condition — or asks to price, update, delete or check an
`NGP-…` listing — follow the **abebooks-lister** skill. Everything it needs is here:

* `tools/abe_local.py` — run from the repo root: `check`, `sku`, `photos`, `price`, `validate`,
  `publish`, `status`, `delete`, `list`. Install deps first if missing:
  `pip install -q -r tools/requirements.txt` (add `--break-system-packages` if pip insists).
* `tools/abe_publish.py` + `.github/workflows/publish.yml` — run by GitHub Actions on every push
  that touches `listings/*.json`; they talk to the AbeBooks Inventory Update API with the
  credentials in the repo's encrypted Actions secrets and write `results/<SKU>.json`.
* `schema/listing.schema.json` — shape of a listing record; `docs/abebooks-api-notes.md` — API notes.

Ground rules:

* Work on the branch the session gives you and push it with the helper; the workflow
  fast-forwards `main` itself. Do not open pull requests for listings.
* SKUs are `NGP-` + the first 4 letters of the author's surname + the initials of the main
  title's words, variable length (`python3 tools/abe_local.py sku --author ... --title ...`),
  never reused. Grade the condition from the photos and list the
  specific points you see; Mark can override the grade.
* Show Mark the draft (title, condition points, price + comps, description, SKU, photo count) and
  wait for "go" before `publish`. He often sends photos in several batches — the photos command
  appends, so keep collecting until he says go (AbeBooks keeps up to 20). Prices follow his rule:
  the average total (item + shipping) that U.S., U.K. and Canadian sellers ask for a copy in the
  same condition — `python3 tools/abe_local.py price` applies it; show the comps in the draft.
* Credentials never go in this repo, in chat, or in memory — only in Actions secrets
  (`ABE_USERNAME` + `ABE_API_KEY`, or `ABE_ACCESS_KEY` + `ABE_SECRET_KEY`).
* `listings/<SKU>.json` is the source of truth. Updates must carry the full record — AbeBooks
  clears any field that is omitted.
