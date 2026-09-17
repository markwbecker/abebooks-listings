#!/usr/bin/env bash
# Re-crop the photos that Claude reviewed one by one on 17 Sep 2026 and judged safe.
# 74 of 349 photos, across 33 of the 44 books. The 12 crops that would have clipped a book
# are deliberately left out. Run from the root of your abebooks-listings clone.
set -euo pipefail
cd "$(dirname "$0")"
git pull --rebase origin main

# Install into the SAME interpreter that runs the helper -- a bare "pip" can belong to
# another python (pyenv, conda, Homebrew) and then python3 still can't see the packages.
PY="${PYTHON:-python3}"
echo "using $("$PY" -c 'import sys; print(sys.executable)')"
if ! "$PY" -c 'import requests, PIL, numpy' 2>/dev/null; then
  "$PY" -m pip install -q -r tools/requirements.txt \
    || "$PY" -m pip install -q --user -r tools/requirements.txt \
    || "$PY" -m pip install -q --break-system-packages -r tools/requirements.txt
fi
"$PY" -c 'import requests, PIL, numpy' || {
  echo "Still missing packages for $PY -- install them there, then re-run." >&2; exit 1; }

"$PY" tools/abe_local.py fix --sku NGP-ANDEWH --recrop 1=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-COMMTOOH --recrop 1=0 3=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-DETITMOTIAG --recrop 1=0 3=0 7=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-DOVEGH --recrop 1=0 3=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-EHREDITROAAT --recrop 6=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-FEENGODS --recrop 1=0 7=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-GALIAUGU --recrop 3=0 8=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-GALIOVID --recrop 1=0 3=0 9=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-GOLDLANG --recrop 1=0 3=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-GREESFRH13370B --recrop 10=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-GRUECULT --recrop 3=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-HARDVIRG --recrop 1=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-HARROXFO --recrop 7=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-HEINVET --recrop 6=0 10=0 11=0 12=0 15=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-HINDALLU --recrop 1=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-HORSVA6 --recrop 2=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-HUTCGREE --recrop 5=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-JANKHHATH --recrop 5=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-NORDPVMABV --recrop 7=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-OTISV --recrop 7=0 8=0 9=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-PARRMHV --recrop 6=0 7=0 8=0 9=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-POMEGODD --recrop 3=0 4=0 5=0 6=0 8=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-PUTNTPOTA --recrop 9=0 10=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-PUTNVIRG --recrop 3=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-REHMGTT --recrop 5=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-REYNTAT --recrop 7=0 8=0 9=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-RUDDH200 --recrop 4=0 7=0 11=0 12=0 13=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-SEGATAC --recrop 8=0 9=0 10=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-SHACPOH --recrop 4=0 5=0 9=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-SHERROMA --recrop 1=0 3=0 5=0 9=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-TARRTEAR --recrop 3=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-WACEACTH --recrop 5=0 7=0 8=0 9=0 >/dev/null
"$PY" tools/abe_local.py fix --sku NGP-WOODRHET --recrop 3=0 8=0 9=0 >/dev/null

# Touch each affected listing so the publish workflow fires and AbeBooks refetches the photos.
"$PY" - <<'PYEOF'
import json, pathlib
SKUS = ['NGP-ANDEWH', 'NGP-COMMTOOH', 'NGP-DETITMOTIAG', 'NGP-DOVEGH', 'NGP-EHREDITROAAT', 'NGP-FEENGODS', 'NGP-GALIAUGU', 'NGP-GALIOVID', 'NGP-GOLDLANG', 'NGP-GREESFRH13370B', 'NGP-GRUECULT', 'NGP-HARDVIRG', 'NGP-HARROXFO', 'NGP-HEINVET', 'NGP-HINDALLU', 'NGP-HORSVA6', 'NGP-HUTCGREE', 'NGP-JANKHHATH', 'NGP-NORDPVMABV', 'NGP-OTISV', 'NGP-PARRMHV', 'NGP-POMEGODD', 'NGP-PUTNTPOTA', 'NGP-PUTNVIRG', 'NGP-REHMGTT', 'NGP-REYNTAT', 'NGP-RUDDH200', 'NGP-SEGATAC', 'NGP-SHACPOH', 'NGP-SHERROMA', 'NGP-TARRTEAR', 'NGP-WACEACTH', 'NGP-WOODRHET']
for sku in SKUS:
    p = pathlib.Path('listings')/f'{sku}.json'
    d = json.loads(p.read_text())
    d['transaction'] = 'update'
    d['photos_recropped'] = '2026-09-17'
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n')
print(f'bumped {len(SKUS)} listings')
PYEOF

git add -A photos listings
git commit -m "photos: trim the background on 74 reviewed shots across 33 books"
echo
echo "Review the diff, then:  git push origin main"
echo "The push starts the publish workflow, which sends 33 updates to AbeBooks."
