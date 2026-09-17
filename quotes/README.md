# quotes/

A **quote** is a price for a book Mark has not decided to list. One JSON file per book, named with
the same `NGP-` SKU the book would get if it were listed, so promoting a quote to a listing is a
copy rather than a rename.

Quotes are inert. The publish workflow triggers on `listings/*.json` only, so nothing in this
directory can reach AbeBooks, and a SKU used here is not reserved — it is derived from the author
and title and will come out the same when the book is listed.

Photos are **not** kept with a quote. Listing the book later needs the photos again.

A quote carries the same field names as a listing record, plus `quoted_at` and the comps it was
built from:

```json
{
  "sku": "NGP-BAILRIV", "quoted_at": "2026-09-17",
  "author": "Bailey, Cyril", "title": "Religion in Virgil",
  "publisher": "Clarendon Press", "publishYear": "1935",
  "binding": "Hardcover", "bindingType": "hard", "dustJacket": false,
  "bookCondition": "Very Good",
  "condition_points": ["spine lightly sunned", "text clean and unmarked"],
  "not_seen_in_photos": ["rear board"],
  "price": 38,
  "pricing": {"method": "average U.S./U.K./Canadian same-condition total (item + shipping)",
              "n_used": 6, "range_total": [24.00, 52.99], "raw_price": 37.83,
              "comps": [{"seller": "...", "location": "...", "condition": "Very Good",
                         "price": 29.0, "shipping": 5.0, "total": 34.0}],
              "set_aside": ["1 outlier at $180"]}
}
```

A quote over about a month old should be re-priced rather than reused — the comps move. Keep the
old file as it stands: it records what that day's market looked like.

**A quote is not a settled grade.** It was made from two or three photos; the ones that come later
are the rear board, the page edges and the interior, which is where the flaws that move a grade
turn up. When a quoted book is listed, the book is graded again from the full set, and if the grade
moves the price is recomputed at the new grade — the comps are filtered by condition, so the two
cannot be separated. The draft says how the new grade and price compare with the quote.
