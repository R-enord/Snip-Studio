"""Per-platform post text for SnipRiot videos: a hook line, credit, and hashtags sized to each platform.
The hook is written per clip (field "hook" on the clip record); the title is the fallback."""
import re
import campaigns

BASE = ["#GTA6", "#GTAVI"]
EXTRA = ["#GTA6Gameplay", "#GrandTheftAuto", "#GTA", "#Gaming"]
STREAMERS = {
    "speed":  {"name": "IShowSpeed",  "tags": ["#IShowSpeed", "#Speed"], "x": "@ishowspeedsui"},
    "kai":    {"name": "Kai Cenat",   "tags": ["#KaiCenat", "#Kai"],     "x": "@KaiCenat"},
    "dylan":  {"name": "Dylan Page",  "tags": ["#DylanPage"],            "x": "@dylanpage"},
    "kreek":  {"name": "KreekCraft",  "tags": ["#KreekCraft", "#GrandTheftAutoVI"], "x": "@KreekCraft",
               "line": "Watch KreekCraft on YouTube and Twitch"},
    "lacy":   {"name": "LACY",        "tags": ["#LACY"],                 "x": "@lacy"},
    "rage":   {"name": "YourRAGE",    "tags": ["#yourrage", "#YourRAGE"], "x": "@YourRAGEgaming"},
    "asianjeff": {"name": "AsianJeff", "tags": ["#asianjeff"],           "x": "@AsianJeff"},
    "clix":   {"name": "Clix",        "tags": ["#clix"],                 "x": "@Clix"},
}
ACCOUNTS = {"a1": "@SnipRiotGTA", "a2": "@smartclips42"}
ORIGINAL_TAGS = ["#GTA6News", "#ViceCity", "#GTA6Leonida"]

def who(rec):
    k = (rec.get("streamer") or rec.get("stream") or rec.get("name") or "").lower()
    for key, s in STREAMERS.items():
        if key in k or s["name"].lower() in k: return s
    return None

def tidy(t):
    return re.sub(r"\s+", " ", (t or "").strip())

def _uniq(tags):
    """Drop repeats, ignoring case, keeping the first spelling (campaign tags come first)."""
    seen, out = set(), []
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower()); out.append(t)
    return out

def build(rec):
    """rec: a clip/original record (title, hook?, streamer/name?, kind?, topic_tags?). Returns dict of platform texts."""
    title = tidy(rec.get("title")) or "GTA 6 moment"
    hook = tidy(rec.get("hook")) or title
    s = who(rec)
    original = rec.get("kind") == "original" or not s
    topic = [t if t.startswith("#") else "#" + t for t in (rec.get("topic_tags") or [])][:3]
    credit = f"🎥 {s['name']}" if s else ""
    if s and s.get("line"): credit = s["line"] + "\n" + credit        # campaign-required caption line
    handle = ACCOUNTS.get(rec.get("account") or "a1", "@SnipRiotGTA")
    stags = s["tags"] if s else ORIGINAL_TAGS[:2]
    camp = campaigns.hashtags(rec.get("campaigns"))     # campaign-required tags go first so the length cut never drops them

    tiktok_tags = camp + BASE + stags + topic + EXTRA[:2] + ["#fyp"]
    tiktok = "\n".join(x for x in [hook, credit, " ".join(_uniq(tiktok_tags))] if x)

    yt_title = (hook if len(hook) <= 90 else title)[:90].rstrip() + " #Shorts"
    yt_desc = "\n".join(x for x in [
        hook,
        f"Clip from {s['name']}'s live stream. All credit to {s['name']}." if s else "",
        f"Follow {handle} for daily GTA 6 clips, news and hidden details.",
        "",
        " ".join(_uniq(camp + BASE + stags + topic + EXTRA + ["#Shorts"])),
    ] if x is not None)

    x_tags = " ".join(_uniq(camp + BASE[:1] + stags[:1] + topic[:1]))
    x_credit = credit
    x = "\n\n".join(t for t in [hook, x_credit, x_tags] if t)
    if len(x) > 270:
        x = "\n\n".join(t for t in [hook[:270 - len(x_tags) - len(x_credit) - 6].rstrip() + "…", x_credit, x_tags] if t)

    return {"youtube_title": yt_title, "youtube": yt_desc, "tiktok": tiktok[:2150], "instagram": tiktok[:2150], "x": x}
