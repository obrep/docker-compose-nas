#!/usr/bin/env python3
"""Make Radarr imports play directly on TVs without DTS support (e.g. LG).

For every movie file Radarr imported since CONVERT_SINCE:
  * DTS audio tracks (DTS, DTS-HD MA, DTS:X, ...) are re-encoded to E-AC-3 (DD+);
    video and every other stream are copied untouched.
  * Text subtitles (SRT/ASS/WebVTT) are extracted next to the movie as sidecar
    .srt files, so the TV renders them itself instead of Plex burning them in.

The original file is replaced atomically only after the output has been checked.
Afterwards Radarr rescans the movie and Plex refreshes the folder.

Usage:
  convert.py            poll Radarr forever (container default)
  convert.py once PATH  process a single file, no Radarr/Plex calls
"""
import datetime
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

RADARR_URL = os.environ.get("RADARR_URL", "http://radarr:7878/radarr").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
PLEX_URL = os.environ.get("PLEX_URL", "").rstrip("/")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
CONVERT_SINCE = os.environ.get("CONVERT_SINCE", "")
POLL_SECONDS = int(os.environ.get("POLL_MINUTES", "5")) * 60
WORK_DIR = os.environ.get("WORK_DIR", "/data/.dts-converter")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
MAX_ATTEMPTS = 2

TEXT_SUBS = {"subrip", "ass", "ssa", "webvtt", "mov_text", "text"}
STATE_FILE = os.path.join(WORK_DIR, "state.json")

log = logging.getLogger("dts-converter")


# --------------------------------------------------------------------------- media


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out)


def streams_of(info, kind):
    return [s for s in info["streams"] if s.get("codec_type") == kind]


def has_dts(info):
    return any(s.get("codec_name") == "dts" for s in streams_of(info, "audio"))


def sidecar_name(movie_path, sub, used):
    base = os.path.splitext(movie_path)[0]
    tags = sub.get("tags", {})
    disp = sub.get("disposition", {})
    title = (tags.get("title") or "").lower()
    parts = [tags.get("language") or "und"]
    if disp.get("forced") or "forced" in title:
        parts.append("forced")
    elif disp.get("hearing_impaired") or "sdh" in title:
        parts.append("sdh")
    name = f"{base}.{'.'.join(parts)}.srt"
    n = 2
    while name in used:
        name = f"{base}.{'.'.join(parts)}.{n}.srt"
        n += 1
    used.add(name)
    return name


def missing_sidecars(path, info):
    """Text subtitle streams whose sidecar .srt does not exist yet."""
    used, todo = set(), []
    for sub in streams_of(info, "subtitle"):
        if sub.get("codec_name") not in TEXT_SUBS:
            continue
        target = sidecar_name(path, sub, used)
        if not os.path.exists(target):
            todo.append((sub, target))
    return todo


def audio_args(info):
    args = []
    for i, s in enumerate(streams_of(info, "audio")):
        if s.get("codec_name") != "dts":
            continue
        channels = min(int(s.get("channels") or 6), 6)
        layout = {1: "1.0", 2: "2.0", 6: "5.1"}.get(channels, f"{channels}ch")
        lang = s.get("tags", {}).get("language", "und")
        args += [
            f"-c:a:{i}", "eac3",
            f"-b:a:{i}", "1024k" if channels > 2 else "256k",
            f"-ac:a:{i}", str(channels),
            f"-metadata:s:a:{i}", f"title=DD+ {layout} (from {s.get('profile') or 'DTS'})",
            f"-metadata:s:a:{i}", f"language={lang}",
        ]
    return args


def run_ffmpeg(args):
    cmd = ["nice", "-n", "10", "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args]
    subprocess.run(cmd, check=True)


def convert_audio(path, info):
    """Write a DTS-free copy to WORK_DIR, verify it, then atomically replace the original."""
    os.makedirs(WORK_DIR, exist_ok=True)
    src_stat = os.stat(path)
    need = src_stat.st_size * 1.05 + 20 * 1024**3
    free = shutil.disk_usage(WORK_DIR).free
    if free < need:
        raise RuntimeError(f"not enough free space ({free / 1e9:.0f} GB free, need {need / 1e9:.0f} GB)")

    tmp = os.path.join(WORK_DIR, "converting.mkv")
    started = time.monotonic()
    try:
        run_ffmpeg([
            "-i", path,
            "-map", "0:v", "-map", "0:a", "-map", "0:s?", "-map", "0:t?",
            "-map_metadata", "0", "-map_chapters", "0",
            "-c", "copy", *audio_args(info),
            "-max_muxing_queue_size", "4096",
            tmp,
        ])
        out = probe(tmp)
        for kind in ("video", "audio", "subtitle"):
            if len(streams_of(out, kind)) != len(streams_of(info, kind)):
                raise RuntimeError(f"{kind} stream count changed")
        if has_dts(out):
            raise RuntimeError("output still contains DTS")
        d_in, d_out = float(info["format"]["duration"]), float(out["format"]["duration"])
        if abs(d_in - d_out) > 2:
            raise RuntimeError(f"duration changed: {d_in:.1f}s -> {d_out:.1f}s")
        now = os.stat(path)
        if (now.st_size, now.st_mtime) != (src_stat.st_size, src_stat.st_mtime):
            raise RuntimeError("source changed during conversion")
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    log.info("converted audio in %.0f min: %s", (time.monotonic() - started) / 60, path)


def extract_subs(path, todo):
    for sub, target in todo:
        run_ffmpeg(["-i", path, "-map", f"0:{sub['index']}", "-c:s", "srt", target])
        log.info("extracted subtitle %s", os.path.basename(target))


def process_file(path):
    """Returns True when the file was changed."""
    info = probe(path)
    dts, subs = has_dts(info), missing_sidecars(path, info)
    if not dts and not subs:
        return False
    log.info("%s: dts=%s, sidecars to extract=%d%s", path, dts, len(subs), " (dry run)" if DRY_RUN else "")
    if DRY_RUN:
        return False
    if dts:
        convert_audio(path, info)
    if subs:
        extract_subs(path, subs)
    return True


# --------------------------------------------------------------------------- services


def http(method, url, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def radarr(method, path, body=None):
    raw = http(method, f"{RADARR_URL}/api/v3/{path}", {"X-Api-Key": RADARR_API_KEY}, body)
    return json.loads(raw) if raw else None


def plex_refresh(folder):
    if not (PLEX_URL and PLEX_TOKEN):
        return
    token = urllib.parse.urlencode({"X-Plex-Token": PLEX_TOKEN})
    root = ET.fromstring(http("GET", f"{PLEX_URL}/library/sections?{token}"))
    for section in root.iter("Directory"):
        for loc in section.iter("Location"):
            if (folder + "/").startswith(loc.get("path").rstrip("/") + "/"):
                q = urllib.parse.urlencode({"path": folder, "X-Plex-Token": PLEX_TOKEN})
                http("GET", f"{PLEX_URL}/library/sections/{section.get('key')}/refresh?{q}")
                log.info("plex refresh requested for %s", folder)
                return


def candidates(since):
    """Movie files imported since `since` that Radarr reports with DTS audio or text subtitles."""
    for movie in radarr("GET", "movie"):
        mf = movie.get("movieFile")
        if not mf or mf.get("dateAdded", "") < since:
            continue
        mi = mf.get("mediaInfo") or {}
        codecs = [mi.get("audioCodec") or ""] + [a.get("codec") or "" for a in mi.get("audioStreams") or []]
        text_subs = any(s.get("format") in TEXT_SUBS for s in mi.get("subtitleStreams") or [])
        if any("DTS" in c for c in codecs) or text_subs:
            yield movie["id"], os.path.join(movie["path"], mf["relativePath"])


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    os.makedirs(WORK_DIR, exist_ok=True)
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(state, f, indent=1)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


def poll_once(state):
    for movie_id, path in candidates(CONVERT_SINCE):
        if not os.path.exists(path):
            continue
        st = os.stat(path)
        key = f"{path}|{st.st_size}|{int(st.st_mtime)}"
        entry = state.get(key, {})
        if entry.get("done") or entry.get("attempts", 0) >= MAX_ATTEMPTS:
            continue
        try:
            changed = process_file(path)
            state[key] = {"done": True}
            if changed:
                radarr("POST", "command", {"name": "RescanMovie", "movieId": movie_id})
                plex_refresh(os.path.dirname(path))
        except Exception as exc:  # keep polling other files
            entry["attempts"] = entry.get("attempts", 0) + 1
            entry["error"] = str(exc)
            state[key] = entry
            log.error("failed (%d/%d) %s: %s", entry["attempts"], MAX_ATTEMPTS, path, exc)
        save_state(state)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    if len(sys.argv) == 3 and sys.argv[1] == "once":
        process_file(sys.argv[2])
        return
    if not (RADARR_API_KEY and CONVERT_SINCE):
        sys.exit("RADARR_API_KEY and CONVERT_SINCE must be set")
    datetime.date.fromisoformat(CONVERT_SINCE[:10])  # fail fast on a bad date
    log.info("watching Radarr imports since %s, every %ds%s", CONVERT_SINCE, POLL_SECONDS, " (dry run)" if DRY_RUN else "")
    state = load_state()
    while True:
        try:
            poll_once(state)
        except Exception as exc:
            log.error("poll failed: %s", exc)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
