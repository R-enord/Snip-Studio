"""ClipUp campaign rules: required tags in every caption, logo placement, and the rendition queue waits for the logo file."""
import json, os, shutil, sys, tempfile, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import campaigns  # noqa: E402
import captions  # noqa: E402

CLIP = {"title": "lucia walks into a club", "hook": "lucia walks into a purple-lit club full of dancers in GTA VI",
        "streamer": "dylan", "name": "Dylan Page", "account": "a1"}
CAMP = "clipup_gta6_logo"


class CaptionTests(unittest.TestCase):
    def test_required_tags_in_every_platform_text(self):
        out = captions.build(dict(CLIP, campaigns=[CAMP]))
        for k in ("tiktok", "instagram", "youtube", "x"):
            low = out[k].lower()
            self.assertIn("#gta6", low, k)
            self.assertIn("#clipup", low, k)

    def test_required_tags_not_repeated(self):
        tags = [t.lower() for t in captions.build(dict(CLIP, campaigns=[CAMP]))["tiktok"].split() if t.startswith("#")]
        self.assertEqual(len(tags), len(set(tags)))

    def test_normal_clip_has_no_campaign_tags(self):
        out = captions.build(dict(CLIP))
        for v in out.values():
            self.assertNotIn("#clipup", v.lower())


class LogoPlacementTests(unittest.TestCase):
    def test_top_right_at_own_size(self):
        self.assertEqual(campaigns.logo_position(300, 150, 180, (28, 150, 300, 196)), (1080 - 300 - 40, 40))

    def test_refuses_to_shrink_a_tall_logo(self):
        with self.assertRaises(campaigns.CampaignError):
            campaigns.logo_position(300, 200, 180)

    def test_refuses_overlap_with_credit_pill(self):
        with self.assertRaises(campaigns.CampaignError):
            campaigns.logo_position(1000, 150, 180, (28, 150, 300, 196))

    def test_refuses_logo_wider_than_frame(self):
        with self.assertRaises(campaigns.CampaignError):
            campaigns.logo_position(1100, 100, 180)


class RenditionQueueTests(unittest.TestCase):
    def setUp(self):
        import renderer
        self.R = renderer
        self.root = tempfile.mkdtemp(prefix="sr_camp_")
        self.cand = os.path.join(self.root, "candidates")
        os.makedirs(os.path.join(self.cand, "dylan", "2026-11-21"))
        self.logo = os.path.join(self.root, "logo.png")
        self.patches = [mock.patch.object(self.R, "CANDIDATES", self.cand)]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in self.patches: p.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def sidecar(self, **extra):
        d = {"id": "dylan_test1", "status": "final", "title": "t", "approved": True, "campaigns": [CAMP],
             "created": "2026-11-21T13:00:00Z", "day": "2026-11-21", "stream_id": "dylan@local_x", "source": "x.mp4"}
        d.update(extra)
        p = os.path.join(self.cand, "dylan", "2026-11-21", "dylan_test1.json")
        json.dump(d, open(p, "w", encoding="utf-8"))
        return p

    def test_waits_until_logo_file_exists(self):
        self.sidecar()
        with mock.patch.dict(campaigns.CAMPAIGNS[CAMP], {"logo": self.logo}):
            self.assertEqual(self.R.campaign_todo({}), [])
            open(self.logo, "wb").write(b"png")
            self.assertEqual(len(self.R.campaign_todo({})), 1)

    def test_skips_unapproved_and_already_rendered(self):
        open(self.logo, "wb").write(b"png")
        with mock.patch.dict(campaigns.CAMPAIGNS[CAMP], {"logo": self.logo}):
            self.sidecar(approved=False)
            self.assertEqual(self.R.campaign_todo({}), [])
            self.sidecar(approved=True, campaign_renditions={CAMP: "x.mp4"})
            self.assertEqual(self.R.campaign_todo({}), [])


class TagTests(unittest.TestCase):
    def test_tag_and_untag_round_trip(self):
        root = tempfile.mkdtemp(prefix="sr_tag_")
        try:
            p = os.path.join(root, "c.json")
            json.dump({"id": "c"}, open(p, "w", encoding="utf-8"))
            self.assertEqual(campaigns.set_campaigns(p, [CAMP]), [CAMP])
            self.assertEqual(campaigns.set_campaigns(p, [CAMP], remove=True), [])
            with self.assertRaises(campaigns.CampaignError):
                campaigns.set_campaigns(p, ["not_a_campaign"])
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
