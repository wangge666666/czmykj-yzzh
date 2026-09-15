"""Additional people for the existing project forms; no separate workflow."""
import copy
import json
import re
import uuid

from flask import request
from workflow_core import WorkflowError
from cast_colors import COLOR_NAMES, normalize_plan, color_contract, color_model_prompt, validate_mentions
from video_proxy_rules import REMOVE_OVERLAY_TEXT, PROXY_DIALOGUE, FINAL_DIALOGUE


def submitted():
    try:
        items = json.loads(request.form.get('additional_people', '[]'))
    except (ValueError, TypeError):
        raise WorkflowError('新增人物数据无效，请刷新人物卡片。')
    if not isinstance(items, list) or len(items) > 3:
        raise WorkflowError('当前项目最多支持 4 个人物。')
    ids = set()
    for item in items:
        if not isinstance(item, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{8,60}', str(item.get('id', ''))) or item['id'] in ids:
            raise WorkflowError('新增人物编号无效或重复。')
        ids.add(item['id'])
        source = str(item.get('source', '')).strip()
        if len(source) > 100 or '@' in source:
            raise WorkflowError('人物位置说明最多 100 字，不填写 @ 素材。')
    return items


def collect(host, source, target, images, enabled=True):
    items = submitted()
    if items and not enabled:
        raise WorkflowError('新增人物请切换到有人物、服装参考的流程。')
    previous = {p['id']: p for p in source.cast_continuity.get('inline_cast', [])}
    records = []
    for number, item in enumerate(items, 2):
        uri = str(item.get('uri', '')).strip()
        host.wardrobe_object.asset(uri, 'Image')
        record = dict(id=item['id'], uri=uri, source=str(item.get('source', '')).strip(), image_index=len(images)+1)
        images.append(uri)
        upload = request.files.get('additional_clothing_' + item['id'])
        clothing = None
        if upload and upload.filename:
            folder = target.run_dir / 'inline_cast' / uuid.uuid4().hex
            folder.mkdir(parents=True, exist_ok=True)
            saved = host.save_upload(upload, folder, 'clothing')
            clothing = host.prepare_wardrobe_seedance_reference(target, saved, role='clothing_'+uuid.uuid4().hex, label=f'人物{number}服装')
        elif item.get('keep_clothing'):
            clothing = host._runs_record_file(previous.get(item['id'], {}).get('clothing', ''))
            if not clothing:
                raise WorkflowError(f'人物{number}的服装已失效，请重新上传。')
        if clothing:
            record.update(clothing=str(clothing), clothing_index=len(images)+1)
            images.append(str(clothing))
        records.append(record)
    if len(images) > 9:
        raise WorkflowError('人物、服装及其他参考图合计最多 9 张，请移除不需要的参考。')
    if target is not source:
        target.cast_continuity['inline_cast'] = records
        if source.cast_continuity.get('wardrobe_segment'):
            target.cast_continuity['wardrobe_segment'] = copy.deepcopy(source.cast_continuity['wardrobe_segment'])
        if enabled and source.cast_continuity.get('inline_color_plan'):
            target.cast_continuity['inline_color_plan'] = copy.deepcopy(source.cast_continuity['inline_color_plan'])
    return records


def remember(source, target):
    source.cast_continuity['inline_cast'] = copy.deepcopy(target.cast_continuity.get('inline_cast', []))
    if request.form.get('primary_clothing_optional') == 'true':
        source.clothing_path = target.clothing_path


def public(job):
    return [dict(id=p['id'], uri=p['uri'], source=p.get('source', ''),
                 clothing_url=f'/api/inline-cast/{job.id}/{p["id"]}/clothing' if p.get('clothing') else '')
            for p in job.cast_continuity.get('inline_cast', [])]


def contract(records, colored=False, color_plan=None, first_clothing=True):
    count = len(records) + 1
    if color_plan:
        plan = normalize_plan(color_plan)
        if len(plan) != count:
            raise WorkflowError(f'当前母版为 {len(plan)} 人，已选择 {count} 人；请按当前人数重新生成分色母版。')
        colored = True
    else:
        plan = normalize_plan([{}] + [dict(source=p.get('source', '')) for p in records])
    text = f'画面中需处理 {count} 个主要人物，逐人绑定身份与服装，禁止串脸、换位、漏人、加人或互换服装。'
    colors = [COLOR_NAMES[p['color']] for p in plan]
    if colored:
        text += color_contract(plan) + '；'.join(f'人物{i+1}=p{i+1}{color}模' for i, color in enumerate(colors)) + '。'
    else:
        text += '若视频已分色，则' + '、'.join(f'人物{i+1}对应{color}模' for i, color in enumerate(colors)) + '；未分色时按原片首次清晰同框位置识别人：'
        text += ('双人时一男一女则男性为人物1、女性为人物2；同性或不能判断时左侧为人物1、右侧为人物2。' if count == 2 else
                 '按首次清晰同框从左到右编号，未同框者按首次出现顺序补位。')
        text += ('已有白模或未变白的主要人物都按此对应替换，路人不修改。交叉走位和切镜后编号保持身份，不按新位置重排。')
    text += ('人物1仅使用@图片1的身份、五官、发型、体型与@图片2的服装。' if first_clothing else
             '人物1的身份、五官、发型、体型和服装均使用@图片1；未上传独立服装参考，使用此人物形象图中的服装。')
    for i, item in enumerate(records, 2):
        text += f'人物{i}' + (f'（原片中{item["source"]}）' if item.get('source') else '')
        text += f'仅使用@图片{item["image_index"]}的身份、五官、发型和体型；'
        text += f'服装仅使用@图片{item["clothing_index"]}。' if item.get('clothing_index') else f'未单独上传服装，此人的服装也使用@图片{item["image_index"]}中的服装。'
    return text + '各图片不得改变人物姿势、机位、景别、遮挡、裁切或运镜；面部恢复清晰自然，成片不得残留打码、色模或素材拼贴。'


def final_prompt(mode, records, image_count, custom='', colored=False, has_replacement=False, color_plan=None, first_clothing=True):
    custom = validate_mentions(custom.strip(), image_count)
    if len(custom) > 1000:
        raise WorkflowError('补充要求最多 1000 字。')
    text = '以@视频1为逐帧动作、站位、尺度、前后景、局部入画、遮挡、时序、机位、对焦、构图与运镜的唯一依据，保持完整时长和背景视差。'
    text += contract(records, colored, color_plan, first_clothing)
    environment_index = 2 + int(first_clothing)
    if mode == 'dynamic_object':
        text += '只按要求更换指定物品，保留其接触、握持与遮挡关系，背景保持原片。'
        if has_replacement: text += f'@图片{environment_index}仅提供目标物品外观。'
    elif mode == 'dynamic_scene':
        text += f'@图片{environment_index}仅提供新背景环境，替换绿底，保留原片人物动作、互动道具与镜头空间关系。'
    elif mode == 'dynamic':
        text += '背景、道具和次要人物完全保留原片。'
    else:
        text += f'@图片{environment_index}仅提供场景外观，动作和构图继续跟随视频。'
    text += '输出正常彩色成片，不添加字幕、标题、水印、边框或分屏。' + REMOVE_OVERLAY_TEXT + FINAL_DIALOGUE
    if custom: text += '\n修改要求：' + custom
    if len(text) > 2000: raise WorkflowError('总提示词过长，请精简补充要求和人物说明。')
    return text


def build_white_prompt(mode, count, sources=None, extra=''):
    if not 1 <= count <= 4: raise WorkflowError('人物数量需为 1–4 人。')
    if count == 1: return '', []
    sources = sources or [''] * (count - 1)
    if not isinstance(sources, list) or len(sources) != count - 1:
        raise WorkflowError('人物描述数量与人物数量不一致。')
    plan = normalize_plan([{}] + [dict(source=source) for source in sources])
    extra = validate_mentions(extra.strip(), 0)
    if len(extra) > 400: raise WorkflowError('白模补充要求最多 400 字。')
    return color_model_prompt(plan, green=mode == 'dynamic_scene') + PROXY_DIALOGUE + ('\n补充要求：'+extra if extra else ''), plan


def white_prompt(mode):
    try:
        count = int(request.form.get('inline_cast_count', '1'))
        sources = json.loads(request.form.get('inline_cast_sources', 'null'))
    except (ValueError, TypeError):
        raise WorkflowError('人物数量或描述无效。')
    return build_white_prompt(mode, count, sources, request.form.get('white_prompt', ''))


def prompt_preview(host, values):
    """Pure local preview using the exact builders used by paid submissions."""
    mode = values.get('mode')
    if mode not in {'person', 'clothing', 'scene', 'custom', 'dynamic', 'dynamic_object', 'dynamic_scene', 'rewrite'}:
        raise WorkflowError('无效的项目模式。')
    items = values.get('people', [])
    if not isinstance(items, list) or not 0 <= len(items) <= 3 or any(not isinstance(p, dict) for p in items):
        raise WorkflowError('提示词预览需要 1–4 个人物。')
    sources = [str(p.get('source', '')).strip() for p in items]
    count = len(items) + 1
    # Validate anchors in rewrite too, even though that workflow has no white pass.
    normalize_plan([{}] + [dict(source=s) for s in sources])
    has_replacement = values.get('has_replacement') is True
    extra_count = values.get('extra_count', 0)
    if not isinstance(extra_count, int) or not 0 <= extra_count <= 7:
        raise WorkflowError('其他参考图数量无效。')
    first_clothing = values.get('first_clothing', True) is True
    index = (2 + extra_count if mode == 'rewrite' else
             2 + int(has_replacement) if mode == 'dynamic_object' else
             2 if mode == 'dynamic' else 3) - int(not first_clothing)
    people = []
    for item, source in zip(items, sources):
        index += 1
        record = dict(source=source, image_index=index)
        if item.get('has_clothing') is True:
            index += 1
            record['clothing_index'] = index
        people.append(record)
    result = dict(count=count, image_count=index, final_prompt='', white_prompt='', final_error='', white_error='')
    if mode != 'rewrite':
        try:
            result['white_prompt'] = build_white_prompt(mode, count, sources, str(values.get('white_extra', '')))[0]
        except WorkflowError as exc:
            result['white_error'] = str(exc)
    try:
        if index > 9: raise WorkflowError('人物、服装及其他参考图合计最多 9 张。')
        if mode == 'rewrite':
            from wardrobe_full_rewrite import full_rewrite_prompt
            from wardrobe_intervals import normalize_segments
            duration = float(values.get('duration') or 0)
            if not 2 <= duration <= 15.08: raise WorkflowError('请先选择 2–15 秒完整原片，再设置改写区间。')
            segments = normalize_segments(values.get('segments'), duration, float(values.get('fps') or 30), require_prompts=True)
            result['final_prompt'] = full_rewrite_prompt(segments, 'references', extra_count, duration, people, first_clothing=first_clothing)
        else:
            custom = str(values.get('custom', '')).strip()
            if mode in {'person', 'clothing', 'scene', 'custom'} and custom == host.build_wardrobe_swap_prompt(mode):
                custom = ''
            result['final_prompt'] = final_prompt(mode, people, index, custom, values.get('colored') is True, has_replacement,
                                                  color_plan=values.get('color_plan'), first_clothing=first_clothing)
    except (WorkflowError, ValueError, TypeError) as exc:
        result['final_error'] = str(exc)
    return result
