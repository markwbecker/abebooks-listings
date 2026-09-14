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
  sku                            print a fresh unique SKU (NGP- + 8 chars)
  photos  --sku SKU [--out DIR] f1 f2 ...   normalise photos -> photos/SKU/1.jpg, 2.jpg ...
  price   --comps comps.json --condition "Very Good" [--binding hard|soft|any] [--new]
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


def new_sku(taken: set[str]) -> str:
    for _ in range(50):
        cand = SKU_PREFIX + "".join(secrets.choice(SKU_ALPHABET) for _ in range(8))
        if cand.upper() not in taken:
            return cand
    raise SystemExit("could not generate a unique SKU")


def cmd_sku(_):
    print(new_sku(existing_skus()))


def cmd_photos(a):
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    out = Path(a.out) if a.out else ((ROOT / "photos" / a.sku) if GIT_MODE else Path("photos") / a.sku)
    out.mkdir(parents=True, exist_ok=True)
    report = []
    for i, src in enumerate(a.files, start=1):
        p = Path(src)
        try:
            im = Image.open(p)
            im = ImageOps.exif_transpose(im)
        except Exception as e:
            report.append({"source": str(p), "error": f"cannot open: {e}"})
            continue
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.thumbnail((a.max_px, a.max_px))  # keeps aspect ratio; never upsizes
        dest = out / f"{i}.jpg"
        im.save(dest, "JPEG", quality=a.quality, optimize=True, progressive=True)
        report.append({"source": str(p), "output": str(dest), "size_kb": round(dest.stat().st_size / 1024),
                       "width": im.width, "height": im.height})
    repo_paths = [f"photos/{a.sku}/{Path(r['output']).name}" for r in report if "output" in r]
    print(json.dumps({"sku": a.sku, "dir": str(out), "photos": report, "listing_photos_field": repo_paths}, indent=2))


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


def cmd_price(a):
    comps = json.loads(Path(a.comps).read_text())
    mine_tier = 7 if a.new else condition_tier(a.condition)
    if mine_tier is None:
        sys.exit(f"could not understand condition {a.condition!r}; use e.g. 'Very Good', 'Good', 'Fine'")
    junk = re.compile(r"test|\bqa\b|qa_|zz[-_]|prueba|no comprar|do not buy|sample listing", re.I)
    kept, dropped = [], []
    for c in comps:
        try:
            price = float(str(c.get("price", "")).replace("$", "").replace(",", "").replace("US", "").strip())
        except ValueError:
            dropped.append({**c, "reason": "no price"})
            continue
        c = {**c, "price": price, "tier": condition_tier(c.get("condition")), "kind": binding_kind(c.get("binding"))}
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
    price = max(1, int(mean + 0.5))  # average of item prices, rounded to whole dollars
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
    if not re.fullmatch(r"NGP-[A-Z0-9]{8}", l["sku"]):
        problems.append(f"sku {l['sku']!r} must look like NGP-XXXXXXXX (8 letters/digits)")
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
    sub.add_parser("sku").set_defaults(fn=cmd_sku)
    p = sub.add_parser("photos"); p.add_argument("--sku", required=True); p.add_argument("--out")
    p.add_argument("--max-px", type=int, default=1600); p.add_argument("--quality", type=int, default=85)
    p.add_argument("files", nargs="+"); p.set_defaults(fn=cmd_photos)
    p = sub.add_parser("price"); p.add_argument("--comps", required=True); p.add_argument("--condition", required=True)
    p.add_argument("--binding", choices=["hard", "soft", "any"], default="any"); p.add_argument("--new", action="store_true")
    p.add_argument("--min-comps", type=int, default=3); p.set_defaults(fn=cmd_price)
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
