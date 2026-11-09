#!/usr/bin/env python3
"""Copy the recitation MP3s from their GitHub releases to Cloudflare R2, and check them on the public audio address.

Streaming plan, Part B (docs/STREAMING_PLAN.md). The source of truth is the app's own manifests,
android/app/src/main/assets/content/audio_manifest*.json (the iOS copies are byte-identical). Only `tracks` are
copied; the `legacy` entry (husary-hafs-v1, deleted on 4 Nov 2026) is not.

  export  build the file list (key, set, file, size, sha256) from the app's manifests
  copy    for each listed file not already in R2: download it from its GitHub release (the app's own address),
          check its size and sha256 against the list and against GitHub's own digest, upload it with
          Content-Type audio/mpeg and a one-year immutable Cache-Control, then read its R2 metadata back
  verify  check each file on the public address: HEAD (200, size, type, cache header), a byte range (206),
          optionally a full download (sha256), and that a repeat request is served from Cloudflare's cache

It runs on a GitHub Actions machine in DiaaElberry/ekhtem-quran-audio (mirror/audio_mirror.py there is a copy of this
file, with mirror/files.json made by `export`), so the 17 GB never passes through a PC. It also runs locally.

Secrets come only from environment variables and are never printed:
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY (copy), GITHUB_TOKEN (optional, for the API fallback).

Rule: never overwrite a file. A file already in R2 with a different size or sha256 is reported, not replaced.
"""
import argparse
import concurrent.futures
import glob
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

REPO = "DiaaElberry/ekhtem-quran-audio"
BUCKET = "alquran-audio"
PUBLIC_BASE = "https://audio.ekhtemalquran.stream/"
CONTENT_TYPE = "audio/mpeg"
CACHE_CONTROL = "public, max-age=31536000, immutable"
USER_AGENT = "EkhtemAlQuran-mirror"
MANIFEST_GLOB = os.path.join("android", "app", "src", "main", "assets", "content", "audio_manifest*.json")
CHUNK = 1 << 20
PART = 64 * 1024 * 1024  # files above this go up in 64 MiB parts
RETRY_WAITS = (5, 15, 30, 60)

_lock = threading.Lock()


def log(*parts):
    with _lock:
        print(*parts, flush=True)


def mb(n):
    return f"{n / 1e6:.1f} MB"


# ---------------------------------------------------------------- list

def cmd_export(args):
    paths = sorted(glob.glob(os.path.join(args.root, MANIFEST_GLOB)))
    if not paths:
        sys.exit(f"no manifests under {args.root}")
    files, seen = [], set()
    for path in paths:
        manifest = json.load(open(path, encoding="utf-8"))
        set_id = manifest["id"]
        expected = f"https://github.com/{REPO}/releases/download/{set_id}/"
        if manifest["baseUrl"] != expected:
            sys.exit(f"{os.path.basename(path)}: baseUrl {manifest['baseUrl']} is not {expected}")
        for track in manifest["tracks"]:
            key = f"{set_id}/{track['file']}"
            if key in seen:
                sys.exit(f"duplicate key {key}")
            seen.add(key)
            files.append({"key": key, "set": set_id, "file": track["file"],
                          "size": track["size"], "sha256": track["sha256"]})
    total = sum(f["size"] for f in files)
    data = {"about": "Made by tools/audio_mirror.py export from the app's audio manifests. Do not edit by hand.",
            "repo": REPO, "bucket": BUCKET, "publicBase": PUBLIC_BASE,
            "sets": len(paths), "count": len(files), "totalBytes": total, "files": files}
    with open(args.out, "w", encoding="utf-8", newline="\n") as out:
        json.dump(data, out, indent=1, ensure_ascii=False)
        out.write("\n")
    print(f"{len(paths)} sets, {len(files)} files, {total / 1e9:.2f} GB -> {args.out}")


def load_list(path, only):
    files = json.load(open(path, encoding="utf-8"))["files"]
    wanted = [k.strip() for k in (only or "").split(",") if k.strip()]
    if wanted:
        by_key = {f["key"]: f for f in files}
        missing = [k for k in wanted if k not in by_key]
        if missing:
            sys.exit(f"not in the list: {', '.join(missing)}")
        files = [by_key[k] for k in wanted]
    return files


# ---------------------------------------------------------------- GitHub

def github_request(url, accept):
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": USER_AGENT,
                                                   "X-GitHub-Api-Version": "2022-11-28"})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        # Unredirected: the token goes to api.github.com only, never to the storage address it redirects to.
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    return request


def with_retries(what, action):
    for attempt, wait in enumerate((0,) + RETRY_WAITS):
        if wait:
            time.sleep(wait)
        try:
            return action()
        except urllib.error.HTTPError as error:
            if error.code in (400, 401, 403, 404, 410) or attempt == len(RETRY_WAITS):
                raise
            log(f"   retry {attempt + 1} for {what}: HTTP {error.code}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            if attempt == len(RETRY_WAITS):
                raise
            log(f"   retry {attempt + 1} for {what}: {type(error).__name__}")


def release_assets(tag):
    """name -> {id, size, digest} for one release, all pages."""
    def page(n):
        url = f"https://api.github.com/repos/{REPO}/releases/tags/{tag}" if n == 0 else \
            f"https://api.github.com/repos/{REPO}/releases/{release_id}/assets?per_page=100&page={n}"
        with urllib.request.urlopen(github_request(url, "application/vnd.github+json"), timeout=60) as response:
            return json.load(response)

    release = with_retries(f"release {tag}", lambda: page(0))
    release_id = release["id"]
    assets, n = {}, 1
    while True:
        batch = with_retries(f"release {tag} assets page {n}", lambda: page(n))
        for asset in batch:
            assets[asset["name"]] = {"id": asset["id"], "size": asset["size"], "digest": asset.get("digest")}
        if len(batch) < 100:
            return assets
        n += 1


def download(url, accept, dest):
    def run():
        digest, size = hashlib.sha256(), 0
        request = github_request(url, accept) if url.startswith("https://api.github.com/") else \
            urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60) as response, open(dest, "wb") as out:
            while True:
                block = response.read(CHUNK)
                if not block:
                    break
                digest.update(block)
                out.write(block)
                size += len(block)
        return size, digest.hexdigest()
    return with_retries(url.rsplit("/", 2)[-2] + "/" + url.rsplit("/", 1)[-1], run)


# ---------------------------------------------------------------- R2

def r2_client():
    import boto3
    from botocore.config import Config
    missing = [n for n in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY") if not os.environ.get(n)]
    if missing:
        sys.exit(f"missing environment variables: {', '.join(missing)}")
    config = Config(retries={"max_attempts": 8, "mode": "standard"}, max_pool_connections=32,
                    # R2 doesn't accept the default checksums newer boto3 adds to every upload.
                    request_checksum_calculation="when_required", response_checksum_validation="when_required")
    return boto3.client("s3", endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                        region_name="auto", config=config)


def r2_head(s3, key):
    from botocore.exceptions import ClientError
    try:
        return s3.head_object(Bucket=BUCKET, Key=key)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def r2_matches(head, entry):
    """Problems with an object already in R2, or [] when it is exactly the listed file."""
    problems = []
    if head["ContentLength"] != entry["size"]:
        problems.append(f"size {head['ContentLength']} not {entry['size']}")
    if head.get("Metadata", {}).get("sha256") != entry["sha256"]:
        problems.append("sha256 metadata differs")
    if head.get("ContentType") != CONTENT_TYPE:
        problems.append(f"Content-Type {head.get('ContentType')}")
    if head.get("CacheControl") != CACHE_CONTROL:
        problems.append(f"Cache-Control {head.get('CacheControl')}")
    return problems


def copy_one(s3, transfer, assets, entry, workdir, dry_run):
    key = entry["key"]
    head = r2_head(s3, key)
    if head is not None:
        problems = r2_matches(head, entry)
        return ("skipped", "already in R2") if not problems else ("conflict", "in R2 but " + "; ".join(problems))

    asset = assets.get(entry["set"], {}).get(entry["file"])
    if asset is None:
        return "failed", "not in the GitHub release"
    if asset["size"] != entry["size"]:
        return "failed", f"GitHub size {asset['size']} not {entry['size']}"
    if asset["digest"] and asset["digest"] != "sha256:" + entry["sha256"]:
        return "failed", "GitHub digest differs from the list"

    dest = os.path.join(workdir, key.replace("/", "__"))
    try:
        url = f"https://github.com/{REPO}/releases/download/{entry['set']}/{entry['file']}"
        try:
            size, sha = download(url, "*/*", dest)
        except Exception as error:  # the app's address failed after retries: try the API with the token
            if not os.environ.get("GITHUB_TOKEN"):
                raise
            log(f"   {key}: release address failed ({type(error).__name__}), using the API")
            size, sha = download(f"https://api.github.com/repos/{REPO}/releases/assets/{asset['id']}",
                                 "application/octet-stream", dest)
        if size != entry["size"]:
            return "failed", f"downloaded {size} bytes, not {entry['size']}"
        if sha != entry["sha256"]:
            return "failed", "downloaded sha256 differs from the list"
        if dry_run:
            return "checked", "downloaded and matched; not uploaded (dry run)"
        s3.upload_file(dest, BUCKET, key, Config=transfer,
                       ExtraArgs={"ContentType": CONTENT_TYPE, "CacheControl": CACHE_CONTROL,
                                  "Metadata": {"sha256": entry["sha256"]}})
    finally:
        if os.path.exists(dest):
            os.remove(dest)

    head = r2_head(s3, key)
    problems = ["missing after upload"] if head is None else r2_matches(head, entry)
    return ("uploaded", "size, sha256, type and cache header confirmed") if not problems else \
        ("failed", "after upload: " + "; ".join(problems))


def cmd_copy(args):
    from boto3.s3.transfer import TransferConfig
    files = load_list(args.list, args.only)
    s3 = r2_client()
    s3.head_bucket(Bucket=BUCKET)
    transfer = TransferConfig(multipart_threshold=PART, multipart_chunksize=PART, max_concurrency=4)
    assets = {tag: release_assets(tag) for tag in sorted({f["set"] for f in files})}
    total = sum(f["size"] for f in files)
    log(f"{'Dry run: ' if args.dry_run else ''}{len(files)} files, {total / 1e9:.2f} GB, "
        f"{len(assets)} releases, {args.workers} at a time")

    started, counts, failures, moved, done = time.time(), {}, [], 0, 0
    workdir = tempfile.mkdtemp(prefix="audio-mirror-")
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(copy_one, s3, transfer, assets, f, workdir, args.dry_run): f for f in files}
        for future in concurrent.futures.as_completed(futures):
            entry = futures[future]
            try:
                status, detail = future.result()
            except Exception as error:  # report and carry on with the other files
                status, detail = "failed", f"{type(error).__name__}: {str(error)[:200]}"
            done += 1
            counts[status] = counts.get(status, 0) + 1
            if status in ("uploaded", "checked"):
                moved += entry["size"]
            if status in ("failed", "conflict"):
                failures.append(f"{entry['key']}: {detail}")
            log(f"[{done}/{len(files)}] {status:8} {entry['key']} ({mb(entry['size'])}) {detail}")
    os.rmdir(workdir)

    minutes = (time.time() - started) / 60
    log("\nSummary")
    for status in ("uploaded", "checked", "skipped", "conflict", "failed"):
        if counts.get(status):
            log(f"  {status}: {counts[status]}")
    log(f"  data moved: {moved / 1e9:.2f} GB in {minutes:.1f} min")
    if failures:
        log("Problems:")
        for line in failures:
            log("  " + line)
        sys.exit(1)


# ---------------------------------------------------------------- public address

def fetch(method, url, headers=None, read=True):
    request = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        response = urllib.request.urlopen(request, timeout=60)
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}, b""
    with response:
        body = response.read() if read and method != "HEAD" else b""
        return response.status, {k.lower(): v for k, v in response.headers.items()}, body


def full_hash(url):
    digest, size = hashlib.sha256(), 0
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        cache = response.headers.get("cf-cache-status")
        while True:
            block = response.read(CHUNK)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest(), cache


def verify_one(entry, base, do_hash):
    url = base + entry["key"]
    checks = {}
    status, headers, _ = fetch("HEAD", url)
    checks["head 200"] = status == 200
    checks["size"] = headers.get("content-length") == str(entry["size"])
    checks["type audio/mpeg"] = headers.get("content-type", "").startswith(CONTENT_TYPE)
    checks["one-year cache header"] = headers.get("cache-control") == CACHE_CONTROL
    status, headers, body = fetch("GET", url, {"Range": "bytes=0-1023"})
    checks["range 206"] = status == 206 and len(body) == 1024 and \
        headers.get("content-range") == f"bytes 0-1023/{entry['size']}"
    if do_hash:
        size, sha, _ = with_retries(entry["key"], lambda: full_hash(url))
        checks["sha256"] = size == entry["size"] and sha == entry["sha256"]
    # A GET, as the players send: Cloudflare answers HEAD requests as DYNAMIC even when the file is cached.
    status, headers, _ = fetch("GET", url, {"Range": "bytes=0-1023"})
    checks["cache HIT on repeat"] = status == 206 and headers.get("cf-cache-status") == "HIT"
    return checks


def cmd_verify(args):
    files = load_list(args.list, args.only)
    base = args.base if args.base.endswith("/") else args.base + "/"
    log(f"Checking {len(files)} files on {base}{' with full sha256' if args.full_hash else ''}")
    started, totals, failures, done = time.time(), {}, [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(verify_one, f, base, args.full_hash): f for f in files}
        for future in concurrent.futures.as_completed(futures):
            entry = futures[future]
            try:
                checks = future.result()
            except Exception as error:
                checks = {"reachable": False}
                log(f"   {entry['key']}: {type(error).__name__}: {str(error)[:200]}")
            done += 1
            for name, ok in checks.items():
                passed, seen = totals.get(name, (0, 0))
                totals[name] = (passed + ok, seen + 1)
            bad = [name for name, ok in checks.items() if not ok]
            if bad:
                failures.append(f"{entry['key']}: {', '.join(bad)}")
            log(f"[{done}/{len(files)}] {'ok  ' if not bad else 'FAIL'} {entry['key']}"
                f"{'' if not bad else ' (' + ', '.join(bad) + ')'}")
    log(f"\nSummary ({(time.time() - started) / 60:.1f} min)")
    for name, (passed, seen) in totals.items():
        log(f"  {name}: {passed}/{seen}")
    if failures:
        log("Problems:")
        for line in failures:
            log("  " + line)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    here = os.path.dirname(os.path.abspath(__file__))
    export = sub.add_parser("export", help="build the file list from the app's manifests")
    export.add_argument("out")
    export.add_argument("--root", default=os.path.dirname(here), help="the app repo (default: this tool's parent)")
    copy = sub.add_parser("copy", help="copy files from the GitHub releases to R2")
    copy.add_argument("--list", required=True)
    copy.add_argument("--only", default="", help="comma-separated keys, e.g. afasy-hafs-v1/067.mp3; empty = all")
    copy.add_argument("--workers", type=int, default=6)
    copy.add_argument("--dry-run", action="store_true", help="download and check, but upload nothing")
    verify = sub.add_parser("verify", help="check the files on the public address")
    verify.add_argument("--list", required=True)
    verify.add_argument("--only", default="")
    verify.add_argument("--base", default=PUBLIC_BASE)
    verify.add_argument("--full-hash", action="store_true", help="also download each file and check its sha256")
    verify.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    {"export": cmd_export, "copy": cmd_copy, "verify": cmd_verify}[args.command](args)


if __name__ == "__main__":
    main()
