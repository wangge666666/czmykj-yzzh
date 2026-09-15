import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import web_app as host
import inline_cast
from wardrobe_dynamic import DYNAMIC_WHITE_PROMPT, DYNAMIC_SCENE_WHITE_PROMPT, DYNAMIC_FINAL_PROMPT
from video_proxy_rules import BARE_PROXY_BODY, REMOVE_OVERLAY_TEXT


class WardrobeProxyRulesTests(unittest.TestCase):
    def test_single_and_colored_proxy_rules_reach_real_options(self):
        prompts = [DYNAMIC_WHITE_PROMPT, DYNAMIC_SCENE_WHITE_PROMPT, host.WARDROBE_SAFE_WHITE_MODEL_PROMPT]
        for count in (2, 3, 4):
            for mode in ('dynamic', 'dynamic_object', 'dynamic_scene'):
                prompt, plan = inline_cast.build_white_prompt(mode, count)
                self.assertEqual(plan[0]['color'], 'red')
                self.assertEqual(plan[1]['color'], 'white')
                self.assertEqual([p['color'] for p in plan], ['red','white','yellow','blue'][:count])
                prompts.append(prompt)
        for prompt in prompts:
            with self.subTest(prompt=prompt[:35]), patch.object(host, 'resolved_seedance_ratio', return_value='9:16'):
                submitted = host.build_wardrobe_white_model_options(Path('source.mp4'), 5, prompt_override=prompt)['prompt']
                self.assertEqual(submitted, prompt)
                self.assertIn(BARE_PROXY_BODY, submitted)
                self.assertLess(submitted.index(BARE_PROXY_BODY), 150)
                self.assertIn('原片衣物的轮廓、厚度与外扩部分不作为保留依据', submitted)
                self.assertNotIn('不能只染衣服', submitted)
                self.assertNotIn('不得将衣服染白', submitted)
                self.assertNotIn('不互换动作、台词或服装', submitted)
                self.assertIn(REMOVE_OVERLAY_TEXT, submitted)
                self.assertNotIn('字幕、花字、特效包装全部保留', submitted)
        self.assertIn(REMOVE_OVERLAY_TEXT, DYNAMIC_FINAL_PROMPT)
        self.assertIn('唯一服装', DYNAMIC_FINAL_PROMPT)
        self.assertNotIn(BARE_PROXY_BODY, DYNAMIC_FINAL_PROMPT)

    def test_dynamic_white_cleans_source_and_generated_proxy_without_changing_original(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, mosaic, raw, conformed = [root/name for name in ('source.mp4', 'mosaic.mp4', 'raw.mp4', 'white_model.mp4')]
            for file in (source, mosaic, raw, conformed): file.write_bytes(b'original')
            job = host.WebJob(id='proxy-rules', kind='wardrobe_prepare_dynamic', run_dir=root, mosaic_path=mosaic)
            def clean(src, dst, **kwargs):
                dst.write_bytes(b'cleaned'); return dst, {'changed':True}
            def generate(child, **kwargs):
                child.status='succeeded';child.output_path=raw
            with (
                patch.dict(host.JOBS, {}, clear=True),
                patch.object(host, 'inspect_video', return_value=Mock(duration=5)),
                patch.object(host, 'resolved_seedance_ratio', return_value='9:16'),
                patch.object(host, 'sanitize_video_visible_text_if_needed', side_effect=clean) as cleanup,
                patch.object(host.wardrobe_audio, 'attach', side_effect=lambda src,original,dst:src) as audio,
                patch.object(host, 'run_generation', side_effect=generate) as generation,
                patch.object(host, 'conform_video_duration', return_value=conformed),
            ):
                output = host.run_wardrobe_white_model(job, source, preserve_scene=True, prompt_override=DYNAMIC_WHITE_PROMPT)
            self.assertEqual(cleanup.call_count, 2)
            self.assertEqual(cleanup.call_args_list[0].args[0], mosaic)
            self.assertEqual(cleanup.call_args_list[1].args[0], conformed)
            self.assertEqual(cleanup.call_args_list[0].kwargs, {'preserve_audio':True, 'overlay_only':True})
            self.assertEqual(cleanup.call_args_list[1].kwargs, {'preserve_audio':False, 'overlay_only':True})
            self.assertEqual(generation.call_args.kwargs['depth_path'], audio.call_args_list[0].args[0])
            self.assertEqual(audio.call_count, 2)
            self.assertTrue(all(call.args[1] == source for call in audio.call_args_list))
            self.assertEqual(output.name, 'white_model_no_subtitles.mp4')
            self.assertEqual(source.read_bytes(), b'original')
            self.assertEqual(mosaic.read_bytes(), b'original')

    def test_final_cleanup_preserves_audio_and_is_scoped_to_wardrobe(self):
        with patch.object(host, 'sanitize_video_visible_text_if_needed', return_value=(Path('cleaned.mp4'), {'changed':True})) as clean:
            for mode in ('dynamic', 'dynamic_object', 'dynamic_scene', 'person'):
                job = host.WebJob(id='final-'+mode, kind='wardrobe_generate_'+mode, run_dir=Path('.'))
                self.assertEqual(host.clean_wardrobe_generated_subtitles(job, Path('download.mp4')), Path('cleaned.mp4'))
                self.assertEqual(clean.call_args.kwargs, {'preserve_audio':True, 'overlay_only':True})
            clean.reset_mock()
            other = host.WebJob(id='other', kind='unrelated', run_dir=Path('.'))
            self.assertEqual(host.clean_wardrobe_generated_subtitles(other, Path('original.mp4')), Path('original.mp4'))
            clean.assert_not_called()


if __name__ == '__main__': unittest.main()
