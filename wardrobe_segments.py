"""Keep long wardrobe uploads as independent, recoverable project segments."""
import copy
import json
import math
import re
import threading
from pathlib import Path

from workflow_core import WorkflowError, resolve_ffmpeg
from long_video_core import _run_ffmpeg

LOCK = threading.RLock()


def plan(duration, limit=15.0):
    if not math.isfinite(duration) or duration <= 0:
        raise WorkflowError('无法读取原片时长。')
    return [dict(index=i+1, start=i*limit, end=min(duration,(i+1)*limit),
                 duration=min(limit,duration-i*limit))
            for i in range(int(math.ceil((duration-1e-6)/limit)))]


def book_path(host, group_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{4,80}',str(group_id)):
        raise WorkflowError('分段项目编号无效。')
    return host.PROJECT_DIR/'runs'/'_wardrobe_segments'/f'{group_id}.json'


def read(host, job):
    meta=job.cast_continuity.get('wardrobe_segment') or {}
    if not meta:return {}
    path=book_path(host,meta['group_id'])
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def save(host, book):
    path=book_path(host,book['id'])
    temporary=path.with_suffix('.tmp')
    host.save_shot_manifest(temporary,book)
    temporary.replace(path)


def prepare(host, job, source, duration, limit=15.0):
    existing=job.cast_continuity.get('wardrobe_segment') or {}
    if existing:
        path=host._runs_record_file(existing.get('source'))
        if path:return path
    segments=plan(duration,limit)
    if len(segments)==1:return source
    job.update(stage=f'保留完整原片，正在拆分为 {len(segments)} 段',progress=1)
    clips=host.split_video_shots(source,[host.ShotBoundary(s['index'],s['start'],s['end']) for s in segments],job.run_dir/'segments')
    book=dict(id=job.id,mode=host.wardrobe_mode_from_job(job),duration=duration,segments=[],output='')
    for segment,clip in zip(segments,clips):
        child=job if segment['index']==1 else host.new_job(job.kind)
        if child is not job:
            child.cast_continuity=copy.deepcopy({k:v for k,v in job.cast_continuity.items() if k!='wardrobe_segment'})
            child.replacement_path=job.replacement_path
            child.update(status='succeeded',stage='分段原片已保存，请先打码',progress=0)
        child.cast_continuity['wardrobe_segment']=dict(group_id=job.id,source=str(clip),**segment)
        child.source_duration=segment['duration']
        host.persist_wardrobe_swap_job(child,book['mode'])
        book['segments'].append(dict(job_id=child.id,output='',**segment))
    with LOCK:save(host,book)
    job.log(f'原片 {duration:.3f} 秒已完整保留，拆为 '+ ' + '.join(f"{s['duration']:.3f}秒" for s in segments)+'；在分段列表逐段制作，全部完成后自动合成。')
    return clips[0]


def replace_member(host, previous, current):
    """Remasking a segment creates a new attempt but keeps its timeline slot."""
    with LOCK:
        book=read(host,previous)
        if not book:return
        meta=copy.deepcopy(previous.cast_continuity['wardrobe_segment'])
        meta['source']=str(host._run_artifact(current.run_dir,'reference',{'.mp4','.mov'}))
        current.cast_continuity['wardrobe_segment']=meta
        segment=book['segments'][meta['index']-1]
        segment.update(job_id=current.id,output='')
        book.update(output='',output_signature='')
        save(host,book)


def record_result(host, source, output):
    with LOCK:
        book=read(host,source)
        if not book:return
        segment=book['segments'][source.cast_continuity['wardrobe_segment']['index']-1]
        # Ignore a late result for a superseded remasking attempt.
        if segment['job_id']!=source.id:return
        segment['output']=str(output) if output else ''
        book.update(output='',output_signature='')
        save(host,book)


def assemble(host, job):
    with LOCK:
        book=read(host,job)
        if not book:return None
        outputs=[host._runs_record_file(s.get('output')) for s in book['segments']]
        if not all(outputs):return None
        signature=[(str(p),p.stat().st_mtime_ns) for p in outputs]
        signature=json.dumps(signature)
        existing=host._runs_record_file(book.get('output'))
        if existing and signature==book.get('output_signature'):return existing
        folder=book_path(host,book['id']).parent/book['id'];folder.mkdir(parents=True,exist_ok=True)
        # Conform each segment without retiming, then join in original order.
        conformed=[]
        info=host.inspect_video(outputs[0])
        with_audio=any(host.video_has_audio(p) for p in outputs)
        for segment,path in zip(book['segments'],outputs):
            target=folder/f"segment_{segment['index']:02d}.mp4"
            duration=segment['duration']
            command=[str(resolve_ffmpeg()),'-hide_banner','-loglevel','error','-y','-i',str(path)]
            audio_index=0
            if with_audio and not host.video_has_audio(path):
                command+=['-f','lavfi','-i','anullsrc=r=48000:cl=stereo'];audio_index=1
            command+=['-map','0:v:0','-vf',f'trim=duration={duration:.6f},setpts=PTS-STARTPTS,fps=30,scale={info.width}:{info.height}:force_original_aspect_ratio=decrease,pad={info.width}:{info.height}:(ow-iw)/2:(oh-ih)/2,setsar=1,tpad=stop_mode=clone:stop_duration={duration:.6f}',
                      '-t',f'{duration:.6f}','-c:v','libx264','-preset','veryfast','-crf','18','-pix_fmt','yuv420p']
            if with_audio:
                command+=['-map',f'{audio_index}:a:0','-af',f'atrim=duration={duration:.6f},asetpts=PTS-STARTPTS,apad=pad_dur={duration:.6f}',
                          '-c:a','aac','-b:a','160k','-ar','48000','-ac','2']
            else:command+=['-an']
            command+=['-movflags','+faststart',str(target)]
            _run_ffmpeg(command,'统一分段尺寸、帧率与音轨')
            conformed.append(target)
        joined=host.concatenate_videos(conformed,folder/'joined.mp4')
        output=host.conform_video_duration(joined,folder/'完整成片.mp4',book['duration'],with_audio=with_audio)
        book.update(output=str(output),output_signature=signature)
        save(host,book)
        job.log(f"全部 {len(outputs)} 段已合成为完整视频，保留原片约 {book['duration']:.3f} 秒。")
        return output


def public(host, job):
    book=read(host,job)
    if not book:return {}
    current=job.cast_continuity['wardrobe_segment']['index']
    output=host._runs_record_file(book.get('output'))
    return dict(group_id=book['id'],duration=book['duration'],current=current,
                output_url=f"/api/wardrobe-swap/segments/{book['id']}/output" if output else '', output_revision=str(output.stat().st_mtime_ns) if output else '',
                segments=[{**{k:s[k] for k in ('job_id','index','start','end','duration')},
                           'source_url':f"/api/jobs/{s['job_id']}/file/source",
                           'finished':bool(host._runs_record_file(s.get('output')))} for s in book['segments']])
