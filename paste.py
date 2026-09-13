"""
Parser for the Kling Studio master prompt: the block the frames-to-kling skill writes.

    batch: c150
    settings: 9:16 · 5s · pro · sound off
    references:
      G:\\avatars\\meera.png
      needed: the same woman as in the script

    1.
    image: wide shot of a diya burning on a dark table
    motion: the flame flickers, wisps of smoke sway
    ref: frame 1

Two numbered lists are accepted just as well:

    image prompts:
    1. ...
    motion prompts:
    1. ...

Anything it can't place comes back in "ignored" so the app can show it instead of
dropping it silently. Prompts are never reworded.
"""

import re

QUOTES = "\"'\u201c\u201d\u2018\u2019`"
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")

_BATCH = re.compile(r"^(?:batch(?:\s*name)?|name)\s*[:=]\s*(.+)$", re.I)
_SETTINGS = re.compile(r"^settings?\s*[:=]\s*(.+)$", re.I)
_IMAGE_LIST = re.compile(r"^(?:image|frame)\s*prompts?\s*[:=]?\s*$", re.I)
_MOTION_LIST = re.compile(r"^(?:motion|video|kling)\s*prompts?\s*[:=]?\s*$", re.I)
_PLAIN_LIST = re.compile(r"^prompts?\s*[:=]?\s*$", re.I)
_REFS_HEADER = re.compile(r"^(?:references?|ref\s*images?)\s*[:=]\s*(.*)$", re.I)
_KEY = re.compile(r"^(image|frame|motion|video|kling|ref|reference)\s*[:=]\s*(.*)$", re.I)
_NUMBER = re.compile(r"^(?:clip|frame|shot|scene)?\s*#?(\d{1,3})\s*[.):\-\u2013\u2014]\s*(.*)$", re.I)
_NUMBER_BARE = re.compile(r"^(?:clip|frame|shot|scene)\s*#?(\d{1,3})\s*$", re.I)
_BULLET = re.compile(r"^[-*\u2022]\s+")
_FRAME_REF = re.compile(r"^(?:use\s+)?(?:frame|clip|image)\s*#?(\d{1,3})$", re.I)
_NEEDED = re.compile(r"^needed\s*[:\-\u2013\u2014]?\s*(.*)$", re.I)
_NAME_LIKE = re.compile(r"^[A-Za-z0-9 _-]{1,40}$")

KEY_FIELD = {"image": "image", "frame": "image", "motion": "motion", "video": "motion",
             "kling": "motion", "ref": "ref", "reference": "ref"}


def sanitize_batch_name(text):
    """"C146 video 3" -> "C146_video_3"; None if nothing usable is left"""
    name = re.sub(r"\s+", "_", str(text).strip().strip(QUOTES).strip())
    name = re.sub(r"[^A-Za-z0-9_-]", "", name)[:60]
    return name or None


def looks_like_path(value):
    v = str(value).strip().strip(QUOTES)
    return bool(re.match(r"^[A-Za-z]:[\\/]", v)) or v.startswith(("\\\\", "/", "./", ".\\")) \
        or v.lower().endswith(IMG_EXT)


def parse_ref(value):
    """"frame 1", a file path, or "needed: the same woman" -> what the app should use"""
    v = str(value).strip().strip(QUOTES).strip()
    if not v:
        return {"kind": "needed", "note": ""}
    m = _FRAME_REF.match(v)
    if m:
        return {"kind": "frame", "index": int(m.group(1))}
    m = _NEEDED.match(v)
    if m:
        return {"kind": "needed", "note": m.group(1).strip()}
    if looks_like_path(v):
        return {"kind": "path", "path": v.strip(QUOTES)}
    return {"kind": "needed", "note": v}


def parse_settings(text):
    """pull "9:16 · 5s · pro · sound off · 2K" apart; only what's actually there"""
    out = {}
    m = re.search(r"\b(\d{1,2}):(\d{1,2})\b", text)
    if m:
        out["aspect_ratio"] = f"{m.group(1)}:{m.group(2)}"
    m = re.search(r"\b(\d{1,2})\s*s(?:ec|econds)?\b", text, re.I)
    if m:
        out["duration"] = m.group(1)
    m = re.search(r"\b(pro|std|standard)\b", text, re.I)
    if m:
        out["mode"] = "pro" if m.group(1).lower() == "pro" else "std"
    m = re.search(r"\bsound\s*(on|off|yes|no)\b", text, re.I)
    if m:
        out["sound"] = m.group(1).lower() in ("on", "yes")
    m = re.search(r"\b([124])\s*k\b", text, re.I)
    if m:
        out["resolution"] = m.group(1).upper() + "K"
    return out


def parse_master(text):
    """Read a master prompt block.

    Returns {"name", "settings", "references", "clips", "warnings", "ignored"}, where a
    clip is {"image", "motion", "ref"} and ref is None or the result of parse_ref.
    """
    lines = str(text or "").strip().strip(QUOTES).splitlines()
    name, settings, references, ignored, warnings = None, {}, [], [], []
    images, motions = {}, {}       # clip number -> prompt, for the two-list style
    blocks, order = {}, []         # clip number -> {"image", "motion", "ref"}
    mode = None                    # "images" | "motions" | "refs" | None
    current, field = None, None    # clip being filled, and the field a stray line continues
    open_item = False              # is a numbered prompt still being written on the next line?

    def block(n):
        if n not in blocks:
            blocks[n] = {"image": "", "motion": "", "ref": None}
            order.append(n)
        return blocks[n]

    def add_ref_line(value):
        v = str(value).strip().strip(QUOTES).strip()
        if v:
            references.append(parse_ref(v))

    for raw in lines:
        line = raw.strip()
        if not line:
            # a blank line closes whatever was being written: prose after a gap is
            # a stray line to report, not more of the last prompt
            field, open_item = None, False
            continue

        m = _BATCH.match(line)
        if m:
            name = sanitize_batch_name(m.group(1)) or name
            mode, current, field = None, None, None
            continue

        m = _SETTINGS.match(line)
        if m:
            settings.update(parse_settings(m.group(1)))
            mode, field = None, None
            continue

        if _IMAGE_LIST.match(line):
            mode, current, field = "images", None, None
            continue
        if _MOTION_LIST.match(line) or _PLAIN_LIST.match(line):
            mode, current, field = "motions", None, None
            continue

        m = _REFS_HEADER.match(line)
        if m and current is None:
            mode, field = "refs", None
            add_ref_line(m.group(1))
            continue

        m = _KEY.match(line)
        if m:
            key, value = KEY_FIELD[m.group(1).lower()], m.group(2).strip()
            if current is None:                  # an "image:" line before any number
                current = (max(order) + 1) if order else 1
                block(current)
            if key == "ref":
                block(current)["ref"] = parse_ref(value)
                field = None
            else:
                block(current)[key] = value
                field = key
            mode = None
            continue

        m = _NUMBER.match(line) or _NUMBER_BARE.match(line)
        if m:
            n = int(m.group(1))
            rest = m.group(2).strip() if m.re is _NUMBER else ""
            if mode == "images":
                images[n], current, field, open_item = rest, None, None, True
            elif mode == "motions":
                motions[n], current, field, open_item = rest, None, None, True
            else:
                current, field = n, None
                b = block(n)
                if rest:                          # "1. some prompt" with no image:/motion: keys
                    b["motion"], field = rest, "motion"
            continue

        line = _BULLET.sub("", line)
        if mode == "refs":
            add_ref_line(line)
        elif mode in ("images", "motions") and open_item and (images or motions):
            target = images if mode == "images" else motions
            last = max(target)
            target[last] = (target[last] + " " + line).strip()
        elif current is not None and field:
            b = block(current)
            b[field] = (b[field] + " " + line).strip()
        elif name is None and _NAME_LIKE.match(line) and sanitize_batch_name(line):
            name = sanitize_batch_name(line)
        else:
            ignored.append(line)

    clips = []
    if images or motions:
        for n in sorted(set(images) | set(motions)):
            clips.append({"image": images.get(n, "").strip(), "motion": motions.get(n, "").strip(), "ref": None})
        if images and motions and len(images) != len(motions):
            warnings.append(f"{len(images)} image prompts but {len(motions)} motion prompts.")
    for n in sorted(order):
        b = blocks[n]
        clips.append({"image": b["image"].strip(), "motion": b["motion"].strip(), "ref": b["ref"]})

    if not clips:
        warnings.append("No prompts found. Every clip needs an image prompt and a motion prompt.")
    else:
        no_image = [i + 1 for i, c in enumerate(clips) if not c["image"]]
        no_motion = [i + 1 for i, c in enumerate(clips) if not c["motion"]]
        if no_image:
            warnings.append(f"No image prompt for clip {', '.join(map(str, no_image[:10]))}. "
                            "Add one, or drop your own picture on the card.")
        if no_motion:
            warnings.append(f"No motion prompt for clip {', '.join(map(str, no_motion[:10]))}.")
    if not name:
        warnings.append("No batch name found. Put \"batch: <name>\" on the first line.")
    return {"name": name, "settings": settings, "references": references,
            "clips": clips, "warnings": warnings, "ignored": ignored}
