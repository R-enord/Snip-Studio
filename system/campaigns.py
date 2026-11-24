"""Campaign rules for finished clips. A campaign gets its own rendition file, so the normal clip keeps its status and file.

ClipUp "GTA VI Clipup Logo Campaign" (Content Rewards, owner joined 2026-11-24). From the campaign page and its guidelines doc:
- Put the ClipUp logo on screen for the whole clip. ClipUp supplies the file; keep it at brand/campaigns/clipup/logo.png.
  It is never stretched, recoloured, cropped or shrunk, and nothing may cover it.
- Caption must include #gta6 and #Clipup.
- Streamer gameplay only. Each post stays up at least 60 days.
- The logo is this campaign's requirement, so the no-watermark rule does not apply to its renditions.

Tag a clip:  python campaigns.py tag <sidecar.json> <campaign> [--remove]
"""
import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANVAS_W = 1080
MARGIN = 40                     # gap from the top-right corner

CAMPAIGNS = {
    "clipup_gta6_logo": {
        "name": "GTA VI Clipup Logo Campaign",
        "hashtags": ["#gta6", "#Clipup"],
        "logo": os.path.join(ROOT, "brand", "campaigns", "clipup", "logo.png"),
        "logo_max_h": 180,      # the top band above the title box (title starts at y=230 without a facecam)
        "keep_days": 60,
    },
}


class CampaignError(ValueError):
    pass


def campaign(name):
    if name not in CAMPAIGNS:
        raise CampaignError(f"unknown campaign {name!r}")
    return CAMPAIGNS[name]


def hashtags(names):
    out = []
    for n in names or []:
        if n in CAMPAIGNS:
            out += CAMPAIGNS[n]["hashtags"]
    return out


def logo_ready(name):
    return os.path.exists(campaign(name)["logo"])


def logo_position(logo_w, logo_h, max_h, credit_box=None):
    """Top-right corner at the logo's own size. Raises rather than shrink it or let it touch the credit pill."""
    if logo_h > max_h:
        raise CampaignError(f"logo is {logo_h}px tall but the top band is {max_h}px. ClipUp's rules say never shrink it, "
                            "so the file needs a different size or the band needs to change.")
    x, y = CANVAS_W - logo_w - MARGIN, MARGIN
    if x < 0:
        raise CampaignError(f"logo is {logo_w}px wide, wider than the frame allows at a {MARGIN}px margin")
    if credit_box:
        cx0, cy0, cx1, cy1 = credit_box
        if not (x >= cx1 or y + logo_h <= cy0 or y >= cy1):
            raise CampaignError("logo would overlap the credit pill, which would hide part of the logo's space")
    return x, y


def set_campaigns(path, names, remove=False):
    """Add or remove campaign names on one clip sidecar. Written atomically; unknown names are refused."""
    for n in names:
        campaign(n)
    d = json.load(open(path, encoding="utf-8"))
    cur = list(d.get("campaigns") or [])
    for n in names:
        if remove and n in cur: cur.remove(n)
        if not remove and n not in cur: cur.append(n)
    d["campaigns"] = cur
    tmp = path + ".tmp"
    json.dump(d, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return cur


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "tag":
        print(set_campaigns(sys.argv[2], [sys.argv[3]], remove="--remove" in sys.argv))
    else:
        print(__doc__)
