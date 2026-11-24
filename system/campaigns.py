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
CANVAS_H = 1920
MARGIN = 40                     # gap from the canvas edges

CAMPAIGNS = {
    "clipup_gta6_logo": {
        "name": "GTA VI Clipup Logo Campaign",
        "hashtags": ["#gta6", "#Clipup"],
        "logo": os.path.join(ROOT, "brand", "campaigns", "clipup", "logo.png"),
        "logo_width": 540,      # px on the 1080-wide canvas; the height follows the file's aspect ratio
        "logo_margin": 0,       # flush to the bottom-right corner, as in the owner's reference image
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


def logo_fit(logo_w, logo_h, target_w, cam_h=0, credit_box=None, margin=MARGIN):
    """Bottom-right corner, scaled to target_w keeping the file's aspect ratio: never cropped, stretched or recoloured.
    Raises rather than cover the facecam (cam_h) or the credit pill."""
    if logo_w <= 0 or logo_h <= 0:
        raise CampaignError("logo file has no size")
    if target_w > CANVAS_W - 2 * margin:
        raise CampaignError(f"target width {target_w}px does not fit the {CANVAS_W}px frame at a {margin}px margin")
    w, h = target_w, round(logo_h * target_w / logo_w)
    x, y = CANVAS_W - w - margin, CANVAS_H - h - margin
    if y < cam_h:
        raise CampaignError(f"logo would reach y={y}, into the facecam (ends at y={cam_h})")
    if credit_box:
        cx0, cy0, cx1, cy1 = credit_box
        if not (x >= cx1 or y + h <= cy0 or y >= cy1):
            raise CampaignError("logo would overlap the credit pill")
    return w, h, x, y



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
