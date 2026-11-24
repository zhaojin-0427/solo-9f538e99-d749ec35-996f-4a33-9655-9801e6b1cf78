"""存储合并、结论保留与 Flask 端到端自测。"""
import io
import os
import tempfile
import unittest

from app import create_app
from app import service, storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        self.conn = storage.get_db(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    @staticmethod
    def csv(text):
        return text.encode("utf-8")

    def test_upload_and_overwrite_same_minute(self):
        incoming = storage.parse_csv(self.csv(
            "timestamp,reading\n2026-11-01 00:00,0\n"
            "2026-11-01 00:15,30\n2026-11-01 00:30,60\n"))
        total, ow = storage.merge_upload(self.conn, incoming)
        self.assertEqual((total, ow), (3, 0))
        # 同刻覆盖
        incoming2 = storage.parse_csv(self.csv(
            "timestamp,reading\n2026-11-01 00:15,33\n"
            "2026-11-01 00:45,90\n"))
        total, ow = storage.merge_upload(self.conn, incoming2)
        self.assertEqual((total, ow), (4, 1))
        rows = dict(storage.load_readings(self.conn))
        import datetime as _dt
        self.assertEqual(rows[_dt.datetime(2026, 11, 1, 0, 15)], 33)

    def test_rollback_rejects_whole_batch_and_keeps_old(self):
        storage.merge_upload(self.conn, storage.parse_csv(self.csv(
            "timestamp,reading\n2026-11-01 00:00,100\n"
            "2026-11-01 00:15,130\n")))
        bad = storage.parse_csv(self.csv(
            "timestamp,reading\n2026-11-01 00:30,120\n"
            "2026-11-01 00:45,120\n"))  # 00:30 相对 00:15 的 130 回卷
        with self.assertRaises(storage.UploadError) as ctx:
            storage.merge_upload(self.conn, bad)
        self.assertIn("回卷", str(ctx.exception))
        self.assertIn("2026-11-01 00:30", str(ctx.exception))
        rows = storage.load_readings(self.conn)
        self.assertEqual(len(rows), 2)  # 旧数据原样保留

    def test_unaligned_period_rejected(self):
        from datetime import datetime
        bad = {datetime(2026, 11, 1, 0, 1): 0,
               datetime(2026, 11, 1, 0, 16): 30}
        with self.assertRaises(storage.UploadError):
            storage.merge_upload(self.conn, bad)

    def test_cap_violation_after_merge_rejected(self):
        storage.save_config(self.conn, 5, None, None)
        storage.merge_upload(self.conn, storage.parse_csv(self.csv(
            "timestamp,reading\n2026-11-01 00:00,0\n"
            "2026-11-01 00:15,30\n")))
        with self.assertRaises(storage.UploadError):
            storage.merge_upload(self.conn, storage.parse_csv(self.csv(
                "timestamp,reading\n2026-11-01 00:30,200\n")))

    def test_initial_conclusion_preserved_current_updates(self):
        storage.merge_upload(self.conn, storage.parse_csv(self.csv(
            "timestamp,reading\n2026-11-01 00:00,0\n"
            "2026-11-01 00:15,30\n2026-11-01 00:30,90\n")))
        # 初核：others_breach（报窗0=30，窗1固定60）
        r1 = service.run_audit(self.conn, "10", "30", "2026-11-01 00:00")
        self.assertEqual(r1.status, "others_breach")
        initial = storage.latest_audit(self.conn, "initial")
        # 改参数再核：当前变化，初核不变
        r2 = service.run_audit(self.conn, "10", "60", "2026-11-01 00:15")
        self.assertEqual(r2.status, "feasible")
        self.assertEqual(
            storage.latest_audit(self.conn, "initial")["id"], initial["id"])
        self.assertNotEqual(
            storage.latest_audit(self.conn, "current")["id"], initial["id"])
        # 失败的核对不覆盖当前结论
        before = storage.latest_audit(self.conn, "current")["id"]
        with self.assertRaises(service.AuditInputError):
            service.run_audit(self.conn, "1", "60", "2026-11-01 00:15")
        self.assertEqual(
            storage.latest_audit(self.conn, "current")["id"], before)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "web.db")
        self.app = create_app(self.db)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_flow_via_http(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        data = {"csv": (io.BytesIO(
            b"timestamp,reading\n2026-11-01 00:00,0\n"
            b"2026-11-01 00:15,30\n2026-11-01 00:30,90\n"),
            "r.csv")}
        resp = self.client.post("/upload", data=data,
                                content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 302)
        # 核对通过：报窗1=60
        resp = self.client.post("/audit", data={
            "per_minute_cap": "10", "peak_wh": "60",
            "reported_window": "2026-11-01 00:15"})
        self.assertEqual(resp.status_code, 302)
        page = self.client.get("/").data.decode()
        self.assertIn("核对通过", page)
        csv_out = self.client.get("/windows.csv").data.decode()
        self.assertIn("window_start,window_index,energy_wh", csv_out)
        # 坏批次被拒
        bad = {"csv": (io.BytesIO(
            b"timestamp,reading\n2026-11-01 00:45,80\n"), "b.csv")}
        resp = self.client.post("/upload", data=bad,
                                content_type="multipart/form-data")
        self.assertIn("整批拒绝",
                      self.client.get("/").data.decode())
        # 重置
        self.assertEqual(self.client.post("/reset").status_code, 302)
        self.assertEqual(storage.load_readings(
            storage.get_db(self.db)), [])

    def test_bad_csv_rejected(self):
        bad = {"csv": (io.BytesIO(b"a,b\nx,y\n"), "b.csv")}
        self.client.post("/upload", data=bad,
                         content_type="multipart/form-data")
        self.assertIn("整批拒绝", self.client.get("/").data.decode())


if __name__ == "__main__":
    unittest.main(verbosity=2)
