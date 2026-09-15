"""Keep source dialogue available to wardrobe video edits and their downloads."""
import json
from pathlib import Path

from long_video_core import conform_video_duration, mux_original_audio, strip_video_audio, video_has_audio
from video_proxy_rules import FINAL_DIALOGUE
from workflow_core import WorkflowError, inspect_video


def original(host, job):
    try:
        return host.wardrobe_prepare_source(job)
    except WorkflowError:
        # Uploaded white masters can be used without a separately saved original.
        return job.mosaic_path or job.white_model_path


def attach(video, source, output):
    if Path(video).resolve() == Path(output).resolve():
        return Path(video)
    if source and video_has_audio(source):
        return mux_original_audio(video, source, output, preserve_video_duration=True)
    return strip_video_audio(video, output)


def match_source_duration(video, source):
    duration = inspect_video(source).duration
    actual = inspect_video(video).duration
    if actual < duration - 0.5:
        raise WorkflowError('生成画面明显短于原片，不能仅补声音；请重新生成当前段，保留完整动作与对白。')
    if abs(actual - duration) > 0.02:
        return conform_video_duration(video, video.with_name(video.stem + '_audio_timeline.mp4'), duration, with_audio=False)
    return video


def prepare(host, job, depth_path, depth_reference, options):
    if not job.kind.startswith('wardrobe_generate_'):
        return depth_path, options
    enabled = bool(options.get('generate_audio', True))
    if enabled and FINAL_DIALOGUE not in options['prompt']:
        options = {**options, 'prompt': options['prompt'] + '\n' + FINAL_DIALOGUE}
    if depth_reference or not depth_path:
        return depth_path, options
    source = depth_path
    manifest = job.run_dir / 'wardrobe_manifest.json'
    if manifest.is_file():
        parent_id = json.loads(manifest.read_text(encoding='utf-8')).get('source_job_id')
        if parent_id:
            try:
                parent = host.get_job(parent_id)
            except WorkflowError:
                parent = host.restore_wardrobe_swap_job(parent_id)
                if parent is None:
                    raise WorkflowError('无法恢复原片任务，请先在项目中重新打开原片。')
            source = original(host, parent) or depth_path
    job.cast_continuity['wardrobe_audio'] = dict(source=str(source), enabled=enabled)
    if enabled:
        # Also repairs a legacy silent white master before it is sent to Seedance.
        depth_path = attach(depth_path, source, job.run_dir / 'reference_original_audio.mp4')
        job.depth_path = depth_path
        if not video_has_audio(source):
            options = {**options, 'generate_audio': False}
        job.log('参考视频已携带原片声音，成片将保留同一音轨并按对白生成口型。' if video_has_audio(source)
                else '原片没有音轨；本次不新增对白。')
    return depth_path, options


def finish(host, job, output):
    if not job.kind.startswith('wardrobe_generate_'):
        return output
    settings = job.cast_continuity.get('wardrobe_audio')
    record = job.run_dir / 'job.json'
    if not settings and record.is_file():
        settings = json.loads(record.read_text(encoding='utf-8')).get('wardrobe_audio')
    if not settings or not settings.get('enabled'):
        return output
    source = host._runs_record_file(settings.get('source'))
    if not source:
        raise WorkflowError('找不到已保存的原片音轨来源，已保留云端下载文件；请恢复原片后重试下载。')
    timed = match_source_duration(output, source)
    result = attach(timed, source, output.with_name(output.stem + '_original_audio.mp4'))
    job.log('已恢复原片对白、音乐与环境音，保持原时间轴。' if video_has_audio(source) else '原片无声音，成片保持静音。')
    return result
