"""Gallery selection across older and newer SQLite runtimes.

Uses unittest and in-memory DBs so this file can run on a target host without
installing pytest or touching a persistent library. SQLite 3.40.1 cannot resolve an
outer alias in a correlated subquery's ORDER BY, even when that column exists.
"""
import unittest

from frame_art_organizer import db, store


class GallerySQLiteCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        self.addCleanup(self.conn.close)
        self.sequence = 0

    def asset(self, name="photo.jpg", mode="auto", position=500):
        self.sequence += 1
        aid = store.insert_asset(
            self.conn, sha256=f"{self.sequence:064x}", original_path="/unused/original.jpg",
            original_name=name, bytes_=1, mime="image/jpeg", width=1600, height=1200,
            captured_at=None,
        )
        if mode != "auto":
            with self.conn:
                store.set_presentation(self.conn, aid, mode, position)
        return aid

    def derivative(self, aid, mode="auto", version=2):
        return store.add_derivative(
            self.conn, asset_id=aid, path="/unused/derivative.jpg", sha256="unused",
            fit_mode=mode, width=1600, height=900, pipeline_version=version,
        )

    def gallery(self, fit="auto", version=2, **kwargs):
        return store.list_gallery(self.conn, fit_mode=fit, pipeline_version=version, **kwargs)

    def test_empty_gallery_compiles(self):
        self.assertEqual(self.gallery(), [])

    def test_auto_without_a_policy_row_prefers_the_configured_render(self):
        aid = self.asset()
        current = self.derivative(aid)
        self.derivative(aid, "cover", 3)
        self.assertEqual(self.gallery()[0]["d_id"], current)

    def test_crop_policy_is_resolved_in_a_supported_correlation_scope(self):
        aid = self.asset(mode="crop", position=237)
        self.derivative(aid)
        current = self.derivative(aid, "crop-237")
        self.derivative(aid, "crop-500", 3)
        rows = self.gallery()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["d_id"], current)

    def test_matte_policy_prefers_the_whole_photo_render(self):
        aid = self.asset(mode="matte")
        current = self.derivative(aid, "matte")
        self.derivative(aid)
        self.assertEqual(self.gallery()[0]["d_id"], current)

    def test_rollback_prefers_the_configured_older_pipeline(self):
        aid = self.asset()
        current = self.derivative(aid, "cover", 1)
        self.derivative(aid)
        self.assertEqual(self.gallery(fit="cover", version=1)[0]["d_id"], current)

    def test_missing_current_render_falls_back_to_newest_with_a_stable_tiebreak(self):
        aid = self.asset(mode="crop", position=1000)
        self.derivative(aid, "cover", 1)
        self.derivative(aid)
        newest = self.derivative(aid, "crop-500")
        self.assertEqual(self.gallery()[0]["d_id"], newest)

    def test_unrendered_photo_is_still_listed(self):
        aid = self.asset(mode="matte")
        rows = self.gallery()
        self.assertEqual([r["id"] for r in rows], [aid])
        self.assertIsNone(rows[0]["d_id"])

    def test_collection_filter_and_name_sort_keep_correct_parameter_order(self):
        z = self.asset("z.jpg", mode="crop", position=0)
        a = self.asset("a.jpg", mode="matte")
        zd = self.derivative(z, "crop-0")
        ad = self.derivative(a, "matte")
        cid = store.get_or_create_collection(self.conn, "test")
        store.assign_collection(self.conn, z, cid)
        self.assertEqual([r["d_id"] for r in self.gallery(sort="name")], [ad, zd])
        self.assertEqual([r["d_id"] for r in self.gallery(filt=f"col:{cid}")], [zd])


if __name__ == "__main__":
    unittest.main()
