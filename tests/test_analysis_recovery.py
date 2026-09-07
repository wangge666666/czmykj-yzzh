"""Analysis output recovery uses local results, never a speculative paid retry."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import web_app as app
from performance_analysis import ArkPerformanceAnalyzer, AnalysisOutputError


class AnalysisRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.projects = (app.VIRTUAL_LONG_PROJECT, app.REAL_PERSON_LONG_PROJECT)
        self.patches = [
            patch.object(app, "PROJECT_DIR", self.root),
            patch.object(app, "JOBS", {}),
            patch.object(app, "touch_long_workspace"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def job(self, project):
        prefix = "real_long" if project == app.REAL_PERSON_LONG_PROJECT else "long"
        run = self.root / "runs" / ("20260907_100000_web_" + prefix + "_analyze_fixture")
        run.mkdir(parents=True, exist_ok=True)
        (run / "reference.mp4").write_bytes(b"synthetic-source")
        shots = []
        for index in (1, 2):
            folder = run / "shots" / str(index)
            folder.mkdir(parents=True, exist_ok=True)
            source = folder / "source.mp4"
            source.write_bytes(b"synthetic-shot" + bytes([index]))
            analysis = {"dialogue": [{"text": "synthetic", "speaker_slot": 1}],
                        "performance": [{"actor_slot": 1, "core_intent": "synthetic"}]}
            output = app.save_shot_manifest(folder / "performance.json", analysis)
            shots.append({"index": index, "start": index - 1, "end": index, "duration": 1,
                          "source_path": str(source), "performance_path": str(output),
                          "performance": analysis, "status": "ready", "cast_confirmed": False})
        return app.WebJob(id=prefix+"fixture", kind="long_performance", project=project,
                          run_dir=run, shots=shots)

    def analyzer(self, text):
        session = Mock()
        session.post.return_value = Mock(status_code=200)
        session.post.return_value.json.return_value = {"output_text": text}
        return ArkPerformanceAnalyzer("synthetic-key", session=session), session

    def test_received_invalid_output_keeps_shots_and_repeated_clicks_do_not_upload_or_charge(self):
        for project in self.projects:
            for reply in ("not json", '{"assignments":[{"shot_index":1,"slot":1,"character_id":1}]}'):
                with self.subTest(project=project, reply=reply):
                    job = self.job(project)
                    # Separate two received failure fixtures.
                    (job.run_dir / "cast_continuity_response.json").unlink(missing_ok=True)
                    analyzer, session = self.analyzer(reply)
                    with patch.object(app, "performance_analyzer", return_value=analyzer), \
                         patch.object(app, "TempFileMediaStore") as store, \
                         patch.object(app, "long_performance_video_reference", return_value=("synthetic-url", "")) as upload:
                        app.run_long_performance_analysis(job)
                        self.assertEqual(job.status, "succeeded", job.error)
                        self.assertTrue(job.cast_continuity["manual_review_required"])
                        self.assertIn("人工", job.stage)
                        self.assertTrue(all(shot["performance"]["dialogue"] for shot in job.shots))
                        self.assertTrue(all(shot["status"] == "ready" for shot in job.shots))
                        self.assertEqual(session.post.call_count, 1)
                        self.assertEqual(upload.call_count, 1)
                        receipt = json.loads((job.run_dir / "cast_continuity_response.json").read_text())
                        self.assertNotIn("synthetic-key", json.dumps(receipt))
                        self.assertNotIn("synthetic-url", json.dumps(receipt))
                        app.run_long_performance_analysis(job)
                        self.assertEqual(session.post.call_count, 1)
                        self.assertEqual(upload.call_count, 1)
                        # A process restart after receiving the reply also reuses it.
                        job.cast_continuity = {}
                        app.run_long_performance_analysis(job)
                        self.assertEqual(session.post.call_count, 1)
                        self.assertEqual(upload.call_count, 1)

    def test_unknown_post_or_permission_failure_never_retries_or_becomes_manual_success(self):
        for project in self.projects:
            for error in (app.ArkConnectionError("POST", "disconnected", 1),
                          app.WorkflowError("PREVIOUS_REQUEST_UNCERTAIN_CHECK_PROVIDER"),
                          app.ArkAPIError(403, "not permitted", "denied")):
                with self.subTest(project=project, error=str(error)):
                    job = self.job(project)
                    analyzer = Mock()
                    analyzer.analyze_cast_continuity.side_effect = error
                    counts, ranges = app.long_continuity_layout(job)
                    with self.assertRaises(type(error)):
                        app.analyze_long_cast_continuity(job, analyzer, "synthetic-url",
                            shot_slot_counts=counts, shot_ranges=ranges)
                    self.assertEqual(analyzer.analyze_cast_continuity.call_count, 1)
                    self.assertFalse(job.cast_continuity.get("manual_review_required"))

    def test_legacy_format_failure_recovers_through_normal_job_query_and_restart(self):
        for project in self.projects:
            with self.subTest(project=project):
                job = self.job(project)
                job.update(status="failed", error="跨镜人物连续性分析接口没有返回可解析的 JSON。")
                app.JOBS[job.id] = job
                response = app.app.test_client().get("/api/jobs/" + job.id)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json["cast_continuity"]["manual_review_required"])
                app.JOBS.clear()
                restored = app.restore_latest_long_video_job(project, job.id)
                self.assertEqual(restored.status, "succeeded")
                self.assertTrue(restored.cast_continuity["manual_review_required"])
                self.assertTrue(all(shot["performance"] for shot in restored.shots))

    def test_persisted_uncertain_error_survives_restart_without_false_success(self):
        job = self.job(app.VIRTUAL_LONG_PROJECT)
        job.update(status="failed", error="PREVIOUS_REQUEST_UNCERTAIN_CHECK_PROVIDER")
        app._persist_long_job(job)
        restored = app.restore_latest_long_video_job(job.project, job.id)
        self.assertEqual(restored.status, "failed")
        self.assertIn("UNCERTAIN", restored.error)
        self.assertFalse(app.recover_long_continuity_output(restored))

    def test_prior_manual_review_does_not_hide_a_later_unknown_request(self):
        job = self.job(app.REAL_PERSON_LONG_PROJECT)
        job.cast_continuity = app.manual_cast_continuity("earlier format failure")
        job.update(status="failed", error="PREVIOUS_REQUEST_UNCERTAIN_CHECK_PROVIDER")
        app._persist_long_job(job)
        restored = app.restore_latest_long_video_job(job.project, job.id)
        self.assertEqual(restored.status, "failed")
        self.assertIn("UNCERTAIN", restored.error)

    def test_empty_shot_records_do_not_count_as_completed_analysis(self):
        job = self.job(app.VIRTUAL_LONG_PROJECT)
        job.shots[0]["performance"] = {}
        job.update(status="failed", error="跨镜人物连续性分析接口没有返回可解析的 JSON。")
        self.assertFalse(app.recover_long_continuity_output(job))
        self.assertEqual(job.status, "failed")

    def test_review_cache_is_invalidated_by_changed_source_or_shot_range(self):
        job = self.job(app.VIRTUAL_LONG_PROJECT)
        counts, ranges = app.long_continuity_layout(job)
        job.cast_continuity = {**app.manual_cast_continuity("fixture"),
                              "input_signature": app.long_continuity_signature(job, counts, ranges)}
        changed = copy.deepcopy(ranges)
        changed[0]["end"] = 0.9
        self.assertIsNone(app.reusable_long_continuity(job, counts, changed))
        Path(job.shots[0]["source_path"]).write_bytes(b"changed")
        self.assertIsNone(app.reusable_long_continuity(job, counts, ranges))

    def test_review_does_not_skip_generation_mapping_confirmation(self):
        job = self.job(app.VIRTUAL_LONG_PROJECT)
        job.cast_continuity = app.manual_cast_continuity("fixture")
        with app.app.test_request_context(method="POST", data={"cast_confirmed": "false"}):
            with self.assertRaisesRegex(app.WorkflowError, "核对"):
                app.parse_long_shot_casts(job, 1)
        with app.app.test_request_context(method="POST", data={
            "cast_confirmed": "true", "shot_casts": json.dumps([
                {"index": 1, "actor_ids": [0]}, {"index": 2, "actor_ids": [1]},
            ]),
        }):
            with self.assertRaisesRegex(app.WorkflowError, "编号超出"):
                app.parse_long_shot_casts(job, 1)


if __name__ == "__main__":
    unittest.main()
