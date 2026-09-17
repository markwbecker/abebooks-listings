#!/usr/bin/env python3
"""
abe_local.py — helper that Claude runs in its cloud session (NOT in GitHub Actions).

It never talks to AbeBooks (the session cannot reach abebooks.com). It gets photos and
the listing record into the markwbecker/abebooks-listings repo, then waits for the
"Publish to AbeBooks" workflow to write results/<SKU>.json.

Two transports, chosen automatically:
  git mode  — the current directory is a clone of the repo (a Claude Code cloud session
              started from the repo). Commits and pushes on the current branch; the
              workflow runs on any branch and fast-forwards main afterwards.
  API mode  — no clone; uses the GitHub Contents API (works only in sessions that were
              given write access to the repo).

Sub-commands
  check                          where am I, can I push, is the workflow present
  sku     --author "Surname, First" --title "Title"   NGP- + 4 letters of surname + <=4 title initials, unique (or --random)
  photos  --sku SKU [--replace] f1 f2 ...   normalise photos -> photos/SKU/1.jpg ... ; later calls append
  price   --comps comps.json --condition "Very Good" [--binding hard|soft|any] [--new] [--regions US,UK,CA]
          -> average buyer total (item + shipping) of U.S., U.K. and Canadian sellers in the same condition
             (--method max-total-discount [--discount 0.20] or average-item for the older rules)
  validate listing.json          check fields / lengths, print normalised listing
  publish listing.json [--photos DIR] [--wait 300] [--skip-photos]
  status  SKU                    show results/<SKU>.json
  delete  SKU [--wait 300]       mark listing as transaction=delete and push
  list                           list SKUs already in listings/

Environment: ABE_REPO (owner/repo, default markwbecker/abebooks-listings).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
try:
    import numpy as _np
except Exception:                                   # cropping needs numpy; everything else doesn't
    _np = None

import requests

REPO = os.environ.get("ABE_REPO", "markwbecker/abebooks-listings")
API = "https://api.github.com"
SKU_PREFIX = "NGP-"
SKU_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I — easy to read aloud

BOOK_CONDITION_VALUES = {"New", "As New", "Fine", "Near Fine", "Very Good", "Good", "Fair", "Poor"}
JACKET_CONDITION_VALUES = BOOK_CONDITION_VALUES | {"No Jacket"}
LIMITS = {"sku": 40, "author": 750, "title": 750, "publisher": 750, "subject": 2000, "description": 4000,
          "isbn": 15, "publishPlace": 50, "edition": 40, "bookCondition": 30, "jacketCondition": 30,
          "bookType": 30, "inscriptionType": 50, "illustrator": 254, "size": 50, "printing": 20, "binding": 30}

START_SESSION_HINT = ("This session cannot push to GitHub. Ask Mark to start a Claude Code cloud session from the "
                      "repo instead — Code tab in the Claude app, repository markwbecker/abebooks-listings, or "
                      "https://claude.ai/code/new?repositories=markwbecker/abebooks-listings — and attach the photos there.")


# ----------------------------------------------------------------------------- git helpers
def sh(*args, cwd=None, check=True) -> str:
    r = subprocess.run(list(args), cwd=cwd, text=True, capture_output=True)
    if check and r.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(args)}\n{r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def repo_root() -> Path | None:
    """Path of the repo clone if we are inside one whose origin is the listings repo."""
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], text=True, capture_output=True)
    if r.returncode != 0:
        return None
    root = Path(r.stdout.strip())
    url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root, text=True, capture_output=True).stdout.strip()
    return root if REPO.lower() in url.lower().replace(".git", "") else None


ROOT = repo_root()
GIT_MODE = ROOT is not None


def current_branch() -> str:
    b = sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=ROOT)
    if b == "HEAD":  # detached — make a branch so the push has somewhere to go
        b = f"abebooks/{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
        sh("git", "checkout", "-q", "-b", b, cwd=ROOT)
    return b


def git_pull_quiet(branch: str):
    subprocess.run(["git", "pull", "--rebase", "-q", "origin", branch], cwd=ROOT, text=True, capture_output=True)


def git_push_current() -> tuple[str, str]:
    branch = current_branch()
    r = subprocess.run(["git", "push", "-u", "origin", f"HEAD:{branch}"], cwd=ROOT, text=True, capture_output=True)
    if r.returncode != 0:
        err = r.stderr + r.stdout
        if "authorized repository set" in err or "403" in err:
            raise SystemExit(START_SESSION_HINT + "\n\n" + err.strip()[-400:])
        git_pull_quiet(branch)  # branch moved (e.g. the workflow committed results) — rebase and retry once
        r = subprocess.run(["git", "push", "-u", "origin", f"HEAD:{branch}"], cwd=ROOT, text=True, capture_output=True)
        if r.returncode != 0:
            raise SystemExit("git push failed:\n" + (r.stderr + r.stdout).strip()[-600:])
    return branch, sh("git", "rev-parse", "HEAD", cwd=ROOT)


def git_read_remote_json(branch: str, path: str) -> dict | None:
    subprocess.run(["git", "fetch", "-q", "origin", branch], cwd=ROOT, text=True, capture_output=True)
    r = subprocess.run(["git", "show", f"origin/{branch}:{path}"], cwd=ROOT, text=True, capture_output=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


# ----------------------------------------------------------------------------- GitHub API helpers (API mode + run status)
def _session() -> requests.Session:
    s = requests.Session()
    s.headers["Accept"] = "application/vnd.github+json"
    s.headers["X-GitHub-Api-Version"] = "2022-11-28"
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        s.headers["Authorization"] = f"Bearer {tok}"
    return s


S = _session()


def gh(method: str, path: str, **kw) -> requests.Response:
    r = S.request(method, API + path, timeout=60, **kw)
    if r.status_code == 401 or (r.status_code in (403, 404) and "linked GitHub account" in r.text):
        sys.exit("GitHub is not connected for this Claude account. Ask Mark to connect GitHub at "
                 "https://claude.ai/code (or Settings > Connectors > GitHub), then retry.")
    return r


def api_get_file(path: str, ref: str = "main") -> dict | None:
    r = gh("GET", f"/repos/{REPO}/contents/{path}", params={"ref": ref})
    if r.status_code == 404:
        return None
    if r.status_code == 403 and "not enabled for this session" in r.text:
        sys.exit(START_SESSION_HINT)
    r.raise_for_status()
    return r.json()


def api_read_json(path: str, ref: str = "main") -> dict | None:
    f = api_get_file(path, ref)
    return json.loads(base64.b64decode(f["content"]).decode("utf-8")) if f else None


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def api_put_file(path: str, data: bytes, message: str, branch: str = "main") -> dict:
    existing = api_get_file(path, branch)
    body = {"message": message, "content": base64.b64encode(data).decode(), "branch": branch}
    if existing:
        if existing.get("sha") == git_blob_sha(data):
            return {"skipped": True, "path": path, "sha": existing["sha"]}
        body["sha"] = existing["sha"]
    r = gh("PUT", f"/repos/{REPO}/contents/{path}", json=body)
    if r.status_code == 403:
        raise SystemExit(START_SESSION_HINT + f"\n(HTTP 403 writing {path})")
    if r.status_code not in (200, 201):
        raise SystemExit(f"GitHub refused writing {path}: HTTP {r.status_code} {r.text[:300]}")
    j = r.json()
    return {"path": path, "sha": j["content"]["sha"], "commit": j["commit"]["sha"]}


def api_list_dir(path: str, ref: str = "main") -> list[dict]:
    r = gh("GET", f"/repos/{REPO}/contents/{path}", params={"ref": ref})
    if r.status_code in (403, 404):
        return []
    j = r.json()
    return j if isinstance(j, list) else []


def workflow_runs_for(commit_sha: str) -> list[dict]:
    r = gh("GET", f"/repos/{REPO}/actions/runs", params={"head_sha": commit_sha, "per_page": 5})
    return r.json().get("workflow_runs", []) if r.status_code == 200 else []


# ----------------------------------------------------------------------------- commands
def cmd_check(_):
    info = {"mode": "git" if GIT_MODE else "api", "repo": REPO}
    ok = True
    if GIT_MODE:
        info["clone"] = str(ROOT)
        info["branch"] = sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=ROOT)
        info["workflow_present"] = (ROOT / ".github/workflows/publish.yml").exists()
        info["publisher_present"] = (ROOT / "tools/abe_publish.py").exists()
        r = subprocess.run(["git", "push", "--dry-run", "origin", f"HEAD:{info['branch']}"], cwd=ROOT, text=True, capture_output=True)
        out = r.stderr + r.stdout
        info["can_push"] = r.returncode == 0
        if not info["can_push"]:
            info["push_error"] = out.strip()[-300:]
            ok = False
        info["listings"] = len(list((ROOT / "listings").glob("NGP-*.json")))
    else:
        r = gh("GET", f"/repos/{REPO}")
        info["api_repo_access"] = r.status_code == 200
        if r.status_code != 200:
            info["api_error"] = r.json().get("message", r.text[:200])
            ok = False
        else:
            info["workflow_present"] = bool(api_get_file(".github/workflows/publish.yml"))
            info["listings"] = len(api_list_dir("listings"))
    print(json.dumps(info, indent=2))
    if not ok:
        print("\n" + START_SESSION_HINT)
        sys.exit(1)


def existing_skus() -> set[str]:
    taken = set()
    if GIT_MODE:
        subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=ROOT, text=True, capture_output=True)
        names = sh("git", "ls-tree", "-r", "--name-only", "origin/main", "--", "listings", cwd=ROOT, check=False).splitlines()
        names += [str(p) for p in (ROOT / "listings").glob("*.json")]
        for src in names:
            name = Path(src).name
            if name.endswith(".json"):
                taken.add(name[:-5].upper())
    else:
        taken = {e["name"][:-5].upper() for e in api_list_dir("listings") if e["name"].endswith(".json")}
    return taken


def _alnum(text: str, digits: bool) -> str:
    """Uppercase ASCII letters (and optionally digits) of text, accents stripped."""
    import unicodedata
    flat = "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c))
    keep = "A-Z0-9" if digits else "A-Z"
    return re.sub(f"[^{keep}]", "", flat.upper())


# Words that carry no weight in a title: articles, conjunctions and prepositions, English plus the
# handful of foreign ones that turn up in scholarly titles. Used only when the full set of initials
# is too long for the SKU's 4-character title slot.
MINOR_TITLE_WORDS = {
    "a", "an", "the",
    "and", "but", "or", "nor", "for", "yet", "so", "as", "than", "if", "that", "whether",
    "at", "by", "down", "from", "in", "into", "near", "of", "off", "on", "onto", "out", "over",
    "per", "to", "up", "upon", "via", "with", "within", "without", "about", "above", "across",
    "after", "against", "along", "among", "around", "before", "behind", "below", "beneath",
    "beside", "between", "beyond", "during", "except", "inside", "outside", "since", "through",
    "throughout", "toward", "towards", "under", "until", "versus", "vs",
    "le", "la", "les", "un", "une", "des", "du", "de", "der", "die", "das", "den", "dem", "ein",
    "eine", "und", "von", "zu", "im", "el", "los", "las", "y", "del", "il", "lo", "gli", "e",
    "dei", "della", "nel", "et", "en", "op", "van", "het",
}


def _word_token(word: str) -> str:
    """One title word -> its SKU contribution: its first letter, or the whole thing if it is a number."""
    a = _alnum(word, digits=True)
    return a if a.isdigit() else a[:1]


def title_code(main_title: str, limit: int = 4) -> str:
    """Initials of the main title, capped at `limit` characters.

    Every word counts when the initials already fit (Bailey / 'Religion in Vergil' -> RIV). When they
    don't, the minor words drop out and only the major ones are left ('The Masters of Truth in Archaic
    Greece' -> MTAG), because those are the words a reader would use to recognise the book. A title
    with more than `limit` major words is cut to the first `limit`.
    """
    words = [w for w in re.split(r"[\s\-\u2013\u2014/]+", main_title) if _alnum(w, digits=True)]
    if not words:
        return ""
    full = "".join(_word_token(w) for w in words)
    if len(full) <= limit:
        return full
    major = [w for w in words if _alnum(w, digits=True).lower() not in MINOR_TITLE_WORDS]
    code = "".join(_word_token(w) for w in (major or words))
    return code[:limit]


def derive_sku(author: str | None, title: str | None, taken: set[str]) -> str:
    """NGP- + first 4 letters of the author's surname + up to 4 characters of title initials.

    'Bailey, Cyril'    / 'Religion in Vergil'                  -> NGP-BAILRIV    (3 initials, all words fit)
    'Detienne, Marcel' / 'The Masters of Truth in Archaic ...'  -> NGP-DETIMTAG   (7 would be too long -> major words)
    'Hemingway, Ernest'/ 'The Old Man and the Sea'              -> NGP-HEMIOMS
    'Orwell, George'   / '1984'                                 -> NGP-ORWE1984   (a numeric word is kept whole)
    Subtitle after a colon is ignored. A second copy of the same book gets ...2, then ...3.
    """
    surname = ""
    if author:
        first = re.split(r"[;&]| and ", author)[0].strip()
        surname = first.split(",")[0].strip() if "," in first else (first.split()[-1] if first.split() else "")
    main_title = re.split(r"[:;(\[]", title or "")[0].strip()
    core = _alnum(surname, digits=False)[:4] + title_code(main_title)
    if not core:
        core = "".join(secrets.choice(SKU_ALPHABET) for _ in range(8))
    cand = SKU_PREFIX + core
    if cand not in taken:
        return cand
    for n in range(2, 100):
        cand = f"{SKU_PREFIX}{core}{n}"
        if cand not in taken:
            return cand
    raise SystemExit("could not derive a unique SKU — pass --random")


def new_sku(taken: set[str]) -> str:
    for _ in range(50):
        cand = SKU_PREFIX + "".join(secrets.choice(SKU_ALPHABET) for _ in range(8))
        if cand.upper() not in taken:
            return cand
    raise SystemExit("could not generate a unique SKU")


def cmd_sku(a):
    taken = existing_skus()
    if a.random or not (a.author or a.title):
        print(new_sku(taken))
    else:
        print(derive_sku(a.author, a.title, taken))


ROTATE_WORDS = {"none": 0, "0": 0, "left": 90, "ccw": 90, "90": 90, "180": 180, "flip": 180,
                "right": 270, "cw": 270, "270": 270, "-90": 270}


def parse_rotations(spec: str | None, files: list[str]) -> dict[str, int]:
    """--rotate takes one angle for every file, a comma list in file order, or name=angle pairs."""
    if not spec:
        return {}
    parts = [x.strip() for x in spec.split(",") if x.strip()]
    if any("=" in x for x in parts):
        out = {}
        for part in parts:
            name, _, ang = part.partition("=")
            name, ang = name.strip(), ang.strip().lower()
            if ang not in ROTATE_WORDS:
                raise SystemExit(f"--rotate: '{ang}' is not 0/90/180/270 (or left/right/flip)")
            hits = [f for f in files if Path(f).name == name or f == name or Path(f).stem == name]
            if not hits:
                raise SystemExit(f"--rotate: no file named '{name}' in this batch")
            for f in hits:
                out[f] = ROTATE_WORDS[ang]
        return out
    if len(parts) == 1:
        if parts[0].lower() not in ROTATE_WORDS:
            raise SystemExit(f"--rotate: '{parts[0]}' is not 0/90/180/270 (or left/right/flip)")
        return {f: ROTATE_WORDS[parts[0].lower()] for f in files}
    if len(parts) != len(files):
        raise SystemExit(f"--rotate: {len(parts)} angles for {len(files)} files; give one angle, "
                         f"one per file in order, or name=angle pairs")
    for x in parts:
        if x.lower() not in ROTATE_WORDS:
            raise SystemExit(f"--rotate: '{x}' is not 0/90/180/270 (or left/right/flip)")
    return {f: ROTATE_WORDS[x.lower()] for f, x in zip(files, parts)}


def _open_mask(mask, k=2):
    for _ in range(k):
        mask = mask & _np.roll(mask, 1, 0) & _np.roll(mask, -1, 0) & _np.roll(mask, 1, 1) & _np.roll(mask, -1, 1)
    for _ in range(k):
        mask = mask | _np.roll(mask, 1, 0) | _np.roll(mask, -1, 0) | _np.roll(mask, 1, 1) | _np.roll(mask, -1, 1)
    return mask


def _largest_blob(mask):
    try:
        from scipy import ndimage                       # nicer when it happens to be installed
        lab, n = ndimage.label(mask)
        if not n:
            return None
        sizes = ndimage.sum(mask, lab, range(1, n + 1))
        sl = ndimage.find_objects(lab == int(_np.argmax(sizes)) + 1)[0]
        return sl[1].start, sl[0].start, sl[1].stop - 1, sl[0].stop - 1
    except Exception:
        pass

    def longest_run(profile, n, occ=0.10):               # numpy-only fallback
        on = list(profile > occ * n) + [False]
        best = cur = None
        for i, v in enumerate(on):
            if v:
                cur = i if cur is None else cur
            elif cur is not None:
                if best is None or (i - cur) > (best[1] - best[0] + 1):
                    best = (cur, i - 1)
                cur = None
        return best or (0, len(profile) - 1)

    h, w = mask.shape
    x0, x1 = longest_run(mask.sum(0), h)
    y0, y1 = longest_run(mask.sum(1), w)
    return x0, y0, x1, y1


def autocrop_box(im, margin=0.015, work=640, corner=0.10, cover=0.85, tol=46.0, min_px=800):
    """Find the book against its background. Returns (box|None, diagnostics).

    Deliberately conservative: it declines whenever the evidence is thin, because a crop that
    clips the book is far worse than a photo that keeps some tabletop. Claude checks the results.
    """
    if _np is None:
        return None, {"reason": "numpy not installed; run pip install -r tools/requirements.txt"}
    W, H = im.size
    sm = im.convert("RGB").copy()
    sm.thumbnail((work, work))
    a = _np.asarray(sm, dtype="float32")
    h, w, _ = a.shape
    c = max(4, int(round(min(h, w) * corner)))

    votes, seen = {}, {}                                  # background = colours shared by >=2 corners
    for idx, patch in enumerate([a[:c, :c], a[:c, -c:], a[-c:, :c], a[-c:, -c:]]):
        q = (patch.reshape(-1, 3) // 24).astype("int32")
        keys, counts = _np.unique(q, axis=0, return_counts=True)
        o = _np.argsort(-counts)
        keys, counts = keys[o], counts[o]
        take = int(_np.searchsorted(_np.cumsum(counts) / counts.sum(), cover)) + 1
        for k, n in zip(map(tuple, keys[:take]), counts[:take]):
            votes.setdefault(k, set()).add(idx)
            seen[k] = seen.get(k, 0) + int(n)
    shared = [k for k, cs in votes.items() if len(cs) >= 2] or sorted(seen, key=lambda k: -seen[k])[:3]
    bg = _np.array(shared, dtype="float32") * 24 + 12.0

    dist = _np.linalg.norm(a[:, :, None, :] - bg[None, None, :, :], axis=3).min(axis=2)
    mask = _open_mask(dist > tol)
    fg = float(mask.mean())
    diag = {"subject_fraction": round(fg, 3)}
    if fg > 0.93:
        return None, {**diag, "reason": "book already fills the frame"}
    if fg < 0.04:
        return None, {**diag, "reason": "no subject stands out from the background"}
    bb = _largest_blob(mask)
    if bb is None:
        return None, {**diag, "reason": "no subject found"}
    x0, y0, x1, y1 = bb
    fill = float(mask[y0:y1 + 1, x0:x1 + 1].mean())
    diag["solidity"] = round(fill, 3)
    if fill < 0.75:
        return None, {**diag, "reason": "no solid edge (looks like text or a close-up, nothing to trim)"}

    sx, sy = W / w, H / h
    mx, my = margin * W, margin * H
    box = (max(0, int(x0 * sx - mx)), max(0, int(y0 * sy - my)),
           min(W, int((x1 + 1) * sx + mx)), min(H, int((y1 + 1) * sy + my)))
    cov = ((box[2] - box[0]) * (box[3] - box[1])) / float(W * H)
    diag["coverage"] = round(cov, 3)
    if cov < 0.20:
        return None, {**diag, "reason": "detected region too small to be the book"}
    if cov > 0.985:
        return None, {**diag, "reason": "nothing worth trimming"}
    gx, gy = 0.012 * W, 0.012 * H
    sides = {"left": box[0] > gx, "top": box[1] > gy, "right": box[2] < W - gx, "bottom": box[3] < H - gy}
    diag["margins"] = sorted(k for k, v in sides.items() if v)
    if not ((sides["left"] and sides["right"]) or (sides["top"] and sides["bottom"])):
        return None, {**diag, "reason": "book runs off the frame; nothing safe to trim"}
    if min(box[2] - box[0], box[3] - box[1]) < min_px and min(W, H) >= min_px:
        return None, {**diag, "reason": "crop would leave too few pixels"}
    return box, diag


def process_image(im, rotate=0, crop=True):
    """EXIF orientation, then Mark's rotation, then the auto-crop. Returns (image, notes)."""
    from PIL import ImageOps
    im = ImageOps.exif_transpose(im)
    notes = {}
    if rotate:
        im = im.rotate(rotate, expand=True)      # PIL rotates counter-clockwise
        notes["rotated"] = rotate
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    if crop:
        box, diag = autocrop_box(im)
        if box:
            before = im.size
            im = im.crop(box)
            notes["crop"] = {"applied": True, "box": list(box),
                             "trimmed_pct": round(100 * (1 - (im.size[0] * im.size[1]) /
                                                         float(before[0] * before[1]))),
                             **{k: v for k, v in diag.items() if k in ("solidity", "margins")}}
        else:
            notes["crop"] = {"applied": False, "reason": diag.get("reason", "no box found")}
    return im, notes


def cmd_photos(a):
    from PIL import Image
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    out = Path(a.out) if a.out else ((ROOT / "photos" / a.sku) if GIT_MODE else Path("photos") / a.sku)
    out.mkdir(parents=True, exist_ok=True)
    existing = sorted_photos(str(out))
    if a.replace:
        for p in existing:
            p.unlink()
        existing = []
    start = (max(int(p.stem) for p in existing) + 1) if existing else 1   # later batches append, numbering continues
    rotations = parse_rotations(getattr(a, "rotate", None), list(a.files))
    report = []
    for i, src in enumerate(a.files, start=start):
        p = Path(src)
        try:
            im = Image.open(p)
        except Exception as e:
            report.append({"source": str(p), "error": f"cannot open: {e}"})
            continue
        im, notes = process_image(im, rotate=rotations.get(src, 0), crop=not a.no_crop)
        im.thumbnail((a.max_px, a.max_px))  # keeps aspect ratio; never upsizes
        dest = out / f"{i}.jpg"
        im.save(dest, "JPEG", quality=a.quality, optimize=True, progressive=True)
        report.append({"source": str(p), "output": str(dest), "size_kb": round(dest.stat().st_size / 1024),
                       "width": im.width, "height": im.height, **notes})
    all_photos = sorted_photos(str(out))
    repo_paths = [f"photos/{a.sku}/{p.name}" for p in all_photos]
    print(json.dumps({"sku": a.sku, "dir": str(out), "added": report, "total_photos": len(all_photos),
                      "over_abebooks_limit": max(0, len(all_photos) - 20),
                      "listing_photos_field": repo_paths[:20],
                      "review": "open each output and check it: upright, and the book not clipped. "
                                "Fix with: fix --sku <SKU> <n>=180 --recrop, or re-run photos --replace "
                                "--no-crop with the same sources."}, indent=2))


def cmd_fix(a):
    """Rotate or re-crop photos already written to photos/<SKU>/ — after seeing how they came out."""
    from PIL import Image
    out = Path(a.out) if a.out else ((ROOT / "photos" / a.sku) if GIT_MODE else Path("photos") / a.sku)
    have = {p.stem: p for p in sorted_photos(str(out))}
    if not have:
        raise SystemExit(f"no photos in {out}")
    jobs = {}
    for part in a.edits:
        idx, _, ang = part.partition("=")
        idx = idx.strip()
        if idx not in have:
            raise SystemExit(f"{out} has no photo {idx} (have {', '.join(sorted(have, key=int))})")
        ang = (ang or "0").strip().lower()
        if ang not in ROTATE_WORDS:
            raise SystemExit(f"'{ang}' is not 0/90/180/270 (or left/right/flip)")
        jobs[idx] = ROTATE_WORDS[ang]
    if not jobs and a.recrop:
        jobs = {k: 0 for k in have}
    report = []
    for idx, ang in sorted(jobs.items(), key=lambda kv: int(kv[0])):
        src = have[idx]
        im, notes = process_image(Image.open(src), rotate=ang, crop=a.recrop)
        im.save(src, "JPEG", quality=a.quality, optimize=True, progressive=True)
        report.append({"photo": str(src), "width": im.width, "height": im.height,
                       "size_kb": round(src.stat().st_size / 1024), **notes})
    print(json.dumps({"sku": a.sku, "dir": str(out), "changed": report,
                      "note": "these are the processed copies; to undo a crop, re-run photos "
                              "--replace --no-crop with the original files"}, indent=2))


def condition_tier(text: str | None) -> int | None:
    if not text:
        return None
    t = text.lower()
    if re.search(r"as new|like new|mint|brand new", t):
        return 6
    if re.search(r"near fine|\bnf\b", t):
        return 4
    if re.search(r"\bfine\b|\bf\b", t):
        return 5
    if re.search(r"very good|\bvg\b", t):
        return 3
    if re.search(r"\bgood\b|\bg\b", t):
        return 2
    if re.search(r"\bfair\b|acceptable", t):
        return 1
    if re.search(r"\bpoor\b|reading copy", t):
        return 0
    if re.search(r"\bnew\b", t):
        return 7
    return None


def binding_kind(text: str | None) -> str | None:
    if not text:
        return None
    t = text.lower()
    if re.search(r"soft|paper|wrap|pb\b|brosch|tasche|rustica", t):
        return "soft"
    if re.search(r"hard|cloth|board|leather|hc\b|buckram|linen|gebunden", t):
        return "hard"
    return None


US_RE = re.compile(r"\bU\.?\s?S\.?\s?A?\.?\b|\bUnited States\b|\bUSA\b", re.I)
US_STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR".split())
UK_RE = re.compile(r"United Kingdom|Great Britain|\bU\.?K\.?\b|\bGB\b|\bEngland\b|\bScotland\b|\bWales\b|Northern Ireland", re.I)
CA_RE = re.compile(r"\bCanada\b", re.I)
OTHER_RE = re.compile(r"\bIreland\b|Germany|France|Spain|Italy|Netherlands|Belgium|Australia|New Zealand|India|Japan|"
                      r"Austria|Switzerland|Sweden|Denmark|Norway|Finland|Poland|Portugal|Mexico|Brazil|Argentina|South Africa|"
                      r"Israel|Greece|Czech|Hungary|Romania|Turkey|China|Hong Kong|Singapore|Korea", re.I)


def seller_region(location: str | None) -> str | None:
    """'US', 'UK', 'CA' or 'other' from the seller location as AbeBooks shows it; None when unknown."""
    if not location:
        return None
    loc = location.strip()
    if UK_RE.search(loc):                       # checked first: "Northern Ireland" is UK, "Ireland" alone is not
        return "UK"
    if US_RE.search(loc):
        return "US"
    if CA_RE.search(loc):
        return "CA"
    if OTHER_RE.search(loc):
        return "other"
    parts = [x.strip() for x in loc.split(",")]
    if len(parts) >= 2 and parts[-1].upper() in US_STATES:      # "Dallas, TX"
        return "US"
    return None


def seller_in_us(location: str | None) -> bool | None:
    r = seller_region(location)
    return None if r is None else (r == "US")


def money(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower()
    if s in ("", "n/a", "unknown", "none", "null"):
        return None
    if "free" in s:
        return 0.0
    s = re.sub(r"[^0-9.]", "", s.replace(",", ""))
    try:
        return float(s)
    except ValueError:
        return None


COND_URL_TOKEN = {7: "new", 6: "an", 5: "fine", 4: "nf", 3: "vg", 2: "good", 1: "fair", 0: "poor"}
TIER_NAME = {7: "New", 6: "As New", 5: "Fine", 4: "Near Fine", 3: "Very Good", 2: "Good", 1: "Fair", 0: "Poor"}


def cmd_price(a):
    comps = json.loads(Path(a.comps).read_text())
    mine_tier = 7 if a.new else condition_tier(a.condition)
    if mine_tier is None:
        sys.exit(f"could not understand condition {a.condition!r}; use e.g. 'Very Good', 'Good', 'Fine'")
    regions = [r.strip().upper() for r in a.regions.split(",") if r.strip()]
    junk = re.compile(r"test|\bqa\b|qa_|zz[-_]|prueba|no comprar|do not buy|sample listing|seotest", re.I)
    kept, dropped = [], []
    for c in comps:
        price = money(c.get("price"))
        if price is None:
            dropped.append({**c, "reason": "no price"}); continue
        ship = money(c.get("shipping"))
        c = {**c, "price": price, "shipping": ship, "total": (price + ship) if ship is not None else None,
             "tier": condition_tier(c.get("condition")), "kind": binding_kind(c.get("binding")),
             "region": seller_region(c.get("location") or c.get("seller_location") or c.get("country"))}
        blob = " ".join(str(c.get(k, "")) for k in ("seller", "title", "notes"))
        if price <= 0:
            dropped.append({**c, "reason": "non-positive price"})
        elif junk.search(blob):
            dropped.append({**c, "reason": "test/junk listing"})
        elif (c["tier"] == 7) != (mine_tier == 7):
            dropped.append({**c, "reason": "new vs used mismatch"})
        else:
            kept.append(c)

    filters = []
    want_kind = None if a.binding == "any" else a.binding
    if want_kind:
        same = [c for c in kept if c["kind"] == want_kind]
        if len(same) >= a.min_comps:
            dropped += [{**c, "reason": "different binding"} for c in kept if c["kind"] != want_kind]
            kept = same
            filters.append(f"binding={want_kind}")
        else:
            filters.append("binding relaxed (too few same-binding comps)")

    view = lambda c: {k: c.get(k) for k in ("seller", "location", "region", "condition", "binding", "price", "shipping", "total")}

    if a.method in ("avg-total", "max-total-discount"):
        # Mark's rules work on the buyer's total (item + shipping): overseas sellers show a low item
        # price and make it up in shipping. Only sellers in the chosen regions count.
        inreg = [c for c in kept if c["region"] in regions]
        dropped += [{**c, "reason": (f"seller outside {'/'.join(regions)}" if c["region"] else "seller location unknown")}
                    for c in kept if c["region"] not in regions]
        kept = inreg
        filters.append(f"sellers in {'/'.join(regions)} only")
        priced = [c for c in kept if c["total"] is not None]
        dropped += [{**c, "reason": "shipping cost not shown"} for c in kept if c["total"] is None]
        kept = priced
        for width in (0, 1, 99):
            near = [c for c in kept if c["tier"] is not None and abs(c["tier"] - mine_tier) <= width]
            if len(near) >= a.min_comps or width == 99:
                if width < 99:
                    dropped += [{**c, "reason": "different condition grade"} for c in kept if c not in near]
                    kept = near
                filters.append({0: "same condition grade only", 1: "no same-grade copies — widened to ±1 grade",
                                99: "no nearby-grade copies — all used copies in region considered"}[width])
                break
        outliers = []
        if len(kept) >= 4 and not a.no_outlier_filter:
            med = statistics.median(c["total"] for c in kept)
            outliers = [c for c in kept if c["total"] > 4 * med]
            kept = [c for c in kept if c not in outliers]
            if outliers:
                filters.append("totals above 4x the median treated as outliers (listed separately — say so if one should count)")
        if not kept:
            print(json.dumps({"price": None, "method": a.method, "n_used": 0, "filters": filters,
                              "dropped": [{**view(c), "reason": c.get("reason")} for c in dropped],
                              "note": f"no {'/'.join(regions)} listings in this condition with a visible total — widen the search (drop bi=/cond=) or ask Mark for a price"}, indent=2))
            return
        kept.sort(key=lambda c: c["total"], reverse=True)
        totals = [c["total"] for c in kept]
        if a.method == "avg-total":
            raw = statistics.fmean(totals)
            method = f"average of the buyer total (item + shipping) across {'/'.join(regions)} sellers in the same condition, rounded to whole dollars"
            basis = None
        else:
            raw = totals[0] * (1 - a.discount)
            method = f"{int(a.discount*100)}% below the highest total (item + shipping) from a {'/'.join(regions)} seller in the same condition, rounded to whole dollars"
            basis = view(kept[0])
        price = max(1, int(raw + 0.5))
        out = {"price": price, "currency": "USD", "method": method, "raw_price": round(raw, 2),
               "n_used": len(kept), "mean_total": round(statistics.fmean(totals), 2),
               "median_total": round(statistics.median(totals), 2), "range_total": [min(totals), max(totals)],
               "by_region": {r: sum(1 for c in kept if c["region"] == r) for r in regions}, "filters": filters,
               "comps_used": [view(c) for c in kept], "outliers_not_used": [view(c) for c in outliers],
               "dropped": [{**view(c), "reason": c.get("reason")} for c in dropped]}
        if basis:
            out["basis"] = basis
        print(json.dumps(out, indent=2))
        return

    # ---- legacy method: average of comparable item prices (shipping excluded), any seller location
    for width in (1, 2, 99):
        near = [c for c in kept if c["tier"] is not None and abs(c["tier"] - mine_tier) <= width]
        if len(near) >= a.min_comps or width == 99:
            if width < 99:
                dropped += [{**c, "reason": f"condition more than {width} grade(s) away"} for c in kept if c not in near]
                kept = near
            filters.append(f"condition within ±{width} grade(s)" if width < 99 else "condition filter relaxed")
            break
    if len(kept) >= 4:
        med = statistics.median(c["price"] for c in kept)
        inl = [c for c in kept if 0.25 * med <= c["price"] <= 4 * med]
        dropped += [{**c, "reason": "price outlier vs median"} for c in kept if c not in inl]
        kept = inl
        filters.append("outliers beyond 0.25x–4x median removed")
    if not kept:
        print(json.dumps({"price": None, "n_used": 0, "filters": filters, "dropped": dropped,
                          "note": "no comparable listings — ask Mark for a price or widen the search"}, indent=2))
        return
    prices = [c["price"] for c in kept]
    mean = statistics.fmean(prices)
    price = max(1, int(mean + 0.5))
    print(json.dumps({"price": price, "currency": "USD",
                      "method": "average of comparable item prices, shipping excluded, rounded to whole dollars",
                      "n_used": len(kept), "mean_raw": round(mean, 2), "median": round(statistics.median(prices), 2),
                      "min": min(prices), "max": max(prices), "filters": filters,
                      "comps_used": [{k: c.get(k) for k in ("seller", "condition", "binding", "price")} for c in kept],
                      "dropped": [{k: c.get(k) for k in ("seller", "condition", "binding", "price", "reason")} for c in dropped]},
                     indent=2))


def normalise_listing(l: dict) -> tuple[dict, list[str]]:
    problems = []
    l = dict(l)
    l["sku"] = (l.get("sku") or "").strip().upper()
    if not re.fullmatch(r"NGP-[A-Z0-9]{2,36}", l["sku"]):
        problems.append(f"sku {l['sku']!r} must be NGP- followed by 2-36 letters/digits")
    l["transaction"] = (l.get("transaction") or "add").lower()
    if l["transaction"] not in ("add", "update", "delete"):
        problems.append("transaction must be add, update or delete")
    if l["transaction"] != "delete":
        if not any(l.get(k) for k in ("author", "title", "publisher")):
            problems.append("need at least one of author / title / publisher")
        try:
            if float(l.get("price", 0)) <= 0:
                problems.append("price must be positive")
        except (TypeError, ValueError):
            problems.append("price must be a number")
        bc = l.get("bookCondition")
        if bc not in BOOK_CONDITION_VALUES:
            problems.append(f"bookCondition {bc!r} must be one of {sorted(BOOK_CONDITION_VALUES)}")
        jc = l.get("jacketCondition")
        if l.get("dustJacket") and jc not in JACKET_CONDITION_VALUES:
            problems.append(f"jacketCondition {jc!r} must be one of {sorted(JACKET_CONDITION_VALUES)} when dustJacket is true")
        if l.get("bindingType") not in (None, "hard", "soft"):
            problems.append("bindingType must be 'hard' or 'soft'")
        for k, lim in LIMITS.items():
            v = l.get(k)
            if isinstance(v, str) and len(v) > lim:
                problems.append(f"{k} is {len(v)} chars; limit {lim}")
        isbn = re.sub(r"[^0-9Xx]", "", str(l.get("isbn") or ""))
        if isbn and len(isbn) not in (10, 13):
            problems.append(f"isbn {isbn!r} should be 10 or 13 digits")
        yr = str(l.get("publishYear") or "")
        if yr and not re.fullmatch(r"\d{4}", yr):
            problems.append("publishYear should be a 4-digit year")
        if not l.get("photos"):
            problems.append("no photos listed (allowed, but Mark wants photos on every listing)")
        if len(l.get("photos") or []) > 20:
            problems.append("more than 20 photos; AbeBooks keeps the first 20")
        l.setdefault("currency", "USD")
        l.setdefault("quantity", 1)
        l.setdefault("language", "ENG")
    return l, problems


def cmd_validate(a):
    l, problems = normalise_listing(json.loads(Path(a.listing).read_text()))
    print(json.dumps({"ok": not problems, "problems": problems, "listing": l}, indent=2))
    if problems:
        sys.exit(1)


def sorted_photos(photos_dir: str) -> list[Path]:
    return sorted(Path(photos_dir).glob("*.jpg"), key=lambda p: int(p.stem) if p.stem.isdigit() else 999)


def dump(listing: dict) -> str:
    return json.dumps(listing, indent=2, ensure_ascii=False) + "\n"


def wait_for_result(sku: str, commit_sha: str, branch: str, wait: int) -> dict | None:
    deadline = time.time() + wait
    run_seen = False
    while time.time() < deadline:
        res = git_read_remote_json(branch, f"results/{sku}.json") if GIT_MODE else api_read_json(f"results/{sku}.json", branch)
        if res and res.get("trigger_sha") == commit_sha:
            return res
        try:
            for run in workflow_runs_for(commit_sha):
                run_seen = True
                if run.get("status") == "completed" and run.get("conclusion") not in ("success", None):
                    print(f"  workflow run ended with conclusion={run['conclusion']}: {run['html_url']}")
                    deadline = min(deadline, time.time() + 30)  # give the results commit a moment to land
        except Exception:
            pass
        print(f"  waiting... ({int(deadline - time.time())}s left; workflow {'running' if run_seen else 'not yet visible'})")
        time.sleep(10)
    return None


def finish(sku: str, result: dict | None, wait: int, branch: str):
    if result is None:
        sys.exit(f"Timed out after {wait}s waiting for results/{sku}.json. Check "
                 f"https://github.com/{REPO}/actions and re-run `status {sku}` in a minute.")
    if GIT_MODE:  # bring the workflow's results commit into the local clone
        git_pull_quiet(branch)
    print(json.dumps(result, indent=2))
    if not result.get("ok"):
        sys.exit(1)


def cmd_publish(a):
    listing, problems = normalise_listing(json.loads(Path(a.listing).read_text()))
    hard = [p for p in problems if not p.startswith("no photos")]
    if hard:
        sys.exit("listing has problems:\n - " + "\n - ".join(hard))
    sku = listing["sku"]
    title = str(listing.get("title", ""))[:60]

    if GIT_MODE:
        branch = current_branch()
        git_pull_quiet(branch)
        if a.photos and not a.skip_photos:
            dest = ROOT / "photos" / sku
            dest.mkdir(parents=True, exist_ok=True)
            paths = []
            for p in sorted_photos(a.photos):
                if p.resolve() != (dest / p.name).resolve():
                    shutil.copy2(p, dest / p.name)
                paths.append(f"photos/{sku}/{p.name}")
            listing["photos"] = paths
            print(f"  {len(paths)} photo(s) staged under photos/{sku}/")
        target = ROOT / "listings" / f"{sku}.json"
        if target.exists() and listing["transaction"] == "add":
            print(f"  note: {sku} already exists in listings/ — sending as UPDATE instead of ADD")
            listing["transaction"] = "update"
        listing["pushed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        target.write_text(dump(listing))
        if Path(a.listing).resolve() != target.resolve():
            Path(a.listing).write_text(dump(listing))
        subprocess.run(["git", "add", "-A", f"listings/{sku}.json", f"photos/{sku}"], cwd=ROOT, text=True, capture_output=True)
        if not sh("git", "diff", "--cached", "--name-only", cwd=ROOT):
            print("Nothing changed — listing already pushed. Use the workflow's 'Run workflow' button to resend.")
            return
        sh("git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", f"{sku}: {listing['transaction']} {title}", cwd=ROOT)
        branch, commit = git_push_current()
        print(f"Pushed {commit[:7]} to {branch}. Waiting for the AbeBooks workflow ...")
        finish(sku, wait_for_result(sku, commit, branch, a.wait), a.wait, branch)
        return

    # ---- API mode (session with write access but no clone)
    branch = "main"
    if a.photos and not a.skip_photos:
        print(f"Uploading photos for {sku} ...")
        paths = []
        for p in sorted_photos(a.photos):
            res = api_put_file(f"photos/{sku}/{p.name}", p.read_bytes(), f"{sku}: photo {p.name}", branch)
            print(f"  photo {p.name}: {'already up to date' if res.get('skipped') else 'uploaded'}")
            paths.append(f"photos/{sku}/{p.name}")
        listing["photos"] = paths
    if api_read_json(f"listings/{sku}.json", branch) and listing["transaction"] == "add":
        print(f"  note: {sku} already exists in listings/ — sending as UPDATE instead of ADD")
        listing["transaction"] = "update"
    listing["pushed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    res = api_put_file(f"listings/{sku}.json", dump(listing).encode("utf-8"), f"{sku}: {listing['transaction']} {title}", branch)
    Path(a.listing).write_text(dump(listing))
    if res.get("skipped"):
        print("Listing file unchanged — nothing new to publish. Use the workflow's 'Run workflow' button to resend.")
        return
    print(f"Listing pushed (commit {res['commit'][:7]}). Waiting for the AbeBooks workflow ...")
    finish(sku, wait_for_result(sku, res["commit"], branch, a.wait), a.wait, branch)


def cmd_status(a):
    sku = a.sku.upper()
    if GIT_MODE:
        res = git_read_remote_json(current_branch(), f"results/{sku}.json") or git_read_remote_json("main", f"results/{sku}.json")
    else:
        res = api_read_json(f"results/{sku}.json")
    print(json.dumps(res, indent=2) if res else f"no results yet for {sku}")


def cmd_delete(a):
    sku = a.sku.upper()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if GIT_MODE:
        branch = current_branch()
        git_pull_quiet(branch)
        target = ROOT / "listings" / f"{sku}.json"
        listing = json.loads(target.read_text()) if target.exists() else git_read_remote_json("main", f"listings/{sku}.json")
        if not listing:
            sys.exit(f"{sku} is not in listings/ — nothing to delete")
        listing.update(transaction="delete", deleted_at=stamp)
        target.write_text(dump(listing))
        sh("git", "add", str(target), cwd=ROOT)
        sh("git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", f"{sku}: delete", cwd=ROOT)
        branch, commit = git_push_current()
        print(f"Delete pushed ({commit[:7]}). Waiting for the AbeBooks workflow ...")
        finish(sku, wait_for_result(sku, commit, branch, a.wait), a.wait, branch)
        return
    listing = api_read_json(f"listings/{sku}.json")
    if not listing:
        sys.exit(f"{sku} is not in listings/ — nothing to delete")
    listing.update(transaction="delete", deleted_at=stamp)
    res = api_put_file(f"listings/{sku}.json", dump(listing).encode("utf-8"), f"{sku}: delete")
    print(f"Delete pushed ({res['commit'][:7]}). Waiting for the AbeBooks workflow ...")
    finish(sku, wait_for_result(sku, res["commit"], "main", a.wait), a.wait, "main")


def cmd_list(_):
    for s in sorted(existing_skus()):
        print(s)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    p = sub.add_parser("sku"); p.add_argument("--author"); p.add_argument("--title"); p.add_argument("--random", action="store_true")
    p.set_defaults(fn=cmd_sku)
    p = sub.add_parser("photos"); p.add_argument("--sku", required=True); p.add_argument("--out")
    p.add_argument("--replace", action="store_true", help="start numbering from 1 again instead of appending")
    p.add_argument("--max-px", type=int, default=1600); p.add_argument("--quality", type=int, default=85)
    p.add_argument("--rotate", help="one angle for all files, one per file in order, or name=angle pairs; "
                                    "0/90/180/270 or left/right/flip (EXIF orientation is always applied first)")
    p.add_argument("--no-crop", action="store_true", help="keep the full frame; by default the background "
                                                          "is trimmed when the book's edges are unambiguous")
    p.add_argument("files", nargs="+"); p.set_defaults(fn=cmd_photos)
    p = sub.add_parser("fix", help="rotate or re-crop photos already in photos/<SKU>/")
    p.add_argument("--sku", required=True); p.add_argument("--out")
    p.add_argument("--recrop", action="store_true", help="run the auto-crop again on these photos")
    p.add_argument("--quality", type=int, default=88)
    p.add_argument("edits", nargs="*", metavar="N=ANGLE",
                   help="photo number = 0/90/180/270 or left/right/flip, e.g. 3=180 5=left")
    p.set_defaults(fn=cmd_fix)
    p = sub.add_parser("price"); p.add_argument("--comps", required=True); p.add_argument("--condition", required=True)
    p.add_argument("--binding", choices=["hard", "soft", "any"], default="any"); p.add_argument("--new", action="store_true")
    p.add_argument("--method", choices=["avg-total", "max-total-discount", "average-item"], default="avg-total")
    p.add_argument("--regions", default="US,UK,CA", help="seller regions that count, comma-separated (default US,UK,CA)")
    p.add_argument("--discount", type=float, default=0.20, help="max-total-discount only: fraction below the top total")
    p.add_argument("--no-outlier-filter", action="store_true"); p.add_argument("--min-comps", type=int, default=1)
    p.set_defaults(fn=cmd_price)
    p = sub.add_parser("validate"); p.add_argument("listing"); p.set_defaults(fn=cmd_validate)
    p = sub.add_parser("publish"); p.add_argument("listing"); p.add_argument("--photos"); p.add_argument("--wait", type=int, default=300)
    p.add_argument("--skip-photos", action="store_true"); p.set_defaults(fn=cmd_publish)
    p = sub.add_parser("status"); p.add_argument("sku"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("delete"); p.add_argument("sku"); p.add_argument("--wait", type=int, default=300); p.set_defaults(fn=cmd_delete)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
