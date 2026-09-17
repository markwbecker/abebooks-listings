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
* SKUs are `NGP-` + the first 4 letters of the author's surname + up to 4 title initials — every
  word's initial when that fits in 4, otherwise the major words only (`NGP-BAILRIV`,
  `NGP-DETIMTAG`); use `python3 tools/abe_local.py sku --author ... --title ...`, never reused. Grade the condition from the photos and list the
  specific points you see; Mark can override the grade.
* Sessions Mark starts from his phone are all auto-titled "Book condition assessment". You can't
  rename a session yourself, so as soon as you know the book, put `/rename SURNAME Main Title`
  (surname in capitals, no subtitle) on its own line at the top of your first reply for him to
  copy and send.
* Phone photos come out sideways and framed loose. The photos command applies EXIF orientation,
  takes `--rotate` (one angle, one per file, or `name=angle`) for shots that still read sideways or
  upside down, and trims the background when the book's edges are unambiguous. It is deliberately
  conservative and says why it declined. Open every processed file and check it — upright, book not
  clipped — then `fix --sku <SKU> 3=180 --recrop`, or re-run `photos --replace --no-crop`.
* A "quote" or "price check" is not a listing: identify the book, grade it, price it, save
  `quotes/<SKU>.json` and stop — nothing goes into `photos/` or `listings/`, and no description or
  keywords are written. Quotes are inert, since the workflow only fires on `listings/*.json`.
  When he later sends more photos and says list it, re-grade from the whole set and re-price if the
  grade moved — the quote is a starting point, not a settled answer. See `quotes/README.md`.
* **Antiquarian and collectible books** (Mark says "antiquarian"; roughly pre-1875) follow the
  **abebooks-antiquarian** skill on top of this one: a full bibliographical description (format,
  collation in plain type, binding, condition in trade vocabulary, provenance, references, note),
  `NGA-` SKUs from `sku --antiquarian --publisher ... --year ...` (surname 4 + title initials +
  publisher 4 + year), and the comparable-copies price rule — `price --method comparable` — just
  under the nearest equal-or-better copy of the same edition, auctions as a floor, and no price
  without Mark's say when no copy of the edition is on the market. `validate` enforces the record.
* Show Mark the draft (title, condition points, price + comps, description, SKU, photo count) and
  wait for "go" before `publish`. He often sends photos in several batches — the photos command
  appends, so keep collecting until he says go (AbeBooks keeps up to 20). Prices follow his rule:
  the average total (item + shipping) that U.S., U.K. and Canadian sellers ask for a copy in the
  same condition — `python3 tools/abe_local.py price` applies it; show the comps in the draft.
* Credentials never go in this repo, in chat, or in memory — only in Actions secrets
  (`ABE_USERNAME` + `ABE_API_KEY`, or `ABE_ACCESS_KEY` + `ABE_SECRET_KEY`).
* `listings/<SKU>.json` is the source of truth. Updates must carry the full record — AbeBooks
  clears any field that is omitted.
