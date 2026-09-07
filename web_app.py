from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from waitress import serve
from werkzeug.datastructures import FileStorage

from face_mosaic import render_eye_privacy_image, render_face_mosaic_video
from performance_analysis import (
    ArkPerformanceAnalyzer,
    build_performance_prompt,
    build_white_model_performance_prompt,
    sanitize_motion_evidence,
)
from seedance_web import SEEDANCE_WEB
from long_video_core import (
    ShotBoundary,
    cluster_video_shot_scenes,
    concatenate_videos,
    conform_video_duration,
    detect_people_boxes,
    detect_video_shots,
    estimate_people_count,
    extend_video_with_trailing_hold,
    mux_original_audio,
    save_shot_manifest,
    split_video_shots,
    strip_video_audio,
)

from workflow_core import (
    atomic_write_text,
    DEFAULT_ARK_BASE_URL,
    DEFAULT_PROMPT,
    DEFAULT_SEEDREAM_MODEL,
    DEFAULT_SEEDANCE_MODEL,
    DEFAULT_SEEDANCE_25_MODEL,
    PROJECT_DIR,
    MAX_DEPTH_VIDEO_BYTES,
    MAX_DEPTH_VIDEO_SECONDS,
    MAX_FREE_UPLOAD_BYTES,
    ArkAPIError,
    ArkAssetsClient,
    ArkConnectionError,
    ArkVideoClient,
    TempFileMediaStore,
    TemporaryFileServer,
    TemporaryPublicTunnel,
    TemporaryVideoServer,
    TosMediaStore,
    WorkflowError,
    build_multi_seedance_payload,
    build_scene_seedance_payload,
    build_seedance_payload,
    build_video_reference_seedance_payload,
    download_file,
    extract_scene_reference_frames,
    load_env_file,
    run_depth_generation,
    save_job_record,
    timestamped_run_dir,
    inspect_video,
    resolve_ffmpeg,
    validate_seedance_reference_video,
)

from face_mosaic import YuNetFaceDetector, ensure_face_model


WEB_DIR = PROJECT_DIR / "web"
REAL_FINAL_VIDEO_MODELS = {
    DEFAULT_SEEDANCE_MODEL: {
        "id": "seedance_2_0",
        "label": "Seedance 2.0",
        "resolutions": ("480p", "720p", "1080p"),
    },
    DEFAULT_SEEDANCE_25_MODEL: {
        "id": "seedance_2_5",
        "label": "Seedance 2.5",
        "resolutions": ("480p", "720p"),
    },
}
TERMINAL_STATUSES = {"succeeded", "failed"}
RESTORED_SCENE_JOB_ID = "latest-person-scene"
RESTORED_SCENE_ONLY_JOB_ID = "latest-scene-only"
DEFAULT_SCENE_EXTRACTION_PROMPT = (
    "以图2作为最终画面的唯一构图、机位、透视、焦段和裁切基准。删除画面中的所有人物、人体、衣物、"
    "人物携带物和人物阴影，并根据图1、图2、图3中可见的环境信息自然重建被遮挡的背景。严格保留原场景的"
    "建筑结构、地面、墙面、家具、固定陈设、道路、植被、天空、色彩、材质和光照关系；不要改变镜头位置，"
    "不要重新设计场景，不要增加任何人物、文字、标志或新物体。输出一张干净、真实、完整、可直接作为视频"
    "背景参考的空场景图片，画面比例与图2一致。"
)
DEFAULT_PERSON_TRIVIEW_PROMPT = (
    "图1、图2、图3来自同一段视频。识别三张图中同一个主要人物，以图2的人脸、发型、体型和身份特征为主，"
    "图1与图3只用于补足侧面和被遮挡细节。生成一张专业角色三视图参考板，在同一张中性浅灰背景画布中从左到右"
    "排列正面、严格侧面、背面三个完整全身视图。必须保持同一人物身份、年龄、五官、发型、身材比例和原始服装一致，"
    "脚部、手部和头顶完整入画，三个视图等比例、同高度、站姿自然。忽略原视频背景、动作、光影和运动模糊。"
    "不要添加文字、标签、边框、道具、额外人物或新的服装设计。输出画布必须为横向16:9。"
)
DEFAULT_CLOTHING_PERSON_TRIVIEW_PROMPT = (
    "图1、图2、图3来自同一段视频。只提取三张图中同一个主要人物的身份特征，以图2的人脸、发型、肤色、体型和"
    "身体比例为主，图1与图3只用于补足侧面和被遮挡细节。生成一张专业角色三视图参考板，在同一张中性浅灰背景"
    "画布中从左到右排列正面、严格侧面、背面三个完整全身视图。必须保持同一人物身份、年龄、五官、发型、肤色、"
    "身高和身材比例一致，但不要提取、复制或重建原视频中的任何服装。三个视图统一穿纯白色、无图案、无Logo、"
    "无装饰的贴身圆领长袖基础底衫和纯白色贴身基础长裤，赤脚；移除原片的外套、内搭、裙装、裤装、鞋履、帽子、"
    "首饰、腰带和全部服装配饰。脚部、手部和头顶完整入画，三个视图等比例、同高度、自然标准站姿。忽略原视频背景、"
    "动作、光影和运动模糊。不要添加文字、标签、边框、道具或额外人物。输出画布必须为横向16:9。"
)
WARDROBE_NEUTRAL_PERSON_TRIVIEW_PROMPT = (
    "图1、图2、图3来自同一段视频。只提取三张图中同一个主要人物的身份特征，以图2的人脸、发型、肤色、体型和"
    "身体比例为主，图1与图3只用于补足侧面和背面细节。生成一张专业人物三视图参考板，在纯白背景画布中从左到右"
    "排列正面、严格侧面、背面三个完整全身视图。必须保持同一人物的身份、年龄、五官、发型、肤色、身高和身材比例，"
    "但不要提取、复制、补全或重建原视频中的任何服装。三个视图统一穿纯白色、无图案、无Logo、无装饰、不透明的"
    "短袖圆领T恤和纯白色无图案短裤，赤脚。彻底移除原片的上装、下装、外套、内搭、裙装、裤装、鞋袜、帽子、"
    "首饰、腰带、包袋和全部服装配饰，不得保留原服装的颜色、材质、纹样、版型、领口、袖型或轮廓。脚部、手部和"
    "头顶完整入画，三个视图等比例、同高度、自然标准站姿。忽略原视频背景、动作、光影和运动模糊。不要添加文字、"
    "标签、边框、道具或额外人物。输出画布必须为横向16:9。"
)
DEFAULT_CLOTHING_TRIVIEW_PROMPT = (
    "图1、图2、图3来自同一段视频。准确提取主要人物正在穿着的完整服装、鞋履、头饰和固定配饰，以图2为主并结合"
    "图1与图3补足遮挡部分。生成一张专业服装三视图参考板，在纯白背景画布中从左到右排列服装本体的正面、严格侧面、"
    "背面三个完整视图。采用无人体的悬空陈列或平铺商品图效果，忠实保留原服装的版型、层次、长度、颜色、材质、纹样、"
    "扣件和配饰位置，不得重新设计、简化或增加元素。忽略人物身份、原视频背景、动作、光影和运动模糊。"
    "画面中严禁出现真人、模特、人体、人体局部、皮肤、人脸、人头、手脚、人台、人体模型、衣架、玩偶或任何穿着者轮廓；"
    "只允许服装、鞋履、头饰和固定配饰本体。不要添加文字、标签、边框、阴影或场景。"
)
REAL_PERSON_LONG_PROJECT = "real_person_long_video_replication"
VIRTUAL_LONG_PROJECT = "long_video_replication"


def real_long_actor_local_reference_path(
    job: Any,
    actor: dict[str, Any],
    actor_index: int,
    kind: str,
) -> Path | None:
    """Return a saved, local actor reference without exposing its filesystem path."""
    if getattr(job, "project", "") != REAL_PERSON_LONG_PROJECT or kind not in {"person", "clothing"}:
        return None
    run_dir_value = getattr(job, "run_dir", None)
    if not run_dir_value:
        return None
    run_dir = Path(run_dir_value).expanduser().resolve()
    runs_root = (PROJECT_DIR / "runs").resolve()
    if not run_dir.is_dir() or not run_dir.is_relative_to(runs_root):
        return None

    candidates: list[Path] = []
    if kind == "person":
        for key in ("local_person_source", "original_person_source", "person_source"):
            raw_value = str(actor.get(key) or "")
            if raw_value and not raw_value.startswith("asset://"):
                candidates.append(Path(raw_value))
        candidates.extend(run_dir.glob(f"real_person_{actor_index}_original.*"))
    else:
        raw_value = str(actor.get("clothing_source") or "")
        if raw_value:
            candidates.append(Path(raw_value))
        candidates.extend(run_dir.glob(f"real_clothing_{actor_index}.*"))

    for candidate in candidates:
        path = candidate.expanduser().resolve()
        if path.is_file() and path.is_relative_to(run_dir):
            return path
    return None


REAL_PERSON_DUAL_REFERENCE_VERSION = "portrait-three-view-v2"
REAL_PERSON_SKETCH_PROMPT = (
    "将图1转换为高质量黑白铅笔素描人物参考图。严格保持同一人物的脸型、五官比例、眉眼形状、鼻唇结构、"
    "发型轮廓、头部角度、表情、身体比例、姿势、画面裁切与背景构图，不得美化、换脸、改变年龄或重新设计。"
    "若图1是左侧脸部特写、右侧依次为正面全身、侧面全身、背面全身的四栏身份板，必须完整保留这四栏的数量、顺序、比例和横向排列；"
    "若图1是同一人物按正面、侧面、背面从左到右排列的三视图身份板，必须完整保留三个视角的数量、顺序、比例和横向排列；"
    "脸部特写和全部视图属于同一个人物身份，不得解释成多人，不得融合视角或复制出额外人物。"
    "使用细腻石墨线稿、自然交叉排线与轻柔明暗塑形，五官清晰可辨，整体接近写实人物结构稿。"
    "不要添加原图之外的视角、文字、边框、标志、水印或额外人物。"
)

PERSON_TRIVIEW_SIZE = "2560x1440"
PERSON_TRIVIEW_REQUIRED_CONSTRAINT = (
    "强制输出规格：整张画布必须为横向16:9，正面、侧面、背面三个人物视图横向并排且完整入画。"
)
CLOTHING_PERSON_TRIVIEW_REQUIRED_CONSTRAINT = (
    "项目5不可覆盖的强制约束：人物三视图只保留人物身份、面部、发型、肤色、体型和身体比例；"
    "必须将参考帧中可见的全部原片服装、鞋履、帽子和配饰彻底移除并替换成统一的纯白色无图案贴身基础底衫与"
    "纯白色无图案贴身基础长裤。严禁在人物图中保留、模仿、补全、重建或重新设计原片服装，严禁出现原服装的"
    "颜色、材质、纹样、版型、领口、袖型、下摆、鞋履或配饰特征。"
)
CLOTHING_PERSON_TRIVIEW_VERSION = "project5-white-base-v1"
WARDROBE_NEUTRAL_PERSON_TRIVIEW_REQUIRED_CONSTRAINT = (
    "衣装智换人物提取不可覆盖规则：人物三视图只保留人物身份、面部、发型、肤色、体型和身体比例。"
    "正面、侧面、背面三个视图必须全部统一穿纯白色无图案短袖圆领T恤与纯白色无图案短裤，赤脚；"
    "绝对禁止保留、模仿、补全或重新设计原片中的任何服装、鞋袜、帽子、首饰、包袋和穿戴配饰。"
)
WARDROBE_NEUTRAL_PERSON_TRIVIEW_VERSION = "wardrobe-white-tshirt-shorts-v1"
CLOTHING_TRIVIEW_REQUIRED_CONSTRAINT = (
    "强制输出规格：纯白色背景，只展示服装、鞋履、头饰和固定配饰本体的正面、侧面、背面三视图。"
    "严禁出现真人、模特、人体、人体局部、皮肤、人脸、人头、手脚、人台、人体模型、衣架、玩偶、假人或任何穿着者轮廓。"
)
DEFAULT_CLOTHING_ONLY_PROMPT = (
    "参考@视频1，完整保留原视频主角的身份、面部、发型、体型、动作、表情和视线；人物身份、五官、发型、肤色和"
    "身体比例严格参考@图片1。只将主角服装替换为@图片2中的新服装，包含上装、下装、鞋履、头饰和配饰，必须保持"
    "新服装从头到尾一致。@图片1中的纯白色基础底衫和基础长裤只用于表达人物身体轮廓，不是最终服装，最终画面必须"
    "由@图片2的新服装将其完整替换；除非@图片2明确包含白色打底层，否则不得露出或保留任何白色底衫。"
    "原视频场景必须完整保留，背景、空间结构、固定陈设、色彩、材质、透视和环境光照严格参考@图片3，不得改变、扩建或"
    "重新设计场景。完全复刻原视频主角的脚步、抬手、摆臂、躯干倾斜、弹跳重心、转向、停顿和定格姿势，所有动作起始时间、"
    "落点、强拍定格与节奏变化均与原视频帧级对齐；镜头机位、焦段、景别、裁切、透视、运镜轨迹和画面构图完全复刻原视频。"
    "禁止改变人物身份、脸型、发型和体型，禁止残留原服装、混穿、换装闪烁、串脸、重影、额外人物、文字或水印。"
    "@视频1仅作为深度、动作、遮挡和镜头约束，不要输出黑白深度风格，最终输出正常彩色视频。"
)
CLOTHING_ONLY_GENERATION_REQUIRED_CONSTRAINT = (
    "项目5最终成片强制约束：@图片2是唯一服装依据。严禁沿用、复制或恢复@视频1中的原片服装；严禁把@图片1中的"
    "纯白色基础底衫或基础长裤当成目标服装；人物只继承@图片1的身份、脸部、发型、肤色、体型和身体比例。最终人物"
    "从头到脚只能穿@图片2明确展示的新服装、鞋履和配饰，不得混穿原服装、白色底衫或模型自行设计的服装。"
)
DEFAULT_LONG_VIDEO_PROMPT = (
    "完全复刻当前分镜中所有出场人物各自的全部动作、表情、视线、节奏、站位、遮挡和互动关系；"
    "动作起始时间、落点、强拍定格和停顿与参考分镜帧级对齐。跨分镜保持同一人物身份、五官、发型、体型和服装完全一致；"
    "镜头机位、焦段、景别、裁切、透视、构图和运镜轨迹完全复刻原片。禁止串脸、互换服装、合并肢体、遗漏人物、"
    "增加人物、场景漂移或重影。@视频1是当前分镜的动作与镜头母版，最终输出正常彩色视频。"
    "有台词时由Seedance根据新人物形象生成全新的角色音色；不得模仿、复制或保留原片人物的声纹、音色和嗓音特征。"
    "@视频1是完全静音的动作、遮挡和运镜参考，不含原片对白、歌声、BGM、环境音或任何原声波形；"
    "新声音逐字严格跟随本提示中经提取和人工校订的发声起止、语速、停顿长度、重音、语调和情绪，"
    "不得自行放慢、拖长、截断句尾或改变节奏；绝不直接复用原音轨或原声波形。"
    "只生成新人物的台词、人声呼吸和必要口腔声；不得复刻或混入原片对白、BGM、环境音及其他原片声音。"
    "保持台词文字、语言和说话顺序一致；口型、下颌和嘴唇运动与新生成台词逐音节同步；"
    "同一新人物跨分镜保持同一新音色。无台词时不得新增人声。"
)
COMPACT_LONG_VIDEO_PROMPT = (
    "逐帧复刻全部出场人物的动作、表情、视线、节奏、站位、尺度、遮挡、互动和动作时点；"
    "机位、焦段、景别、裁切、透视、构图、对焦及运镜严格跟随@视频1。"
    "跨镜保持人物身份、五官、发型、体型和服装一致；禁止串脸、换位、漏人、加人、服装互换、场景漂移或重影。"
    "输出正常彩色视频。"
)
WHITE_MODEL_FINAL_MASTER_CONSTRAINT = (
    "最高优先级白模母版规则：@视频1是人物数量、屏幕占位、前后景、局部入画、虚实、遮挡、动作时序、机位、焦段、"
    "景别、裁切、透视、构图、对焦和运镜的唯一逐帧依据。逐一替换每个白模人体；即使只在前景露出模糊头肩、背影或"
    "身体局部，也必须按原位置、大小、虚实和遮挡保留，绝不能删除。人物图只提供身份、五官、发型和体型，服装图只提供"
    "服装；场景图只提供环境外观、材质、色彩和光照，三者均不得改变姿势、机位、景别、构图或裁切。禁止重新构图、"
    "自动居中、人物缩放、推近拉远或改变运镜；逐帧保持白模边缘位置、人物尺度和背景视差。"
)
COMPOSITION_RETRY_MASTER_CONSTRAINT = (
    "纠偏时@视频1仍是人物数量、屏幕占位、前后景、遮挡、动作、机位、景别、裁切和运镜的唯一逐帧依据；"
    "人物图、服装图和场景图均不得改变白模构图。"
)
NO_VISIBLE_TEXT_CONSTRAINT = (
    "无文字强制规则：最终画面任何时间、任何位置都不得出现可读文字或字符；彻底移除且不得复刻原片、人物参考图、"
    "服装图和场景图中的字幕、台词文字、标题、片头片尾、姓名条、时间码、数字、招牌文字、屏幕界面文字、Logo、"
    "品牌标识、平台角标和水印。台词只能作为声音与口型存在，绝不能以字幕或文字显示。"
)
REAL_PERSON_COMPACT_NO_VISIBLE_TEXT_CONSTRAINT = (
    "画面全程不得出现任何额外叠加的台词字幕、翻译字幕、标题、姓名条、说明文字、角标或水印；"
    "台词只能作为声音和口型存在。场景中自然存在并随透视运动的招牌、包装和服装标签可以保留。"
)
REAL_WHOLE_NO_VISIBLE_TEXT_CONSTRAINT = (
    "最高优先级零字幕输出：最终全片每一帧都必须是无字幕母版；不得额外叠加或生成台词字幕、翻译字幕、标题、"
    "姓名条、时间码、说明文字、屏幕文字层、平台Logo、角标或水印。人物说话只生成声音和口型，绝不能把台词"
    "可视化为文字。场景中自然存在并随透视和镜头运动的招牌、商品包装与服装标签可以保留，但绝不能把它们复制成"
    "固定在屏幕上的字幕层。"
)
REAL_WHOLE_WHITE_APPEARANCE_CONSTRAINT = (
    "白模外观隔离规则：@视频1只提供人体关节运动、空间占位、尺度、前后景、遮挡与镜头运动，不提供任何人物外观。"
    "忽略并彻底替换白模的白色材质、脸型、头型、身体表面、服装轮廓、配饰以及任何误生成的头发或发丝；"
    "白模中即使出现头发形状也只视为错误占位，最终发型只能来自对应火山角色@图片，绝不得沿用白模头发。"
)
DEFAULT_LONG_SCENE_PLATE_PROMPT = (
    "图1只提供新场景的建筑、陈设、材质、色彩、光照与世界观，绝不提供本镜头的机位或构图。"
    "图2、图3、图4是当前分镜独有的结构蓝图：灰色人体块只表示人物占位、尺度、前后景与遮挡，线条表示原镜头的"
    "透视、地平线、消失点、景别和裁切；结构蓝图不得被图1的原始视角覆盖。请从图1的同一空间重建与图2至图4"
    "完全匹配的当前机位，输出不含人物的干净场景板；同一场景在不同分镜必须随原镜头改变观察方向、焦段、景别和"
    "可见区域，不能照搬同一个正面视角。不得恢复原场景的内容、材质或身份信息。画面中不得出现人物、人体、服装、"
    "玩偶、文字、Logo或水印。输出正常彩色场景图，画布尺寸和纵横比严格跟随结构蓝图。"
)
REAL_SCENE_RENDER_QUALITY_CONSTRAINT = (
    "真实人物场景板质量硬约束：最终场景的完成度、媒介、材质、色彩和光照必须跟随图1；"
    "图1若为实拍或写实渲染，输出必须是完成的照片级彩色场景。图2至图4仅是几何蓝图，"
    "严禁把其中的灰色人体块、白底、轮廓线、线稿、素描、低模或未完成渲染质感带入场景板。"
)
SCENE_PLATE_SIGNATURE_VERSION = 2
REAL_SCENE_PLATE_SIGNATURE_VERSION = 4
REAL_LONG_SHOT_SIGNATURE_VERSION = 19
MAX_AUTOMATIC_COMPOSITION_RETRIES = 2
MAX_REAL_AUTOMATIC_COMPOSITION_RETRIES = 1
MAX_REAL_AUTOMATIC_WHITE_MODEL_RETRIES = 1
SEEDANCE_SAFE_PROMPT_LIMIT = 1980
REAL_RETRY_PROMPT_RESERVE = 180
REAL_WHOLE_SCENE_REFERENCE_CONSTRAINT = "@场景图 只做场景参考，不做必要背景。"
REAL_WHOLE_GENERATION_STRATEGY = "whole_white_model"
REAL_PER_SHOT_GENERATION_STRATEGY = "per_shot"
REAL_CHARACTER_LIBRARY_CACHE_PATH = PROJECT_DIR / "runs" / "_real_character_library_cache.json"
_RAPID_OCR_ENGINE: Any = None
_RAPID_OCR_UNAVAILABLE = False
_RAPID_OCR_LOCK = threading.Lock()
DEFAULT_WHITE_MODEL_PROMPT = (
    "参考@视频1，将当前分镜转换为通用三维人体白模动作母版。逐帧严格复制所有人物的数量、动作时序、身体姿态、"
    "手势、头部朝向、视线、嘴部开合、表情变化、站位、尺度、遮挡、互动和移动路径；严格复制镜头机位、焦段、景别、"
    "裁切、透视、构图、对焦与运镜轨迹。所有人物统一改造成无身份的中性人体白模：光滑光头，不保留头发、发型、"
    "胡须、鞋袜、帽子、眼镜、首饰、耳饰或任何原服装与穿戴装饰。全身使用连续、密封、不透明的纯白素体外壳完整包覆，"
    "没有皮肤纹理和解剖细节，不出现任何裸露感。面部只保留眼窝、眼睑、鼻梁、嘴部和下颌的简化立体结构，用于表达朝向、视线和表情，"
    "不保留原人物五官与身份。人物采用纯白无纹理哑光树脂材质，以清晰轮廓、体块转折、柔和明暗和接触阴影表现立体感；"
    "手指、关节与身体边界必须清楚。仅保留动作必需且正在交互的道具，并简化为纯白几何体；删除其他场景和装饰。背景完整"
    "替换为均匀、干净、无渐变、无噪点的标准绿幕 #00B140，白模边缘锐利且无绿色反光。禁止模糊、融化、重影、闪烁、"
    "多肢、少肢、粘连、人物增减或动作漂移。画面中严禁出现或保留任何字幕、台词文字、标题、时间码、数字、招牌文字、"
    "屏幕界面文字、Logo、品牌标识、平台角标或水印；台词只表现为嘴部动作，不得显示成文字。输出清晰稳定、高对比的白模绿幕视频。"
)

# 衣装智换会把最长 15 秒的单人素材整体提交给 Seedance。旧版共用提示词
# 使用了“素体 / 解剖 / 裸露”等写实人体措辞，正常穿着的输入也可能在输出审核
# 阶段被误判。该项目单独使用非写实工业动作人台，不改变其他已跑通项目。
WARDROBE_SAFE_WHITE_MODEL_PROMPT = (
    "参考@视频1，将整段视频转换为非写实的工业动画动作人台母版。逐帧严格复制画面中所有人物的数量、动作时序、姿态、"
    "手势、头部朝向、视线、嘴部运动、表情节奏、站位、尺度、遮挡、互动与移动路径；严格复制每次切镜、镜头机位、焦段、"
    "景别、裁切、透视、构图、对焦和运镜轨迹。所有人物统一替换成无身份的纯白动画绑定人台：使用光滑椭圆头部、简化面部块面、"
    "几何化躯干和清楚的关节结构；从头到脚均为不反光的硬质白色模型材质，不出现肤色、皮肤纹理、图案或身份特征。"
    "不得保留原人物的头发、发型、胡须、服装、鞋袜、帽子、眼镜、首饰或其他穿戴物；胸腹、腰胯与四肢保持简洁工业模型块面，"
    "不得增加写实人体细节。眼部、鼻部、嘴部与下颌只用简洁几何起伏表达朝向、视线和表情节奏，不复制原人物五官。"
    "手部、关节和人物轮廓必须稳定清楚。仅保留动作中正在使用的必要道具，并统一简化为纯白几何模型；删除其他场景与装饰。"
    "背景完整替换为均匀、干净、无渐变、无噪点的标准绿幕 #00B140，人物边缘清晰且没有绿色反光。禁止模糊、融化、重影、闪烁、"
    "多肢、少肢、粘连、人物增减或动作漂移。严禁出现字幕、台词文字、标题、时间码、数字、招牌文字、界面文字、Logo、品牌标识、"
    "平台角标或水印；台词只保留嘴部运动，不显示文字。输出清晰、稳定、高对比的纯白工业动作人台绿幕视频。"
)

WARDROBE_SWAP_PROJECT = "wardrobe_intelligent_swap"
WARDROBE_SWAP_MODES = {"person", "scene", "clothing", "custom"}
WARDROBE_MAX_SOURCE_SECONDS = 15.0
WARDROBE_MODE_LABELS = {
    "person": "只更换人物",
    "scene": "只更换场景",
    "clothing": "只更换服装",
    "custom": "随心换",
}


def build_wardrobe_swap_prompt(mode: str) -> str:
    """Build the fixed three-reference contract used by 衣装智换.

    In every mode @视频1 is the generated white-model motion master.
    This keeps motion/camera control separate from identity, wardrobe and scene.
    """
    shared = (
        "@视频1是唯一动作与镜头母版。逐帧严格遵守它的切镜时刻、镜头机位、俯仰角、焦段、景别、裁切、透视、"
        "构图、运镜轨迹、人物数量、人物大小、左右站位、前后层级、遮挡关系、动作时序、姿态、手势、视线、"
        "表情和移动路径；不得重新构图、改变景别、漏人、增加人物或交换人物位置。白模只负责时空与表演控制，"
        "不得把白模的绿幕、白色材质、光头或无身份五官带入成片。最终输出自然、清晰、稳定的正常彩色视频。"
        "禁止生成字幕、台词文字、标题、时间码、说明文字、平台角标、Logo或水印；场景中原本真实存在的招牌与"
        "环境文字可以自然保留，但不得新增任何叠加文字。"
    )
    if mode == "person":
        return (
            "参考@视频1、@图片1、@图片2和@图片3完成只更换人物。@图片1是唯一新人物身份依据；"
            "@图片2是原片服装依据；@图片3是原片背景与空间依据。只替换人物身份和面部形象，严格保留原服装、"
            "原场景、原光照关系和原表演，不得改变服装款式或重新设计背景。" + shared
        )
    if mode == "scene":
        return (
            "参考@视频1、@图片1、@图片2和@图片3完成只更换场景。@图片1是原片人物身份依据；"
            "@图片2是原片服装依据；@图片3是唯一新场景依据。完整保留人物身份、发型、体型与服装，只替换环境；"
            "新场景需随@视频1的机位、透视、景别和运镜自然变化，不得把一张背景静态贴在人物背后。" + shared
        )
    if mode == "clothing":
        return (
            "参考@视频1、@图片1、@图片2和@图片3完成只更换服装。@图片1是原片人物身份依据；"
            "@图片2是唯一新服装依据；@图片3是原片背景与空间依据。完整保留人物身份、脸部、发型、体型和原场景，"
            "只替换服装、鞋履及@图片2明确展示的配饰；不得残留或混穿原服装。" + shared
        )
    if mode == "custom":
        return (
            "参考@视频1、@图片1、@图片2和@图片3完成随心换。@图片1是用户最终选定的唯一人物身份、"
            "面部、发型和体型依据；@图片2是用户最终选定的唯一服装、鞋履和明确配饰依据；@图片3是用户最终"
            "选定的唯一场景、空间结构、材质和环境光照依据。三张参考图均可能来自原片自动提取，也可能来自"
            "用户上传的替换图，必须分别独立服从，不得沿用未被选中的旧人物、旧服装或旧场景。" + shared
        )
    raise WorkflowError("衣装智换模式无效。")

FINAL_STYLE_PRESETS: dict[str, dict[str, str]] = {
    "match_character": {
        "label": "跟随新人物参考图",
        "description": "默认推荐。自动识别新人物是实拍、3DCG或二维画风，并统一服装与场景的视觉语言。",
        "prompt": (
            "成片整体视觉媒介与渲染语言严格跟随新人物形象参考图：真人参考输出真人影视质感，"
            "3DCG参考输出同类3DCG，二维参考输出同类二维动画；人物、服装与场景必须风格统一，禁止混合媒介。"
        ),
    },
    "ultra_realistic": {
        "label": "超写实真人",
        "description": "真实皮肤、毛发、布料、光影与镜头成像，适合影视和真人广告。",
        "prompt": "成片统一为超写实真人影视风格，真实皮肤与毛发、自然布料、物理正确光影和电影镜头成像，禁止卡通、插画或塑料CG质感。",
    },
    "cinematic": {
        "label": "电影写实",
        "description": "保留真人真实感，并加强电影布光、色彩层次和镜头氛围。",
        "prompt": "成片统一为电影级写实风格，真实人物与材质，克制电影布光、自然景深、丰富色彩层次和高动态范围，禁止过度磨皮与廉价滤镜。",
    },
    "3d_cg": {
        "label": "高品质 3DCG",
        "description": "电影级三维角色与环境，强调精细建模、材质、灯光和稳定渲染。",
        "prompt": "成片统一为高品质电影级3DCG风格，精细三维建模、PBR材质、全局光照、稳定角色渲染与自然景深，禁止真人实拍和二维笔触混入。",
    },
    "2d_animation": {
        "label": "2D 动画",
        "description": "清晰线稿、二维上色与稳定动画造型，适合插画和动画人物。",
        "prompt": "成片统一为高质量2D动画风格，清晰稳定线稿、二维分层上色、统一角色造型与动画光影，禁止真人皮肤纹理和三维塑料渲染。",
    },
}

REAL_PERSON_COMPACT_STYLE_PROMPTS = {
    "match_character": "超写实真人影视风；素描图只传递五官身份，绝不传递素描或CG画风。",
    "ultra_realistic": "超写实真人影视风，真实皮肤、毛发、布料与物理光影，禁止卡通或CG质感。",
    "cinematic": "电影级真人写实风，自然景深、电影布光与高动态范围。",
    "3d_cg": "电影级3DCG风，精细建模、PBR材质、全局光照与稳定渲染。",
    "2d_animation": "高质量2D动画风，稳定线稿、二维上色与统一动画光影。",
}


def final_style_preset(style_id: str) -> dict[str, str]:
    key = str(style_id or "match_character").strip() or "match_character"
    preset = FINAL_STYLE_PRESETS.get(key)
    if preset is None:
        raise WorkflowError("成片风格选项无效，请重新选择。")
    return {"id": key, **preset}


def apply_final_style_prompt(prompt: str, style_id: str) -> tuple[str, dict[str, str]]:
    preset = final_style_preset(style_id)
    base = prompt.strip() or DEFAULT_LONG_VIDEO_PROMPT
    return f"{base}\n{NO_VISIBLE_TEXT_CONSTRAINT}\n成片风格约束：{preset['prompt']}", preset


def compact_long_generation_constraints(prompt: str, *, real_person_mode: bool = False) -> str:
    """Remove duplicated global rules before adding the shot-specific timeline."""
    value = str(prompt or "").strip()
    if not value:
        return COMPACT_LONG_VIDEO_PROMPT
    if DEFAULT_LONG_VIDEO_PROMPT in value:
        value = value.replace(DEFAULT_LONG_VIDEO_PROMPT, COMPACT_LONG_VIDEO_PROMPT, 1)
    else:
        value = re.sub(
            r"有台词时由Seedance根据新人物形象生成全新的角色音色；.*?无台词时不得新增人声。",
            "",
            value,
            count=1,
            flags=re.DOTALL,
        )
    if real_person_mode:
        value = value.replace(
            NO_VISIBLE_TEXT_CONSTRAINT,
            REAL_PERSON_COMPACT_NO_VISIBLE_TEXT_CONSTRAINT,
        )
        for style_id, preset in FINAL_STYLE_PRESETS.items():
            verbose = f"成片风格约束：{preset['prompt']}"
            compact = f"成片风格：{REAL_PERSON_COMPACT_STYLE_PROMPTS[style_id]}"
            value = value.replace(verbose, compact)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def long_shot_raw_output_path(shot: dict[str, Any]) -> Path | None:
    """Return the Seedance result before duration conforming.

    New jobs persist ``raw_output_path`` explicitly. Older long-video manifests
    predate that field, so recover their original Seedance file from the saved
    task id instead of silently falling back to the conformed download.
    """
    raw_value = str(shot.get("raw_output_path") or "").strip()
    if raw_value:
        raw_path = Path(raw_value)
        if raw_path.is_file():
            return raw_path

    task_id = str(shot.get("task_id") or "").strip()
    source_value = str(shot.get("source_path") or "").strip()
    if not task_id or not source_value:
        return None
    shot_dir = Path(source_value).parent
    for prefix in ("多人复刻成片", "生成成片"):
        candidate = shot_dir / f"{prefix}_{task_id}.mp4"
        if candidate.is_file():
            return candidate
    return None


ERROR_CATEGORY_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "balance",
        "余额/额度",
        (
            "accountoverdue", "overdue balance", "insufficient balance", "quota", "sharedpoolexceeded",
            "余额", "欠费", "额度", "配额", "http 429", "status code 429",
        ),
    ),
    (
        "review",
        "审核",
        (
            "sensitive", "privacyinformation", "content policy", "moderation", "review rejected",
            "敏感", "审核", "违规", "真人/隐私", "隐私安全",
        ),
    ),
    (
        "parameter",
        "参数",
        (
            "invalidparameter", "invalid parameter", "parameters `", "must be between", "must be `",
            "json格式", "json 格式", "格式校验", "提示词过长", "字符上限", "参数无效", "比例", "分辨率",
            "duration", "ratio", "aspect ratio", "fps",
        ),
    ),
    (
        "asset",
        "素材",
        (
            "asset://", "asset id", "input image", "input video", "material", "reference image",
            "素材", "参考图", "参考视频", "尚未上传", "尚未选择", "请选择", "找不到文件", "文件不存在",
            "缺少", "未绑定", "角色库",
        ),
    ),
    (
        "network",
        "网络",
        (
            "proxyerror", "connectionreset", "connection aborted", "connection refused", "remote end closed",
            "max retries exceeded", "read timed out", "connect timeout", "network", "temporary video upload",
            "网络", "连接中断", "连接失败", "超时", "临时通道", "远程主机",
        ),
    ),
)


def classify_error_message(message: Any) -> dict[str, str]:
    """Map raw local/Ark failures into the six user-facing error families."""
    text = str(message or "").strip().lower()
    for category_id, label, needles in ERROR_CATEGORY_RULES:
        if any(needle in text for needle in needles):
            return {"id": category_id, "label": label}
    return {"id": "system", "label": "系统"}


@dataclass
class WebJob:
    id: str
    kind: str
    run_dir: Path
    status: str = "queued"
    stage: str = "等待开始"
    progress: int = 0
    logs: list[str] = field(default_factory=list)
    task_id: str = ""
    depth_path: Path | None = None
    output_path: Path | None = None
    scene_path: Path | None = None
    person_path: Path | None = None
    clothing_path: Path | None = None
    mosaic_path: Path | None = None
    white_model_path: Path | None = None
    white_reference_path: Path | None = None
    performance_path: Path | None = None
    actors: list[dict[str, Any]] = field(default_factory=list)
    cast_continuity: dict[str, Any] = field(default_factory=dict)
    scene_groups: list[dict[str, Any]] = field(default_factory=list)
    shots: list[dict[str, Any]] = field(default_factory=list)
    project: str = ""
    workspace_id: str = ""
    workspace_name: str = ""
    error: str = ""
    recovery_action: str = ""
    cloud_status: str = ""
    cloud_started_at: float = 0.0
    cloud_updated_at: float = 0.0
    source_duration: float = 0.0
    depth_duration: float = 0.0
    generation_duration: int = 0
    generation_strategy: str = REAL_PER_SHOT_GENERATION_STRATEGY
    pause_requested: bool = False
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, **values: Any) -> None:
        with self.lock:
            for key, value in values.items():
                setattr(self, key, value)

    def log(self, message: str) -> None:
        text = message.strip()
        if not text:
            return
        if (text.startswith("失败") or "请求失败" in text[:40]) and not text.startswith("["):
            category = classify_error_message(text)
            text = f"[{category['label']}] {text}"
        with self.lock:
            stamp = datetime.now().strftime("%H:%M:%S")
            self.logs.append(f"[{stamp}] {text}")
            if len(self.logs) > 300:
                self.logs = self.logs[-300:]

    def public(self) -> dict[str, Any]:
        with self.lock:
            progress = self.progress
            estimated = False
            elapsed = 0
            if self.cloud_started_at > 0:
                elapsed = max(0, int(time.time() - self.cloud_started_at))
            if self.cloud_status == "queued":
                progress = max(progress, min(69, 64 + elapsed // 45))
                estimated = True
            elif self.cloud_status == "running":
                # Ark exposes task states, but not a numeric completion percentage.
                # Keep the UI moving within the cloud-generation portion without
                # ever reaching the download stage before the API says succeeded.
                curve = 1.0 - math.exp(-elapsed / 240.0)
                progress = max(progress, min(89, 68 + int(21 * curve)))
                estimated = True
            result = {
                "id": self.id,
                "kind": self.kind,
                "project": self.project,
                "workspace_id": self.workspace_id,
                "workspace_name": self.workspace_name,
                "status": self.status,
                "stage": self.stage,
                "progress": progress,
                "progress_estimated": estimated,
                "logs": list(self.logs),
                "task_id": self.task_id,
                "cloud_status": self.cloud_status,
                "cloud_elapsed_seconds": elapsed,
                "error": self.error,
                "error_category": classify_error_message(self.error) if self.error else None,
                "created_at": self.created_at,
                "source_duration": round(self.source_duration, 3) if self.source_duration else 0,
                "depth_duration": round(self.depth_duration, 3) if self.depth_duration else 0,
                "generation_duration": self.generation_duration,
                "generation_strategy": self.generation_strategy,
                "pause_requested": self.pause_requested,
                "has_depth": bool(self.depth_path and self.depth_path.is_file()),
                "has_output": bool(self.output_path and self.output_path.is_file()),
                "has_scene": bool(self.scene_path and self.scene_path.is_file()),
                "has_person_reference": bool(self.person_path and self.person_path.is_file()),
                "person_reference_current": (
                    wardrobe_person_triview_is_current(self)
                    if self.project == WARDROBE_SWAP_PROJECT
                    and self.kind.startswith("wardrobe_prepare_")
                    and self.kind.rsplit("_", 1)[-1] in {"scene", "clothing", "custom"}
                    else bool(self.person_path and self.person_path.is_file())
                ),
                "has_clothing_reference": bool(self.clothing_path and self.clothing_path.is_file()),
                "has_mosaic": bool(self.mosaic_path and self.mosaic_path.is_file()),
                "has_white_model": bool(self.white_model_path and self.white_model_path.is_file()),
                "has_white_reference": bool(self.white_reference_path and self.white_reference_path.is_file()),
                "has_performance": bool(self.performance_path and self.performance_path.is_file()),
                "has_audio_stems": bool(
                    self.run_dir
                    and (self.run_dir / "audio_stems" / "audio_stems.json").is_file()
                ),
                "recovery_action": self.recovery_action,
                "depth_url": f"/api/jobs/{self.id}/file/depth" if self.depth_path else "",
                "output_url": f"/api/jobs/{self.id}/file/output" if self.output_path else "",
                "scene_url": f"/api/jobs/{self.id}/file/scene" if self.scene_path else "",
                "person_url": f"/api/jobs/{self.id}/file/person" if self.person_path else "",
                "clothing_url": f"/api/jobs/{self.id}/file/clothing" if self.clothing_path else "",
                "scene_revision": (
                    self.scene_path.stat().st_mtime_ns
                    if self.scene_path and self.scene_path.is_file()
                    else 0
                ),
                "person_revision": (
                    self.person_path.stat().st_mtime_ns
                    if self.person_path and self.person_path.is_file()
                    else 0
                ),
                "clothing_revision": (
                    self.clothing_path.stat().st_mtime_ns
                    if self.clothing_path and self.clothing_path.is_file()
                    else 0
                ),
                "mosaic_url": f"/api/jobs/{self.id}/file/mosaic" if self.mosaic_path else "",
                "white_model_url": f"/api/jobs/{self.id}/file/white_model" if self.white_model_path else "",
                "performance_url": f"/api/jobs/{self.id}/file/performance" if self.performance_path else "",
            }
            result["actors"] = [
                {
                    "id": int(actor.get("id") or index),
                    "role": str(actor.get("role") or f"人物 {index}"),
                    "has_person": bool(
                        str(actor.get("trusted_asset_uri") or actor.get("original_person_source") or actor.get("person_source") or "").startswith("asset://")
                        or (
                            (actor.get("local_person_source") or actor.get("original_person_source") or actor.get("person_source"))
                            and Path(str(actor.get("local_person_source") or actor.get("original_person_source") or actor.get("person_source"))).is_file()
                        )
                    ),
                    "has_clothing": bool(
                        actor.get("clothing_source")
                        and Path(str(actor["clothing_source"])).is_file()
                    ),
                    "uses_authorized_asset": str(
                        actor.get("trusted_asset_uri") or actor.get("original_person_source") or actor.get("person_source") or ""
                    ).startswith("asset://"),
                    "asset_uri": (
                        str(actor.get("trusted_asset_uri") or actor.get("original_person_source") or actor.get("person_source") or "")
                        if str(actor.get("trusted_asset_uri") or actor.get("original_person_source") or actor.get("person_source") or "").startswith("asset://")
                        else ""
                    ),
                    "ark_library_upload_status": str(
                        actor.get("ark_library_upload_status") or ""
                    ),
                    "ark_library_asset_name": str(
                        actor.get("ark_library_asset_name") or ""
                    ),
                    "ark_library_group_id": str(
                        actor.get("ark_library_group_id") or ""
                    ),
                    "ark_library_asset_uri": str(
                        actor.get("ark_library_asset_uri") or ""
                    ),
                    "ark_library_upload_error": str(
                        actor.get("ark_library_upload_error") or ""
                    ),
                    "has_masked_person": bool(
                        actor.get("masked_person_source")
                        and Path(str(actor["masked_person_source"])).is_file()
                    ),
                    "has_sketch_person": bool(
                        actor.get("sketch_person_source")
                        and Path(str(actor["sketch_person_source"])).is_file()
                    ),
                    "masked_person_url": (
                        f"/api/jobs/{self.id}/actors/{int(actor.get('id') or index)}/file/masked"
                        if actor.get("masked_person_source") and Path(str(actor["masked_person_source"])).is_file()
                        else ""
                    ),
                    "sketch_person_url": (
                        f"/api/jobs/{self.id}/actors/{int(actor.get('id') or index)}/file/sketch"
                        if actor.get("sketch_person_source") and Path(str(actor["sketch_person_source"])).is_file()
                        else ""
                    ),
                    "person_preview_url": (
                        f"/api/jobs/{self.id}/actors/{int(actor.get('id') or index)}/file/person"
                        if real_long_actor_local_reference_path(
                            self, actor, int(actor.get("id") or index), "person"
                        )
                        else ""
                    ),
                    "clothing_preview_url": (
                        f"/api/jobs/{self.id}/actors/{int(actor.get('id') or index)}/file/clothing"
                        if real_long_actor_local_reference_path(
                            self, actor, int(actor.get("id") or index), "clothing"
                        )
                        else ""
                    ),
                }
                for index, actor in enumerate(self.actors, start=1)
            ]
            result["cast_continuity"] = dict(self.cast_continuity)
            result["scene_groups"] = [
                {
                    "id": str(group.get("id") or ""),
                    "name": str(group.get("name") or "未命名场景"),
                    "description": str(group.get("description") or ""),
                    "images": [
                        {
                            "index": image_index,
                            "name": Path(str(path_value)).name,
                            "url": (
                                f"/api/jobs/{self.id}/scene-groups/{group.get('id')}/{image_index}"
                                if Path(str(path_value)).is_file()
                                else ""
                            ),
                        }
                        for image_index, path_value in enumerate(group.get("images") or [], start=1)
                        if str(path_value)
                    ],
                }
                for group in self.scene_groups
                if str(group.get("id") or "")
            ]
            shot_items: list[dict[str, Any]] = []
            for raw_shot in self.shots:
                shot = dict(raw_shot)
                index = int(shot.get("index") or len(shot_items) + 1)
                public_shot = {
                    key: value
                    for key, value in shot.items()
                    if key not in {
                        "source_path",
                        "depth_path",
                        "scene_path",
                        "output_path",
                        "raw_output_path",
                        "people_map_path",
                        "target_scene_path",
                        "mosaic_path",
                        "white_model_path",
                        "performance_path",
                        "dialogue_timing_path",
                    }
                }
                if public_shot.get("error") and not public_shot.get("error_category"):
                    public_shot["error_category"] = classify_error_message(public_shot["error"])
                for artifact in (
                    "source", "depth", "scene", "output", "raw_output", "people_map", "target_scene",
                    "mosaic", "white_model", "performance",
                ):
                    if artifact == "raw_output":
                        path = long_shot_raw_output_path(shot)
                    else:
                        path_value = shot.get(f"{artifact}_path")
                        path = Path(path_value) if path_value else None
                    available = bool(path and path.is_file())
                    public_shot[f"has_{artifact}"] = available
                    public_shot[f"{artifact}_url"] = (
                        f"/api/jobs/{self.id}/shots/{index}/file/{artifact}" if available else ""
                    )
                sub_job_id = str(shot.get("job_id") or "")
                if sub_job_id and sub_job_id != self.id:
                    sub_job = JOBS.get(sub_job_id)
                    if sub_job is not None:
                        sub_state = sub_job.public()
                        public_shot.update(
                            status=sub_state["status"],
                            stage=sub_state["stage"],
                            progress=sub_state["progress"],
                            task_id=sub_state["task_id"],
                            cloud_status=sub_state["cloud_status"],
                            error=sub_state["error"],
                            error_category=sub_state.get("error_category"),
                        )
                shot_items.append(public_shot)
            result["shots"] = shot_items
            result["shot_count"] = len(shot_items)
            return result


app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="/static")


@app.before_request
def optional_site_auth():
    """Protect formal deployments while preserving the zero-config local workflow."""
    if request.path == "/healthz":
        return None
    expected_user = os.environ.get("DEPTHFLOW_SITE_USER", "").strip()
    expected_password = os.environ.get("DEPTHFLOW_SITE_PASSWORD", "")
    if not expected_user and not expected_password:
        return None
    if not expected_user or not expected_password:
        return jsonify({"error": "网站登录配置不完整：必须同时设置 DEPTHFLOW_SITE_USER 与 DEPTHFLOW_SITE_PASSWORD。"}), 503
    auth = request.authorization
    valid = bool(
        auth
        and hmac.compare_digest(auth.username or "", expected_user)
        and hmac.compare_digest(auth.password or "", expected_password)
    )
    if valid:
        return None
    return Response(
        "需要登录后使用 DepthFlow。",
        401,
        {"WWW-Authenticate": 'Basic realm="DepthFlow", charset="UTF-8"'},
    )


@app.after_request
def disable_local_ui_cache(response):
    """Keep UI fresh and attach a stable error category to every failed API JSON."""
    if request.path.startswith("/api/") and response.status_code >= 400 and response.is_json:
        payload = response.get_json(silent=True)
        if isinstance(payload, dict) and not payload.get("error_category"):
            message = payload.get("error") or payload.get("message")
            if message:
                payload["error_category"] = classify_error_message(message)
                response.set_data(json.dumps(payload, ensure_ascii=False))
                response.headers["Content-Type"] = "application/json; charset=utf-8"
    if request.path == "/" or request.path.startswith("/projects/") or request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response
app.config["MAX_CONTENT_LENGTH"] = 450 * 1024 * 1024
JOBS: dict[str, WebJob] = {}
JOBS_LOCK = threading.Lock()
LONG_WORKSPACE_LOCK = threading.Lock()
LONG_WORKSPACE_INDEX_PATH = PROJECT_DIR / "runs" / "_long_video_workspaces.json"
RESTORED_DEPTH_JOB_ID = "latest-depth"


def match_seedance_duration(seconds: float) -> int:
    """Choose the nearest Seedance-supported whole-second duration."""
    try:
        value = float(seconds)
    except (TypeError, ValueError) as exc:
        raise WorkflowError("无法读取参考视频时长。") from exc
    if not math.isfinite(value) or value <= 0:
        raise WorkflowError("参考视频时长无效。")
    return max(4, min(15, int(math.floor(value + 0.5))))


def match_seedance_cover_duration(seconds: float) -> int:
    """Choose a supported duration that never cuts off the original shot."""
    try:
        value = float(seconds)
    except (TypeError, ValueError) as exc:
        raise WorkflowError("无法读取参考视频时长。") from exc
    if not math.isfinite(value) or value <= 0:
        raise WorkflowError("参考视频时长无效。")
    return max(4, min(15, int(math.ceil(value - 1e-6))))


def seedance_hold_timing_prompt(
    source_duration: float,
    generation_duration: float,
    *,
    compact: bool = False,
) -> str:
    """Tell Seedance that the extended tail is a hold, not extra story time."""
    source_seconds = max(0.01, float(source_duration))
    generation_seconds = max(source_seconds, float(generation_duration))
    if compact:
        return (
            f"时间轴锁定：0.00–{source_seconds:.2f}秒按@视频1完成全部动作、运镜、表情、口型和台词；"
            f"{source_seconds:.2f}–{generation_seconds:.2f}秒仅定格结束姿势，禁止慢放、延后或新增动作与台词。"
        )
    return (
        f"强制时间轴：@视频1中0.00–{source_seconds:.2f}秒是原分镜真实动作与台词区间，"
        f"{source_seconds:.2f}–{generation_seconds:.2f}秒只是为满足生成时长而追加的结束姿势定格。"
        f"所有动作、运镜、表情、口型和台词必须按参考在前{source_seconds:.2f}秒内完成；"
        "定格区间只保持结束画面，禁止把动作慢放、延后或平均摊到整段，禁止新增动作和台词。"
    )


def resolved_seedance_ratio(requested_ratio: str, video_path: Path | None) -> str:
    """Resolve adaptive ratio to the closest ratio Ark reports in task metadata."""
    requested = requested_ratio.strip() or "adaptive"
    if requested != "adaptive" or video_path is None:
        return "" if requested == "adaptive" else requested
    try:
        info = inspect_video(video_path)
        source_ratio = info.width / info.height
    except Exception:
        # Ratio matching is a recovery aid and must never make a valid submission fail.
        return ""
    supported = {
        "16:9": 16 / 9,
        "4:3": 4 / 3,
        "1:1": 1.0,
        "3:4": 3 / 4,
        "9:16": 9 / 16,
        "21:9": 21 / 9,
    }
    return min(supported, key=lambda name: abs(math.log(source_ratio / supported[name])))


class RealLongGenerationPaused(Exception):
    """Internal cooperative stop used only by the real-person long-video project."""


def wait_for_seedance_task_with_real_pause(
    job: WebJob,
    client: Any,
    task_id: str,
    *,
    on_status: Any = None,
    poll_interval: int = 15,
    timeout_seconds: int = 7200,
) -> dict[str, Any]:
    if not is_real_person_long_job(job):
        return client.wait_for_task(
            task_id,
            poll_interval=poll_interval,
            timeout_seconds=timeout_seconds,
            on_status=on_status,
        )
    started = time.monotonic()
    last_status = ""
    while True:
        if job.pause_requested:
            raise RealLongGenerationPaused()
        task = client.get_task(task_id)
        status = str(task.get("status") or "unknown")
        if on_status and status != last_status:
            on_status(task)
        last_status = status
        if status == "succeeded":
            return task
        if status in {"failed", "expired", "cancelled"}:
            error = task.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise WorkflowError(f"Seedance 任务状态为 {status}：{message or '未提供原因'}")
        if time.monotonic() - started > timeout_seconds:
            raise WorkflowError(
                f"等待已超过 {timeout_seconds // 60} 分钟。任务仍在云端运行，可稍后用任务 ID 查询。"
            )
        for _ in range(max(5, int(poll_interval))):
            if job.pause_requested:
                raise RealLongGenerationPaused()
            time.sleep(1.0)


def pause_real_long_generation_at_boundary(
    job: WebJob,
    *,
    shot_index: int = 0,
    task_id: str = "",
) -> bool:
    if not is_real_person_long_job(job) or not job.pause_requested:
        return False
    if shot_index:
        update_long_shot(
            job,
            shot_index,
            status="paused",
            stage="生成已暂停，等待手动继续",
            task_id=task_id or next(
                (
                    str(shot.get("task_id") or "")
                    for shot in job.shots
                    if int(shot.get("index") or 0) == shot_index
                ),
                "",
            ),
            error="",
        )
    job.update(
        status="paused",
        stage="真实人物生成已暂停，不会提交后续分镜",
        error="",
    )
    job.log(
        "暂停已生效：当前已提交的云端任务可能继续完成，但本地已停止跟进、自动纠偏和后续分镜提交；"
        "任务ID与已有结果均已保留。"
    )
    _persist_long_job(job)
    return True


def apply_automatic_duration(job: WebJob, options: dict[str, Any], video_path: Path) -> int:
    info = inspect_video(video_path)
    duration = match_seedance_duration(info.duration)
    options["duration"] = duration
    job.update(source_duration=info.duration, depth_duration=info.duration, generation_duration=duration)
    job.log(f"已按参考视频 {info.duration:.2f} 秒自动匹配 Seedance 输出时长：{duration} 秒。")
    return duration


def new_job(kind: str) -> WebJob:
    job_id = uuid.uuid4().hex[:12]
    run_dir = timestamped_run_dir(f"web_{kind}_{job_id}")
    job = WebJob(id=job_id, kind=kind, project=project_for_kind(kind), run_dir=run_dir)
    with JOBS_LOCK:
        JOBS[job_id] = job
    return job


def project_for_kind(kind: str) -> str:
    if kind.startswith("wardrobe"):
        return WARDROBE_SWAP_PROJECT
    if kind.startswith("multi"):
        return "multi_person_replication"
    if kind.startswith("person"):
        return "person_only_replacement"
    if kind.startswith("scene"):
        return "scene_only_replacement"
    if kind.startswith("clothing"):
        return "clothing_only_replacement"
    if kind.startswith("real_long"):
        return REAL_PERSON_LONG_PROJECT
    if kind.startswith("long"):
        return VIRTUAL_LONG_PROJECT
    return "single_person_replication"


def is_long_video_project(value: str) -> bool:
    return value in {VIRTUAL_LONG_PROJECT, REAL_PERSON_LONG_PROJECT}


def _long_workspace_label(project: str) -> str:
    if project == REAL_PERSON_LONG_PROJECT:
        return "真实人物复刻"
    if project == VIRTUAL_LONG_PROJECT:
        return "虚拟人物复刻"
    raise WorkflowError("长视频项目类型无效。")


def _read_long_workspace_index_unlocked() -> dict[str, Any]:
    try:
        payload = json.loads(LONG_WORKSPACE_INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    projects = payload.get("projects")
    if not isinstance(projects, dict):
        projects = {}
    normalized: dict[str, list[dict[str, Any]]] = {}
    for project in (VIRTUAL_LONG_PROJECT, REAL_PERSON_LONG_PROJECT):
        records: list[dict[str, Any]] = []
        for raw in projects.get(project) or []:
            if not isinstance(raw, dict):
                continue
            workspace_id = str(raw.get("id") or "")
            if not re.fullmatch(r"[a-f0-9]{12}", workspace_id):
                continue
            records.append(
                {
                    "id": workspace_id,
                    "name": " ".join(str(raw.get("name") or "未命名项目").split())[:60] or "未命名项目",
                    "job_id": str(raw.get("job_id") or "")[:80],
                    "manifest_path": str(raw.get("manifest_path") or ""),
                    "created_at": str(raw.get("created_at") or ""),
                    "updated_at": str(raw.get("updated_at") or ""),
                }
            )
        normalized[project] = records
    return {"version": 1, "projects": normalized}


def _write_long_workspace_index_unlocked(payload: dict[str, Any]) -> None:
    LONG_WORKSPACE_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    save_shot_manifest(LONG_WORKSPACE_INDEX_PATH, payload)


def _new_long_workspace_record(name: str, *, job: WebJob | None = None) -> dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "id": uuid.uuid4().hex[:12],
        "name": " ".join(str(name or "").split())[:60] or "未命名项目",
        "job_id": job.id if job else "",
        "manifest_path": str(
            job.run_dir / ("real_long_manifest.json" if is_real_person_long_job(job) else "long_manifest.json")
        ) if job else "",
        "created_at": now,
        "updated_at": now,
    }


def ensure_long_workspace(project: str) -> list[dict[str, Any]]:
    """Return named workspaces, importing the latest legacy task on first use."""
    if not is_long_video_project(project):
        raise WorkflowError("长视频项目类型无效。")
    with LONG_WORKSPACE_LOCK:
        payload = _read_long_workspace_index_unlocked()
        existing = list(payload["projects"].get(project) or [])
    if existing:
        return existing

    latest = restore_latest_long_video_job(project)
    imported_name = (
        f"现有{_long_workspace_label(project)}项目"
        if latest
        else "重绘项目 1"
    )
    with LONG_WORKSPACE_LOCK:
        payload = _read_long_workspace_index_unlocked()
        existing = list(payload["projects"].get(project) or [])
        if not existing:
            record = _new_long_workspace_record(imported_name, job=latest)
            payload["projects"][project] = [record]
            _write_long_workspace_index_unlocked(payload)
            existing = [record]
    if latest:
        latest.workspace_id = existing[0]["id"]
        latest.workspace_name = existing[0]["name"]
    return existing


def create_long_workspace(project: str, name: str) -> dict[str, Any]:
    if not is_long_video_project(project):
        raise WorkflowError("长视频项目类型无效。")
    clean_name = " ".join(str(name or "").split())[:60]
    if not clean_name:
        raise WorkflowError("请输入项目名称，例如：重绘项目 1。")
    with LONG_WORKSPACE_LOCK:
        payload = _read_long_workspace_index_unlocked()
        records = payload["projects"].setdefault(project, [])
        if any(str(item.get("name") or "").casefold() == clean_name.casefold() for item in records):
            raise WorkflowError("已经存在同名项目，请换一个名称。")
        record = _new_long_workspace_record(clean_name)
        records.insert(0, record)
        _write_long_workspace_index_unlocked(payload)
    return record


def get_long_workspace_record(project: str, workspace_id: str) -> dict[str, Any] | None:
    if not workspace_id:
        return None
    with LONG_WORKSPACE_LOCK:
        payload = _read_long_workspace_index_unlocked()
        record = next(
            (item for item in payload["projects"].get(project) or [] if item.get("id") == workspace_id),
            None,
        )
        return dict(record) if record else None


def update_long_workspace(project: str, workspace_id: str, *, name: str) -> dict[str, Any]:
    clean_name = " ".join(str(name or "").split())[:60]
    if not clean_name:
        raise WorkflowError("项目名称不能为空。")
    with LONG_WORKSPACE_LOCK:
        payload = _read_long_workspace_index_unlocked()
        records = payload["projects"].get(project) or []
        if any(
            item.get("id") != workspace_id
            and str(item.get("name") or "").casefold() == clean_name.casefold()
            for item in records
        ):
            raise WorkflowError("已经存在同名项目，请换一个名称。")
        record = next((item for item in records if item.get("id") == workspace_id), None)
        if record is None:
            raise WorkflowError("找不到该重绘项目。")
        record["name"] = clean_name
        record["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _write_long_workspace_index_unlocked(payload)
    job_id = str(record.get("job_id") or "")
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job:
        job.workspace_name = clean_name
    return record


def delete_long_workspace(project: str, workspace_id: str) -> list[dict[str, Any]]:
    with LONG_WORKSPACE_LOCK:
        payload = _read_long_workspace_index_unlocked()
        records = payload["projects"].get(project) or []
        record = next((item for item in records if item.get("id") == workspace_id), None)
        if record is None:
            raise WorkflowError("找不到该重绘项目。")
        job_id = str(record.get("job_id") or "")
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job and job.status in {"queued", "running"}:
            raise WorkflowError("该项目仍在运行，不能删除；可以先切换到其他项目继续操作。")
        records = [item for item in records if item.get("id") != workspace_id]
        if not records:
            records = [_new_long_workspace_record("重绘项目 1")]
        payload["projects"][project] = records
        _write_long_workspace_index_unlocked(payload)
        return records


def bind_long_workspace(job: WebJob, workspace_id: str) -> None:
    """Bind a newly analyzed video to exactly one named workspace."""
    if not workspace_id:
        return
    if not re.fullmatch(r"[a-f0-9]{12}", workspace_id):
        raise WorkflowError("重绘项目编号无效，请刷新页面后重试。")
    with LONG_WORKSPACE_LOCK:
        payload = _read_long_workspace_index_unlocked()
        records = payload["projects"].get(job.project) or []
        record = next((item for item in records if item.get("id") == workspace_id), None)
        if record is None:
            raise WorkflowError("当前重绘项目不存在，请刷新项目列表后重试。")
        record["job_id"] = job.id
        record["manifest_path"] = str(
            job.run_dir / ("real_long_manifest.json" if is_real_person_long_job(job) else "long_manifest.json")
        )
        record["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _write_long_workspace_index_unlocked(payload)
    job.workspace_id = workspace_id
    job.workspace_name = str(record.get("name") or "")


def touch_long_workspace(job: WebJob) -> None:
    if not job.workspace_id:
        return
    with LONG_WORKSPACE_LOCK:
        payload = _read_long_workspace_index_unlocked()
        records = payload["projects"].get(job.project) or []
        record = next((item for item in records if item.get("id") == job.workspace_id), None)
        if record is None:
            return
        record["job_id"] = job.id
        record["manifest_path"] = str(
            job.run_dir / ("real_long_manifest.json" if is_real_person_long_job(job) else "long_manifest.json")
        )
        record["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _write_long_workspace_index_unlocked(payload)


def public_long_workspace(record: dict[str, Any], project: str) -> dict[str, Any]:
    job_id = str(record.get("job_id") or "")
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job and job.project == project:
        job_public = job.public()
        status = str(job_public.get("status") or "new")
        stage = str(job_public.get("stage") or "")
        progress = int(job_public.get("progress") or 0)
        shot_count = int(job_public.get("shot_count") or 0)
        has_output = bool(job_public.get("has_output"))
    else:
        snapshot: dict[str, Any] = {}
        manifest_value = str(record.get("manifest_path") or "")
        if manifest_value:
            manifest = Path(manifest_value).expanduser().resolve()
            runs_root = (PROJECT_DIR / "runs").resolve()
            if manifest.is_file() and manifest.is_relative_to(runs_root):
                try:
                    loaded = json.loads(manifest.read_text(encoding="utf-8"))
                    snapshot = loaded if isinstance(loaded, dict) else {}
                except (OSError, ValueError, TypeError):
                    snapshot = {}
        status = str(snapshot.get("status") or ("saved" if job_id else "new"))
        if status in {"queued", "running"}:
            status = "paused"
        stage = str(snapshot.get("stage") or ("等待继续" if job_id else "尚未上传原片"))
        progress = int(snapshot.get("progress") or 0)
        shot_count = len(snapshot.get("shots") or [])
        has_output = bool(snapshot.get("output"))
    return {
        "id": str(record.get("id") or ""),
        "name": str(record.get("name") or "未命名项目"),
        "job_id": job_id,
        "status": status,
        "stage": stage,
        "progress": max(0, min(100, progress)),
        "shot_count": shot_count,
        "has_output": has_output,
        "created_at": str(record.get("created_at") or ""),
        "updated_at": str(record.get("updated_at") or ""),
    }


def list_long_workspaces(project: str) -> list[dict[str, Any]]:
    records = ensure_long_workspace(project)
    return [
        public_long_workspace(record, project)
        for record in sorted(records, key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    ]


def is_real_person_long_job(job: WebJob) -> bool:
    return job.project == REAL_PERSON_LONG_PROJECT


def real_long_shot_is_skipped(shot: dict[str, Any]) -> bool:
    """Return whether a real-person final shot is intentionally omitted."""
    return bool(shot.get("generation_skipped"))


def real_long_white_model_is_approved(shot: dict[str, Any]) -> bool:
    path_value = str(shot.get("white_model_path") or "")
    if not path_value or not Path(path_value).is_file():
        return False
    qa = shot.get("white_model_qa")
    if white_model_has_hard_text_violation(qa):
        return False
    return bool(
        (isinstance(qa, dict) and qa.get("passed") is True)
        or shot.get("white_model_manually_approved") is True
    )


def white_model_has_hard_text_violation(qa: Any) -> bool:
    """Text in a white model is never a manually approvable quality difference."""
    if not isinstance(qa, dict):
        return False
    reasons = "；".join(str(value) for value in qa.get("reasons") or [])
    return any(token in reasons for token in ("字幕", "文字", "时间码", "Logo", "水印"))


def rebuild_real_long_white_model_merge(job: WebJob) -> Path | None:
    active = [shot for shot in job.shots if not real_long_shot_is_skipped(shot)]
    if not active or not all(real_long_white_model_is_approved(shot) for shot in active):
        job.white_model_path = None
        return None
    outputs = [Path(str(shot["white_model_path"])) for shot in active]
    job.white_model_path = concatenate_videos(outputs, job.run_dir / "白模绿幕视频.mp4")
    return job.white_model_path


def persist_cloud_job(job: WebJob, *, status: str, **values: Any) -> Path:
    """Persist enough cloud-task state to survive a local service restart."""
    path = job.run_dir / "job.json"
    record: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                record.update(loaded)
        except (OSError, ValueError, TypeError):
            pass
    record.update(
        {
            "local_job_id": job.id,
            "kind": job.kind,
            "project": job.project or project_for_kind(job.kind),
            "task_id": job.task_id,
            "status": status,
            "cloud_status": job.cloud_status,
            "submitted_at": job.cloud_started_at,
            "created_at": job.created_at,
            "depth": str(job.depth_path) if job.depth_path else "",
            "scene": str(job.scene_path) if job.scene_path else "",
            "person": str(job.person_path) if job.person_path else "",
            "clothing": str(job.clothing_path) if job.clothing_path else "",
            "output": str(job.output_path) if job.output_path else "",
            "duration": job.generation_duration,
            "recovery_action": job.recovery_action,
        }
    )
    record.update(values)
    return save_job_record(job.run_dir, record)


def _local_record_file(run_dir: Path, value: Any, fallback: str = "") -> Path | None:
    raw = str(value or "").strip()
    candidate = Path(raw).expanduser() if raw else run_dir / fallback if fallback else None
    if candidate is None:
        return None
    try:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(run_dir.resolve()) or not resolved.is_file():
            return None
        return resolved
    except (OSError, ValueError):
        return None


def _run_artifact(run_dir: Path, stem: str, suffixes: set[str]) -> Path | None:
    """Return a non-empty artifact inside one run directory."""
    try:
        resolved_run_dir = run_dir.resolve()
    except OSError:
        return None
    for candidate in sorted(run_dir.glob(f"{stem}.*")):
        try:
            resolved = candidate.resolve()
            if (
                resolved.parent == resolved_run_dir
                and resolved.suffix.lower() in suffixes
                and resolved.is_file()
                and resolved.stat().st_size > 0
            ):
                return resolved
        except OSError:
            continue
    return None


def _runs_record_file(value: Any) -> Path | None:
    """Resolve a persisted artifact only when it remains inside this project's runs."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        candidate = Path(raw).expanduser().resolve()
        runs_root = (PROJECT_DIR / "runs").resolve()
        if candidate.is_relative_to(runs_root) and candidate.is_file():
            return candidate
    except (OSError, ValueError):
        pass
    return None


def resume_cloud_job(job: WebJob) -> None:
    """Resume a previously submitted Seedance task without creating a new paid task."""
    try:
        client = api_client()
        job.update(status="running", stage="本地服务已恢复，正在继续查询 Seedance 任务", progress=65, error="")
        job.log(f"已自动恢复 Seedance 任务：{job.task_id}；不会重复提交或重复计费。")

        def on_status(item: dict[str, Any]) -> None:
            status = str(item.get("status") or "unknown")
            base = 65 if status == "queued" else 68 if status == "running" else 89
            job.update(
                stage=f"Seedance 状态：{status}",
                progress=base,
                cloud_status=status,
                cloud_updated_at=time.time(),
            )
            job.log(f"Seedance 状态：{status}")
            persist_cloud_job(job, status="running")

        task = client.wait_for_task(job.task_id, on_status=on_status)
        try:
            returned_duration = int(float(task.get("duration") or 0))
        except (TypeError, ValueError):
            returned_duration = 0
        if returned_duration:
            job.update(generation_duration=returned_duration)
        video_url = str((task.get("content") or {}).get("video_url") or "")
        if not video_url:
            raise WorkflowError("任务成功，但下载 URL 已不存在或已过期。")
        prefix = (
            "多人复刻成片"
            if job.project == "multi_person_replication"
            else "只更换场景成片"
            if job.project == "scene_only_replacement"
            else "只更换服装成片"
            if job.project == "clothing_only_replacement"
            else "长视频分镜成片"
            if is_long_video_project(job.project)
            else "生成成片"
        )
        output = job.run_dir / f"{prefix}_{job.task_id}.mp4"
        job.update(stage="正在下载 Seedance 成片", progress=90, cloud_status="succeeded")
        download_file(
            video_url,
            output,
            on_retry=lambda attempt, total, error: job.log(
                f"成片下载连接中断，正在从断点自动重试 {attempt}/{total}：{error}"
            ),
        )
        job.output_path = output
        job.update(status="succeeded", stage="Seedance 成片已自动恢复并下载", progress=100, error="")
        job.log(f"成片已下载：{output.name}")
        persist_cloud_job(job, status="succeeded", usage=task.get("usage"), error="")
    except Exception as exc:
        job_error(job, exc)
        persist_cloud_job(job, status="failed", error=str(exc))


def restore_cloud_job(job_id: str, *, resume: bool = True) -> WebJob | None:
    """Restore one job by its old local id and continue polling when needed."""
    with JOBS_LOCK:
        existing = JOBS.get(job_id)
    if existing:
        return existing
    if not re.fullmatch(r"[A-Za-z0-9-]{4,64}", job_id):
        return None
    run_dirs = sorted(
        (PROJECT_DIR / "runs").glob(f"20*_web_*_{job_id}"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for run_dir in run_dirs:
        record_path = run_dir / "job.json"
        if not record_path.is_file():
            continue
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        task_id = str(record.get("task_id") or "").strip()
        if not task_id:
            continue
        output = _local_record_file(run_dir, record.get("output")) or _runs_record_file(record.get("output"))
        depth = _local_record_file(run_dir, record.get("depth"), "depth.mp4") or _runs_record_file(record.get("depth"))
        scene = _local_record_file(run_dir, record.get("scene"), "scene_reference.jpg") or _runs_record_file(record.get("scene"))
        person = _local_record_file(run_dir, record.get("person")) or _runs_record_file(record.get("person"))
        clothing = _local_record_file(run_dir, record.get("clothing")) or _runs_record_file(record.get("clothing"))
        project = str(record.get("project") or project_for_kind(str(record.get("kind") or "query")))
        kind = str(record.get("kind") or "query")
        try:
            submitted_at = float(record.get("submitted_at") or 0)
        except (TypeError, ValueError):
            submitted_at = 0
        job = WebJob(
            id=job_id,
            kind=kind,
            project=project,
            run_dir=run_dir,
            status="succeeded" if output else "running",
            stage="已恢复本地成片" if output else "正在自动恢复 Seedance 任务",
            progress=100 if output else 65,
            task_id=task_id,
            depth_path=depth,
            scene_path=scene,
            person_path=person,
            clothing_path=clothing,
            output_path=output,
            cloud_status="succeeded" if output else str(record.get("cloud_status") or "queued"),
            cloud_started_at=submitted_at or time.time(),
            generation_duration=int(record.get("duration") or 0),
            created_at=str(record.get("created_at") or datetime.now().isoformat(timespec="seconds")),
        )
        job.log("已从本地任务记录恢复，页面无需手动输入 Seedance 任务 ID。")
        with JOBS_LOCK:
            winner = JOBS.setdefault(job.id, job)
        if winner is not job:
            return winner
        if output is None and resume:
            threading.Thread(
                target=resume_cloud_job,
                args=(job,),
                daemon=True,
                name=f"resume-cloud-{job.id}",
            ).start()
        return job
    return None


def restore_latest_cloud_job(*, resume: bool = True) -> WebJob | None:
    records = sorted(
        (PROJECT_DIR / "runs").glob("20*_web_*/job.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for record_path in records:
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not str(record.get("task_id") or "").strip():
            continue
        job_id = str(record.get("local_job_id") or record_path.parent.name.rsplit("_", 1)[-1])
        restored = restore_cloud_job(job_id, resume=resume)
        if restored:
            return restored
    return None


def get_job(job_id: str) -> WebJob:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise WorkflowError("找不到该任务；本地服务重启后，请使用 Seedance 任务 ID 恢复查询。")
    return job


def restore_latest_depth_job() -> WebJob | None:
    with JOBS_LOCK:
        existing = JOBS.get(RESTORED_DEPTH_JOB_ID)
    if existing and existing.depth_path and existing.depth_path.is_file():
        return existing
    candidates = sorted(
        (PROJECT_DIR / "runs").glob("20*_web_*/depth*.mp4"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            info = inspect_video(path)
        except Exception:
            continue
        if info.duration > MAX_DEPTH_VIDEO_SECONDS + 1e-6:
            continue
        if info.size_bytes > MAX_DEPTH_VIDEO_BYTES:
            continue
        output_path: Path | None = None
        task_id = ""
        recorded_duration = 0
        record_path = path.parent / "job.json"
        if record_path.is_file():
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
                candidate = Path(str(record.get("output") or "")).expanduser().resolve()
                if candidate.is_file() and candidate.parent == path.parent.resolve():
                    output_path = candidate
                    task_id = str(record.get("task_id") or "").strip()
                    recorded_duration = int(record.get("duration") or 0)
            except (OSError, ValueError, TypeError):
                pass
        job = WebJob(
            id=RESTORED_DEPTH_JOB_ID,
            kind="full" if output_path else "depth",
            run_dir=path.parent,
            status="succeeded",
            stage="已恢复最近完整结果" if output_path else "已恢复最近深度视频",
            progress=100,
            task_id=task_id,
            depth_path=path,
            output_path=output_path,
            cloud_status="succeeded" if output_path else "",
            source_duration=info.duration,
            depth_duration=info.duration,
            generation_duration=recorded_duration or match_seedance_duration(info.duration),
        )
        job.log(f"已恢复本地深度视频：{path.name}")
        if output_path:
            job.log(f"已恢复本地最终成片：{output_path.name}")
        with JOBS_LOCK:
            JOBS[RESTORED_DEPTH_JOB_ID] = job
        return job
    return None


def restore_latest_scene_job() -> WebJob | None:
    with JOBS_LOCK:
        existing = JOBS.get(RESTORED_SCENE_JOB_ID)
    if existing and existing.scene_path and existing.scene_path.is_file():
        return existing
    candidates = sorted(
        list((PROJECT_DIR / "runs").glob("20*_web_*/scene_reference.*")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            continue
        if not path.is_file() or path.stat().st_size <= 0:
            continue
        depth_path = path.parent / "depth.mp4"
        if not depth_path.is_file():
            depth_path = None
        source_duration = 0.0
        generation_duration = 0
        if depth_path is not None:
            try:
                info = inspect_video(depth_path)
                source_duration = info.duration
                generation_duration = match_seedance_duration(info.duration)
            except Exception:
                depth_path = None
        job = WebJob(
            id=RESTORED_SCENE_JOB_ID,
            kind="person_prepare",
            run_dir=path.parent,
            status="succeeded",
            stage="已恢复最近的原片场景参考",
            progress=100,
            depth_path=depth_path,
            scene_path=path,
            person_path=_run_artifact(path.parent, "person", {".jpg", ".jpeg", ".png", ".webp"}),
            clothing_path=_run_artifact(path.parent, "clothing", {".jpg", ".jpeg", ".png", ".webp"}),
            source_duration=source_duration,
            depth_duration=source_duration,
            generation_duration=generation_duration,
        )
        job.log(f"已恢复本地场景参考：{path.name}")
        with JOBS_LOCK:
            JOBS[RESTORED_SCENE_JOB_ID] = job
        return job
    return None


def restore_person_retry_job(job_id: str) -> WebJob | None:
    """Restore a Project 3 preparation that stopped before a scene image arrived."""
    with JOBS_LOCK:
        existing = JOBS.get(job_id)
    if existing:
        return existing
    if not re.fullmatch(r"[A-Za-z0-9-]{4,64}", job_id):
        return None
    run_dirs = sorted(
        (PROJECT_DIR / "runs").glob(f"20*_web_person_*_{job_id}"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for run_dir in run_dirs:
        depth = run_dir / "depth.mp4"
        source = _run_artifact(run_dir, "reference", {".mp4", ".mov"})
        scene = _run_artifact(run_dir, "scene_reference", {".jpg", ".jpeg", ".png", ".webp"})
        frames = list(run_dir.glob("scene_source_*.jpg"))
        if not depth.is_file() or source is None or scene is not None or not frames:
            continue
        try:
            info = inspect_video(depth)
        except Exception:
            continue
        kind = "person_full" if "_web_person_full_" in run_dir.name else "person_prepare"
        job = WebJob(
            id=job_id,
            kind=kind,
            project="person_only_replacement",
            run_dir=run_dir,
            status="failed",
            stage="Seedream 连接中断，可安全重试场景提取",
            progress=47 if kind == "person_full" else 73,
            depth_path=depth.resolve(),
            person_path=_run_artifact(run_dir, "person", {".jpg", ".jpeg", ".png", ".webp"}),
            clothing_path=_run_artifact(run_dir, "clothing", {".jpg", ".jpeg", ".png", ".webp"}),
            error=(
                "上次在等待 Seedream 场景图时连接中断。深度视频和参考帧已保留；"
                "请手动确认后只重试场景提取，系统不会自动重复付费请求。"
            ),
            recovery_action="retry_scene",
            source_duration=info.duration,
            depth_duration=info.duration,
            generation_duration=match_seedance_duration(info.duration),
            created_at=datetime.fromtimestamp(run_dir.stat().st_mtime).isoformat(timespec="seconds"),
        )
        job.log("已恢复中断任务：深度视频和三张场景参考帧均完整，无需重新生成深度视频。")
        job.log("Seedream 创建接口未返回可恢复的任务 ID；为避免重复计费，等待你手动确认重试。")
        with JOBS_LOCK:
            winner = JOBS.setdefault(job.id, job)
        return winner
    return None


def restore_latest_person_retry_job() -> WebJob | None:
    """Restore only the newest Project 3 preparation when it is incomplete."""
    run_dirs = sorted(
        [
            *list((PROJECT_DIR / "runs").glob("20*_web_person_prepare_*")),
            *list((PROJECT_DIR / "runs").glob("20*_web_person_full_*")),
        ],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for run_dir in run_dirs:
        # A newer completed scene supersedes any older interrupted preparation.
        if _run_artifact(run_dir, "scene_reference", {".jpg", ".jpeg", ".png", ".webp"}) is not None:
            return None
        if not (run_dir / "depth.mp4").is_file():
            continue
        job_id = run_dir.name.rsplit("_", 1)[-1]
        return restore_person_retry_job(job_id)
    return None


def restore_person_submission_job(job_id: str) -> WebJob | None:
    """Restore a Project 3 job whose Seedance create response was lost."""
    with JOBS_LOCK:
        existing = JOBS.get(job_id)
    if existing:
        return existing
    if not re.fullmatch(r"[A-Za-z0-9-]{4,64}", job_id):
        return None
    run_dirs = sorted(
        (PROJECT_DIR / "runs").glob(f"20*_web_person_*_{job_id}"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for run_dir in run_dirs:
        record: dict[str, Any] = {}
        record_path = run_dir / "job.json"
        if record_path.is_file():
            try:
                loaded = json.loads(record_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    record = loaded
            except (OSError, ValueError, TypeError):
                pass
        if str(record.get("task_id") or "").strip():
            continue
        depth = _runs_record_file(record.get("depth")) or (
            (run_dir / "depth.mp4").resolve() if (run_dir / "depth.mp4").is_file() else None
        )
        scene = _runs_record_file(record.get("scene")) or _run_artifact(
            run_dir, "scene_reference", {".jpg", ".jpeg", ".png", ".webp"}
        )
        source = _run_artifact(run_dir, "reference", {".mp4", ".mov"})
        output = next(iter(run_dir.glob("生成成片_*.mp4")), None)
        if depth is None or scene is None or output is not None:
            continue
        # A full job has a local source. A generate-from-prepared job is restorable
        # when its submission intent record points to the reused depth and scene.
        if source is None and not record:
            continue
        try:
            info = inspect_video(depth)
        except Exception:
            continue
        submitted_at = float(record.get("submitted_at") or scene.stat().st_mtime)
        job = WebJob(
            id=job_id,
            kind=str(record.get("kind") or ("person_full" if "_web_person_full_" in run_dir.name else "person_generate")),
            project="person_only_replacement",
            run_dir=run_dir,
            status="failed",
            stage="Seedance 创建响应超时，可安全找回已创建任务",
            progress=60,
            depth_path=depth,
            scene_path=scene,
            person_path=_runs_record_file(record.get("person")) or _run_artifact(
                run_dir, "person", {".jpg", ".jpeg", ".png", ".webp"}
            ),
            clothing_path=_runs_record_file(record.get("clothing")) or _run_artifact(
                run_dir, "clothing", {".jpg", ".jpeg", ".png", ".webp"}
            ),
            error=(
                "Seedance 创建请求的响应超时，但云端任务可能已经生成。"
                "系统将只读取任务列表并按模型、分辨率、比例、时长和声音设置匹配，不会再次提交付费请求。"
            ),
            recovery_action="recover_seedance_submission",
            source_duration=info.duration,
            depth_duration=info.duration,
            generation_duration=int(record.get("duration") or match_seedance_duration(info.duration)),
            created_at=datetime.fromtimestamp(submitted_at).isoformat(timespec="seconds"),
        )
        job.log("已恢复 Seedance 提交响应中断任务；本地深度视频和场景参考均完整。")
        job.log("可以安全查找对应的方舟任务；此操作不会创建新任务，也不会产生重复费用。")
        with JOBS_LOCK:
            winner = JOBS.setdefault(job.id, job)
        return winner
    return None


def restore_latest_person_submission_job() -> WebJob | None:
    records = sorted(
        (PROJECT_DIR / "runs").glob("20*_web_person_*/job.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for record_path in records:
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if str(record.get("task_id") or "").strip():
            continue
        if str(record.get("status") or "") not in {"submitting", "ambiguous"}:
            continue
        job_id = str(record.get("local_job_id") or record_path.parent.name.rsplit("_", 1)[-1])
        return restore_person_submission_job(job_id)

    # Compatibility recovery for the job that failed before submission intents
    # were persisted. Only consider the newest Project 3 full run.
    run_dirs = sorted(
        (PROJECT_DIR / "runs").glob("20*_web_person_full_*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for run_dir in run_dirs:
        if _run_artifact(run_dir, "reference", {".mp4", ".mov"}) is None:
            continue
        if next(iter(run_dir.glob("生成成片_*.mp4")), None) is not None:
            return None
        if _run_artifact(run_dir, "scene_reference", {".jpg", ".jpeg", ".png", ".webp"}) is None:
            return None
        job_id = run_dir.name.rsplit("_", 1)[-1]
        return restore_person_submission_job(job_id)
    return None


def restore_latest_scene_only_job() -> WebJob | None:
    """Restore the newest Project 4 depth and extracted subject references."""
    recovery_records = sorted(
        (PROJECT_DIR / "runs").glob("20*_web_scene_*/job.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for record_path in recovery_records:
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if str(record.get("task_id") or "").strip():
            continue
        if str(record.get("status") or "") not in {"submitting", "ambiguous"}:
            continue
        depth = _runs_record_file(record.get("depth"))
        person = _runs_record_file(record.get("person"))
        clothing = _runs_record_file(record.get("clothing"))
        scene = _runs_record_file(record.get("scene"))
        if not all((depth, person, clothing, scene)):
            continue
        assert depth is not None
        try:
            info = inspect_video(depth)
        except Exception:
            continue
        job_id = str(record.get("local_job_id") or record_path.parent.name.rsplit("_", 1)[-1])
        with JOBS_LOCK:
            existing = JOBS.get(job_id)
        if existing:
            return existing
        job = WebJob(
            id=job_id,
            kind=str(record.get("kind") or "scene_generate"),
            project="scene_only_replacement",
            run_dir=record_path.parent,
            status="failed",
            stage="Seedance 创建响应超时，可安全找回已创建任务",
            progress=60,
            depth_path=depth,
            person_path=person,
            clothing_path=clothing,
            scene_path=scene,
            error="Seedance 创建响应中断；本地项目 4 素材已完整保留。",
            recovery_action="recover_seedance_submission",
            source_duration=info.duration,
            depth_duration=info.duration,
            generation_duration=int(record.get("duration") or match_seedance_duration(info.duration)),
            created_at=str(record.get("created_at") or datetime.now().isoformat(timespec="seconds")),
        )
        job.log("已恢复项目 4 的 Seedance 提交中断任务；可只读查找，不会重新付费提交。")
        with JOBS_LOCK:
            winner = JOBS.setdefault(job.id, job)
        return winner

    run_dirs = sorted(
        [
            *list((PROJECT_DIR / "runs").glob("20*_web_scene_prepare_*")),
            *list((PROJECT_DIR / "runs").glob("20*_web_scene_full_*")),
        ],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for run_dir in run_dirs:
        depth = run_dir / "depth.mp4"
        source = _run_artifact(run_dir, "reference", {".mp4", ".mov"})
        if not depth.is_file() or source is None:
            continue
        job_id = run_dir.name.rsplit("_", 1)[-1]
        with JOBS_LOCK:
            existing = JOBS.get(job_id)
        if existing:
            return existing
        try:
            info = inspect_video(depth)
        except Exception:
            continue
        person = _run_artifact(
            run_dir, "original_person_triview", {".jpg", ".jpeg", ".png", ".webp"}
        )
        clothing = _run_artifact(
            run_dir, "original_clothing_triview", {".jpg", ".jpeg", ".png", ".webp"}
        )
        scene = _run_artifact(run_dir, "new_scene", {".jpg", ".jpeg", ".png", ".webp", ".bmp"})
        ready = person is not None and clothing is not None
        job = WebJob(
            id=job_id,
            kind="scene_full" if "_web_scene_full_" in run_dir.name else "scene_prepare",
            project="scene_only_replacement",
            run_dir=run_dir,
            status="succeeded" if ready else "failed",
            stage="已恢复原人物与服装三视图" if ready else "人物服装提取中断，可安全重试",
            progress=100 if ready else 58,
            depth_path=depth.resolve(),
            scene_path=scene,
            person_path=person,
            clothing_path=clothing,
            error="" if ready else "Seedream 人物或服装三视图未完整返回；已有结果和深度视频均已保留。",
            recovery_action="" if ready else "retry_scene_subject",
            source_duration=info.duration,
            depth_duration=info.duration,
            generation_duration=match_seedance_duration(info.duration),
            created_at=datetime.fromtimestamp(run_dir.stat().st_mtime).isoformat(timespec="seconds"),
        )
        job.log("已恢复项目 4 本地素材；不会重新生成已经存在的三视图。")
        with JOBS_LOCK:
            winner = JOBS.setdefault(job.id, job)
        return winner
    return None


def restore_latest_clothing_only_job() -> WebJob | None:
    """Restore the newest Project 5 preparation or cloud result."""
    with JOBS_LOCK:
        candidates_in_memory = [
            job for job in JOBS.values() if job.project == "clothing_only_replacement"
        ]
    if candidates_in_memory:
        return max(candidates_in_memory, key=lambda item: item.created_at)
    run_dirs = sorted(
        [
            *list((PROJECT_DIR / "runs").glob("20*_web_clothing_prepare_*")),
            *list((PROJECT_DIR / "runs").glob("20*_web_clothing_full_*")),
            *list((PROJECT_DIR / "runs").glob("20*_web_clothing_generate_*")),
        ],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for run_dir in run_dirs:
        depth = _run_artifact(run_dir, "depth", {".mp4"})
        if depth is None:
            record_path = run_dir / "job.json"
            if record_path.is_file():
                try:
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    record = {}
                depth = _runs_record_file(record.get("depth"))
            else:
                record = {}
        else:
            record = {}
        person = _run_artifact(run_dir, "original_person_triview", {".jpg", ".jpeg", ".png", ".webp"})
        scene = _run_artifact(run_dir, "scene_reference", {".jpg", ".jpeg", ".png", ".webp"})
        clothing = _run_artifact(run_dir, "new_clothing", {".jpg", ".jpeg", ".png", ".webp", ".bmp"})
        output = next(iter(run_dir.glob("生成成片_*.mp4")), None)
        if not record and (run_dir / "job.json").is_file():
            try:
                loaded = json.loads((run_dir / "job.json").read_text(encoding="utf-8"))
                record = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError, TypeError):
                record = {}
        person = person or _runs_record_file(record.get("person"))
        scene = scene or _runs_record_file(record.get("scene"))
        clothing = clothing or _runs_record_file(record.get("clothing"))
        output = output or _runs_record_file(record.get("output"))
        if depth is None or person is None or scene is None:
            continue
        try:
            info = inspect_video(depth)
        except Exception:
            continue
        job_id = str(record.get("local_job_id") or run_dir.name.rsplit("_", 1)[-1])
        ready = output is not None or (person is not None and scene is not None)
        job = WebJob(
            id=job_id,
            kind=str(record.get("kind") or ("clothing_full" if "_full_" in run_dir.name else "clothing_prepare")),
            project="clothing_only_replacement",
            run_dir=run_dir,
            status="succeeded" if ready else "failed",
            stage="已恢复项目 5 最终成片" if output else "已恢复原人物与原场景参考",
            progress=100,
            task_id=str(record.get("task_id") or ""),
            depth_path=depth,
            person_path=person,
            scene_path=scene,
            clothing_path=clothing,
            output_path=output,
            cloud_status="succeeded" if output else "",
            source_duration=info.duration,
            depth_duration=info.duration,
            generation_duration=int(record.get("duration") or match_seedance_duration(info.duration)),
            created_at=str(record.get("created_at") or datetime.fromtimestamp(run_dir.stat().st_mtime).isoformat(timespec="seconds")),
        )
        job.log("已从本地恢复项目 5 素材，不会重新调用付费接口。")
        with JOBS_LOCK:
            return JOBS.setdefault(job.id, job)
    return None


def _persist_long_job(job: WebJob) -> Path:
    payload = {
        "local_job_id": job.id,
        "kind": job.kind,
        "project": job.project or VIRTUAL_LONG_PROJECT,
        "workspace_id": job.workspace_id,
        "workspace_name": job.workspace_name,
        "status": job.status,
        "stage": job.stage,
        "progress": job.progress,
        "pause_requested": job.pause_requested,
        "created_at": job.created_at,
        "source_duration": job.source_duration,
        "generation_strategy": job.generation_strategy,
        "person": str(job.person_path) if job.person_path else "",
        "clothing": str(job.clothing_path) if job.clothing_path else "",
        "mosaic": str(job.mosaic_path) if job.mosaic_path else "",
        "white_model": str(job.white_model_path) if job.white_model_path else "",
        "white_reference": str(job.white_reference_path) if job.white_reference_path else "",
        "performance": str(job.performance_path) if job.performance_path else "",
        "actors": job.actors,
        "cast_continuity": job.cast_continuity,
        "scene_groups": job.scene_groups,
        "output": str(job.output_path) if job.output_path else "",
        "shots": job.shots,
    }
    manifest_name = "real_long_manifest.json" if is_real_person_long_job(job) else "long_manifest.json"
    manifest = save_shot_manifest(job.run_dir / manifest_name, payload)
    touch_long_workspace(job)
    return manifest


def restore_latest_long_video_job(
    project: str = VIRTUAL_LONG_PROJECT,
    job_id: str = "",
) -> WebJob | None:
    if not is_long_video_project(project):
        raise WorkflowError("长视频项目类型无效。")
    with JOBS_LOCK:
        in_memory = [
            job
            for job in JOBS.values()
            if job.project == project
            and (not job_id or job.id == job_id)
            and job.kind not in {"long_shot", "long_white_model_shot", "real_long_shot", "real_long_white_model_shot"}
        ]
    if in_memory:
        return max(in_memory, key=lambda item: item.created_at)
    manifest_glob = (
        "20*_web_real_long_*/real_long_manifest.json"
        if project == REAL_PERSON_LONG_PROJECT
        else "20*_web_long_*/long_manifest.json"
    )
    manifests = sorted(
        (PROJECT_DIR / "runs").glob(manifest_glob),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for manifest in manifests:
        try:
            record = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if str(record.get("project") or VIRTUAL_LONG_PROJECT) != project:
            continue
        restored_job_id = str(record.get("local_job_id") or manifest.parent.name.rsplit("_", 1)[-1])
        if job_id and restored_job_id != job_id:
            continue
        shots = record.get("shots")
        if not isinstance(shots, list) or not shots:
            continue
        safe_shots: list[dict[str, Any]] = []
        interrupted_real_generation = False
        for raw in shots:
            if not isinstance(raw, dict):
                continue
            shot = dict(raw)
            for key in (
                "source_path", "depth_path", "scene_path", "output_path", "mosaic_path",
                "white_model_path", "performance_path", "dialogue_timing_path",
            ):
                resolved = _runs_record_file(shot.get(key))
                shot[key] = str(resolved) if resolved else ""
            if (
                project == REAL_PERSON_LONG_PROJECT
                and str(shot.get("status") or "") in {"queued", "running"}
            ):
                interrupted_real_generation = True
                shot["status"] = "ready"
                shot["stage"] = "生成已暂停，等待人工核对人物映射"
                shot["error"] = ""
            safe_shots.append(shot)
        if not safe_shots:
            continue
        output = _runs_record_file(record.get("output"))
        person = _runs_record_file(record.get("person"))
        clothing = _runs_record_file(record.get("clothing"))
        mosaic = _runs_record_file(record.get("mosaic"))
        white_model = _runs_record_file(record.get("white_model"))
        white_reference = _runs_record_file(record.get("white_reference"))
        performance = _runs_record_file(record.get("performance"))
        safe_actors: list[dict[str, Any]] = []
        for raw_actor in record.get("actors") or []:
            if not isinstance(raw_actor, dict):
                continue
            actor = dict(raw_actor)
            for actor_key in (
                "person_source", "original_person_source", "masked_person_source",
                "sketch_person_source", "clothing_source",
            ):
                source_value = str(actor.get(actor_key) or "")
                if not source_value or source_value.startswith("asset://"):
                    continue
                resolved_source = _runs_record_file(source_value)
                actor[actor_key] = str(resolved_source) if resolved_source else ""
            if project == REAL_PERSON_LONG_PROJECT and actor.get("masked_person_source"):
                # Migrate older real-person manifests in memory so no generic
                # generation path can accidentally treat the raw upload as the
                # final identity reference.
                actor["person_source"] = str(actor["masked_person_source"])
            safe_actors.append(actor)
        safe_scene_groups: list[dict[str, Any]] = []
        for raw_group in record.get("scene_groups") or []:
            if not isinstance(raw_group, dict):
                continue
            group = dict(raw_group)
            images: list[str] = []
            for raw_image in group.get("images") or []:
                resolved_image = _runs_record_file(raw_image)
                if resolved_image:
                    images.append(str(resolved_image))
            if images and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", str(group.get("id") or "")):
                group["images"] = images
                safe_scene_groups.append(group)
        pending_real_composition = (
            project == REAL_PERSON_LONG_PROJECT
            and any(
                bool(shot.get("composition_approval_required"))
                and not real_long_shot_is_skipped(shot)
                for shot in safe_shots
            )
        )
        restored_continuity = (
            dict(record.get("cast_continuity"))
            if isinstance(record.get("cast_continuity"), dict)
            else {}
        )
        needs_manual_continuity_recovery = bool(
            project == REAL_PERSON_LONG_PROJECT
            and str(record.get("status") or "") == "failed"
            and all(isinstance(shot.get("performance"), dict) for shot in safe_shots)
            and not (restored_continuity.get("assignments") or [])
        )
        restored_manual_continuity = bool(
            project == REAL_PERSON_LONG_PROJECT
            and (
                restored_continuity.get("manual_review_required") is True
                or needs_manual_continuity_recovery
            )
        )
        if needs_manual_continuity_recovery:
            restored_continuity = {
                "version": 1,
                "characters": [],
                "assignments": [],
                "manual_review_required": True,
                "analysis_error": "已恢复的任务完成了逐镜台词与表演分析，但未建立有效的跨镜自动身份。",
            }
        job = WebJob(
            id=restored_job_id,
            kind=str(record.get("kind") or "long_analyze"),
            project=project,
            workspace_id=str(record.get("workspace_id") or ""),
            workspace_name=str(record.get("workspace_name") or ""),
            run_dir=manifest.parent,
            status=(
                "paused"
                if project == REAL_PERSON_LONG_PROJECT and str(record.get("status") or "") == "paused"
                else "awaiting_approval"
                if pending_real_composition
                else "succeeded"
            ),
            stage=(
                str(record.get("stage") or "真实人物生成已暂停，不会提交后续分镜")
                if project == REAL_PERSON_LONG_PROJECT and str(record.get("status") or "") == "paused"
                else str(record.get("stage") or "等待人工同意继续构图纠偏；后续分镜尚未提交")
                if pending_real_composition
                else "真实人物生成已暂停，等待核对逐镜人物映射"
                if interrupted_real_generation
                else "逐镜分析已恢复，跨镜身份需人工绑定"
                if restored_manual_continuity
                else "已恢复长视频最终成片" if output else "已恢复分镜分析，可继续生成"
            ),
            progress=(int(record.get("progress") or 0) if pending_real_composition else 100),
            pause_requested=str(record.get("status") or "") == "paused",
            person_path=person,
            clothing_path=clothing,
            mosaic_path=mosaic,
            white_model_path=white_model,
            white_reference_path=white_reference,
            performance_path=performance,
            actors=safe_actors,
            cast_continuity=restored_continuity,
            scene_groups=safe_scene_groups,
            output_path=output,
            shots=safe_shots,
            source_duration=float(record.get("source_duration") or 0),
            generation_strategy=str(
                record.get("generation_strategy") or REAL_PER_SHOT_GENERATION_STRATEGY
            ),
            created_at=str(record.get("created_at") or datetime.fromtimestamp(manifest.stat().st_mtime).isoformat(timespec="seconds")),
        )
        job.log(f"已恢复 {len(safe_shots)} 个分镜及现有结果。")
        if restored_manual_continuity:
            job.log("跨镜自动身份结果不可用；已保留逐镜分析并切换为人工逐镜人物绑定。")
        if interrupted_real_generation:
            job.log(
                "检测到上次真实人物生成在本地中断，已保持暂停状态；"
                "不会自动查询、重新提交或继续生成付费分镜。"
            )
        with JOBS_LOCK:
            if project == REAL_PERSON_LONG_PROJECT:
                # A whole-video Seedance task also leaves a generic cloud record
                # with the same local id. The real-project manifest owns the UI
                # state, mappings, white models and generation strategy, so it
                # must replace that shallow cloud-recovery object after restart.
                JOBS[job.id] = job
                return job
            return JOBS.setdefault(job.id, job)
    return None


def requested_long_project() -> str:
    return REAL_PERSON_LONG_PROJECT if request.path.startswith("/api/real-long-video") else VIRTUAL_LONG_PROJECT


def restore_expected_long_job(job_id: str, project: str | None = None) -> WebJob:
    expected = project or requested_long_project()
    try:
        job = get_job(job_id)
    except WorkflowError:
        job = restore_latest_long_video_job(expected, job_id)
        if job is None:
            raise
    if job.project != expected:
        raise WorkflowError("该任务不属于当前长视频重绘项目。")
    return job


def real_long_archive_dir() -> Path:
    directory = PROJECT_DIR / "runs" / "_real_long_archives"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def real_long_archive_path(archive_id: str) -> Path:
    value = str(archive_id or "").strip()
    if not re.fullmatch(r"[a-f0-9]{12}", value):
        raise WorkflowError("存档编号无效。")
    return real_long_archive_dir() / f"{value}.json"


def list_real_long_archives(
    workspace_id: str = "",
    *,
    workspace_job_id: str = "",
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in real_long_archive_dir().glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        snapshot = record.get("snapshot") if isinstance(record.get("snapshot"), dict) else {}
        archive_workspace_id = str(record.get("workspace_id") or snapshot.get("workspace_id") or "")
        archive_job_id = str(snapshot.get("local_job_id") or "")
        if workspace_id and archive_workspace_id and archive_workspace_id != workspace_id:
            continue
        if workspace_id and not archive_workspace_id and workspace_job_id and archive_job_id != workspace_job_id:
            continue
        records.append(
            {
                "id": str(record.get("id") or path.stem),
                "name": str(record.get("name") or "未命名存档"),
                "saved_at": str(record.get("saved_at") or ""),
                "job_id": archive_job_id,
                "workspace_id": archive_workspace_id,
                "shot_count": len(snapshot.get("shots") or []),
                "has_output": bool(snapshot.get("output")),
                "generation_strategy": str(
                    snapshot.get("generation_strategy") or REAL_PER_SHOT_GENERATION_STRATEGY
                ),
            }
        )
    return sorted(records, key=lambda item: item["saved_at"], reverse=True)


def api_client() -> ArkVideoClient:
    load_env_file(override=True)
    return ArkVideoClient(
        os.getenv("ARK_API_KEY", ""),
        base_url=os.getenv("ARK_BASE_URL", DEFAULT_ARK_BASE_URL),
    )


def ark_assets_credentials() -> tuple[str, str]:
    load_env_file(override=True)
    access_key = (
        os.getenv("ARK_ASSETS_ACCESS_KEY_ID", "").strip()
        or os.getenv("VOLCENGINE_ACCESS_KEY_ID", "").strip()
        or os.getenv("TOS_ACCESS_KEY", "").strip()
    )
    secret_key = (
        os.getenv("ARK_ASSETS_SECRET_ACCESS_KEY", "").strip()
        or os.getenv("VOLCENGINE_SECRET_ACCESS_KEY", "").strip()
        or os.getenv("TOS_SECRET_KEY", "").strip()
    )
    return access_key, secret_key


def ark_assets_configured() -> bool:
    access_key, secret_key = ark_assets_credentials()
    return bool(access_key and secret_key)


def ark_assets_project_name() -> str:
    load_env_file(override=True)
    return os.getenv("ARK_ASSETS_PROJECT_NAME", "default").strip() or "default"


def ark_assets_client() -> ArkAssetsClient:
    access_key, secret_key = ark_assets_credentials()
    return ArkAssetsClient(
        access_key,
        secret_key,
        host=os.getenv("ARK_ASSETS_HOST", "open.volcengineapi.com"),
        region=os.getenv("ARK_ASSETS_REGION", "cn-beijing"),
        project_name=ark_assets_project_name(),
    )


def performance_analyzer() -> ArkPerformanceAnalyzer:
    load_env_file(override=True)
    return ArkPerformanceAnalyzer(
        os.getenv("ARK_API_KEY", ""),
        base_url=os.getenv("ARK_BASE_URL", DEFAULT_ARK_BASE_URL),
        model=os.getenv("ARK_PERFORMANCE_MODEL", "doubao-seed-2-0-lite-260215"),
    )


def save_upload(storage: FileStorage | None, directory: Path, stem: str) -> Path:
    if storage is None or not storage.filename:
        raise WorkflowError(f"缺少上传文件：{stem}")
    suffix = Path(storage.filename).suffix.lower()
    if not suffix:
        suffix = ".bin"
    target = directory / f"{stem}{suffix}"
    storage.save(target)
    if not target.is_file() or target.stat().st_size == 0:
        raise WorkflowError(f"上传文件为空：{storage.filename}")
    return target


def form_bool(name: str, default: bool = False) -> bool:
    value = request.form.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def form_int(name: str, default: int) -> int:
    try:
        return int(request.form.get(name, str(default)))
    except ValueError as exc:
        raise WorkflowError(f"{name} 必须是整数。") from exc


def job_error(job: WebJob, exc: Exception) -> None:
    message = str(exc) or exc.__class__.__name__
    job.log(f"失败：{message}")
    job.update(status="failed", stage="任务失败", error=message)


def parse_depth_progress(job: WebJob, line: str, *, start: int = 0, span: int = 100) -> None:
    job.log(line)
    match = re.search(r"depth_progress=(\d+)/(\d+)", line)
    if match:
        current, total = (int(value) for value in match.groups())
        if total > 0:
            job.update(progress=min(start + span, start + int(span * current / total)))


def run_depth(job: WebJob, source: Path, output: Path, blur_range: str, *, start: int = 0, span: int = 100) -> Path:
    job.update(status="running", stage="正在生成单目深度视频", progress=start)
    job.log(f"开始处理参考视频：{source.name}")
    source_info = inspect_video(source)
    matched_duration = match_seedance_duration(min(source_info.duration, MAX_DEPTH_VIDEO_SECONDS))
    job.update(source_duration=source_info.duration, generation_duration=matched_duration)
    job.log(
        f"原片时长 {source_info.duration:.2f} 秒；最终成片将自动匹配为 {matched_duration} 秒。"
    )
    job.log("深度视频输出将自动限制在 14.5 秒、200 MB 以内。")
    result = run_depth_generation(
        source,
        output,
        blur_range=blur_range,
        on_log=lambda line: parse_depth_progress(job, line, start=start, span=span),
    )
    job.depth_path = result
    job.update(depth_duration=inspect_video(result).duration)
    job.log("深度视频生成完成。")
    return result


def run_scene_extraction(
    job: WebJob,
    source: Path,
    *,
    prompt: str,
    model: str,
    start: int = 0,
    span: int = 100,
    recovery_action: str = "retry_scene",
) -> Path:
    job.update(status="running", stage="正在从原视频选取场景参考帧", progress=start)
    job.log("正在从原视频的前段、中段和后段选取三张场景参考帧。")
    frames = extract_scene_reference_frames(source, job.run_dir, count=3)
    job.update(progress=min(start + span, start + int(span * 0.18)))
    job.log("场景参考帧已准备；Seedream 将以中间帧为主构图并结合前后帧恢复遮挡区域。")
    client = api_client()
    job.update(stage="Seedream 5.0 正在清除人物并重建原场景", progress=start + int(span * 0.25))
    try:
        result = client.generate_image(
            prompt=prompt.strip() or DEFAULT_SCENE_EXTRACTION_PROMPT,
            image_sources=[str(path) for path in frames],
            model=model.strip() or DEFAULT_SEEDREAM_MODEL,
            size="2K",
            watermark=False,
        )
    except ArkConnectionError as exc:
        job.update(recovery_action=recovery_action)
        raise WorkflowError(
            "Seedream 已接收请求，但连接在等待场景图时被远端重置，生成结果未知。"
            "深度视频和三张参考帧均已保留。系统没有自动重试，以避免重复计费；"
            "请点击“安全重试场景提取（复用深度）”并手动确认。"
        ) from exc
    job.update(stage="正在下载 Seedream 场景参考图", progress=start + int(span * 0.86))
    output = job.run_dir / "scene_reference.jpg"
    download_file(result["url"], output)
    job.scene_path = output
    job.update(progress=start + span, recovery_action="")
    job.log(f"原片干净场景参考已生成：{output.name}")
    return output


def _read_relative_video_frame(capture: cv2.VideoCapture, position: float) -> np.ndarray | None:
    frame_count = max(1, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 1))
    capture.set(cv2.CAP_PROP_POS_FRAMES, min(frame_count - 1, int((frame_count - 1) * position)))
    ok, frame = capture.read()
    return frame if ok and frame is not None else None


def build_shot_scene_layout_frames(
    source_video: Path,
    motion_video: Path,
    output_dir: Path,
    *,
    count: int = 3,
) -> list[Path]:
    """Create privacy-safe per-shot geometry boards from source edges and white-model occupancy."""
    source_capture = cv2.VideoCapture(str(source_video))
    motion_capture = cv2.VideoCapture(str(motion_video))
    if not source_capture.isOpened() or not motion_capture.isOpened():
        source_capture.release()
        motion_capture.release()
        return []
    output_dir.mkdir(parents=True, exist_ok=True)
    positions = np.linspace(0.1, 0.9, max(1, count))
    outputs: list[Path] = []
    try:
        for ordinal, position in enumerate(positions, start=1):
            source = _read_relative_video_frame(source_capture, float(position))
            motion = _read_relative_video_frame(motion_capture, float(position))
            if source is None or motion is None:
                continue
            height, width = source.shape[:2]
            motion = cv2.resize(motion, (width, height), interpolation=cv2.INTER_LINEAR)
            hsv = cv2.cvtColor(motion, cv2.COLOR_BGR2HSV)
            green = cv2.inRange(hsv, np.array([35, 45, 35]), np.array([95, 255, 255]))
            subject = cv2.bitwise_not(green)
            kernel_size = max(5, int(round(min(width, height) * 0.018)) | 1)
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
            subject = cv2.morphologyEx(subject, cv2.MORPH_CLOSE, kernel)
            subject = cv2.dilate(subject, kernel, iterations=1)
            subject_ratio = float(np.count_nonzero(subject)) / float(width * height)
            if not 0.01 <= subject_ratio <= 0.92:
                continue

            gray = cv2.cvtColor(source, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
            edges = cv2.Canny(gray, 45, 130)
            edges = cv2.dilate(edges, np.ones((2, 2), dtype=np.uint8), iterations=1)
            edges[subject > 0] = 0
            layout = np.full((height, width, 3), 242, dtype=np.uint8)
            layout[edges > 0] = (48, 48, 48)
            layout[subject > 0] = (158, 158, 158)
            contours, _hierarchy = cv2.findContours(subject, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(layout, contours, -1, (92, 92, 92), max(2, round(min(width, height) / 360)))
            output = output_dir / f"scene_layout_{ordinal:02d}.jpg"
            ok, encoded = cv2.imencode(".jpg", layout, [cv2.IMWRITE_JPEG_QUALITY, 94])
            if ok:
                encoded.tofile(str(output))
                outputs.append(output)
    finally:
        source_capture.release()
        motion_capture.release()
    return outputs


def run_long_scene_plate(
    job: WebJob,
    depth_video: Path,
    target_scene: Path,
    *,
    prompt: str,
    model: str,
    source_video: Path | None = None,
    motion_video: Path | None = None,
    shot_index: int = 0,
    strict_real_person_layout: bool = False,
    start: int = 42,
    span: int = 18,
) -> Path:
    """Recompose an uploaded target scene to one shot without exposing real-person frames."""
    job.update(status="running", stage="正在匹配新场景机位与构图", progress=start)
    layout_frames: list[Path] = []
    if source_video is not None and motion_video is not None and source_video.is_file() and motion_video.is_file():
        layout_frames = build_shot_scene_layout_frames(
            source_video,
            motion_video,
            job.run_dir / "scene_plate_layout_frames",
            count=3,
        )
    if len(layout_frames) == 3:
        job.log("已生成本分镜专属的三张隐私安全结构蓝图：场景透视线、人物尺度、站位与遮挡均按本镜锁定。")
    else:
        frames_dir = job.run_dir / "scene_plate_depth_frames"
        layout_frames = extract_scene_reference_frames(depth_video, frames_dir, count=3)
        job.log("本镜结构蓝图生成条件不足，已回退使用三张无身份信息的深度构图帧。")
    enforced_prompt = prompt.strip() or DEFAULT_LONG_SCENE_PLATE_PROMPT
    if strict_real_person_layout and motion_video is not None and motion_video.is_file():
        enforced_prompt = (
            f"{build_real_scene_geometry_prompt(motion_video)}"
            f"{REAL_SCENE_RENDER_QUALITY_CONSTRAINT}"
            f"{enforced_prompt}"
        )
    if shot_index > 0:
        enforced_prompt = (
            f"这是分镜{shot_index:02d}的独立机位。禁止沿用其他分镜或图1原始视角。"
            f"{enforced_prompt}"
        )
    client = api_client()
    job.update(stage="Seedream 5.0 正在生成本镜头新场景板", progress=start + int(span * 0.25))
    try:
        result = client.generate_image(
            prompt=enforced_prompt,
            image_sources=[str(target_scene), *[str(path) for path in layout_frames]],
            model=model.strip() or DEFAULT_SEEDREAM_MODEL,
            size="2K",
            watermark=False,
        )
    except ArkConnectionError as exc:
        raise WorkflowError(
            "Seedream 在生成新场景机位匹配图时连接中断，结果未知。系统没有自动重复付费请求；"
            "目标场景图和深度帧均已保留，可稍后安全重试本镜头。"
        ) from exc
    output = job.run_dir / "scene_plate.jpg"
    job.update(stage="正在下载本镜头新场景板", progress=start + int(span * 0.86))
    download_file(
        result["url"],
        output,
        on_retry=lambda attempt, total, error: job.log(
            f"新场景板下载连接中断，正在从断点自动重试 {attempt}/{total}：{error}"
        ),
    )
    job.scene_path = output
    job.update(progress=start + span)
    job.log(f"新场景已按本镜头机位重新构图：{output.name}")
    return output


def build_person_only_prompt() -> str:
    return (
        "参考@视频1，将原视频中的主角完整替换为@图片1中的人物形象，主角服装严格参考@图片2；"
        "原视频场景必须保留，背景、空间结构、固定陈设、色彩、材质、透视和环境光照严格参考@图片3，"
        "不得改变、扩建或重新设计场景；完全复刻原视频主角的全部动作、表情、视线和表演节奏，精准匹配"
        "脚步、抬手、摆臂、躯干倾斜、弹跳重心变化、转向、停顿与定格姿势，所有动作的起始时间、落点、"
        "强拍定格和节奏变化均与原视频帧级对齐，镜头运镜与画面构图完全复刻原视频；保持新人物身份和服装"
        "从头到尾稳定，禁止出现原人物面部、串脸、换装、重影或额外人物；@视频1仅作为深度、动作、遮挡和"
        "镜头约束，不要输出黑白深度风格，最终输出正常彩色视频。"
    )


def requested_character_asset() -> str:
    """Return one validated Ark AIGC character Asset selected by the current form."""
    value = request.form.get("person_asset", "").strip()
    if not value:
        return ""
    if not re.fullmatch(r"asset://[A-Za-z0-9_-]{6,160}", value):
        raise WorkflowError("火山角色素材 ID 无效；请选择角色库中状态为 Active 的人物。")
    return value


def save_person_only_references(job: WebJob, *, reuse_job: WebJob | None = None) -> tuple[str, str]:
    person_asset = requested_character_asset()
    if person_asset:
        person_source = person_asset
    else:
        person_upload = request.files.get("person_image")
        if person_upload is not None and person_upload.filename:
            job.person_path = save_upload(person_upload, job.run_dir, "person")
            person_source = str(job.person_path)
        elif reuse_job and reuse_job.person_path and reuse_job.person_path.is_file():
            job.person_path = reuse_job.person_path
            person_source = str(reuse_job.person_path)
            job.log("已复用中断任务中保存的新人物参考图。")
        else:
            raise WorkflowError("缺少新人物图片；请选择图片或填写授权人物素材 ID。")
    clothing_upload = request.files.get("clothing_image")
    if clothing_upload is not None and clothing_upload.filename:
        job.clothing_path = save_upload(clothing_upload, job.run_dir, "clothing")
        clothing_source = str(job.clothing_path)
    elif reuse_job and reuse_job.clothing_path and reuse_job.clothing_path.is_file():
        job.clothing_path = reuse_job.clothing_path
        clothing_source = str(reuse_job.clothing_path)
        job.log("已复用中断任务中保存的服装参考图。")
    else:
        raise WorkflowError("缺少新服装参考图。")
    return person_source, clothing_source


def _clean_long_role(value: str, index: int) -> str:
    role = " ".join(value.strip().split()) or f"原片人物 {index}"
    if len(role) > 200:
        raise WorkflowError(f"人物 {index} 的原片角色定位不能超过 200 个字符。")
    return role


def parse_long_shot_casts(job: WebJob, actor_count: int) -> dict[int, list[int]]:
    if not form_bool("cast_confirmed"):
        raise WorkflowError("请先核对每个分镜的出场角色，并勾选“已确认分镜角色配置”。")
    raw_value = request.form.get("shot_casts", "").strip()
    try:
        raw_items = json.loads(raw_value)
    except (TypeError, ValueError) as exc:
        raise WorkflowError("分镜角色配置格式无效，请重新分析或刷新页面后再试。") from exc
    if not isinstance(raw_items, list):
        raise WorkflowError("分镜角色配置必须是列表。")
    parsed: dict[int, list[int]] = {}
    valid_shots = {int(shot.get("index") or 0) for shot in job.shots}
    for item in raw_items:
        if not isinstance(item, dict):
            raise WorkflowError("分镜角色配置中存在无效记录。")
        try:
            shot_index = int(item.get("index"))
            raw_mappings = item.get("actor_mappings")
            if isinstance(raw_mappings, list):
                ordered_mappings = sorted(raw_mappings, key=lambda value: int(value.get("slot") or 0))
                slots = [int(value.get("slot") or 0) for value in ordered_mappings]
                if slots != list(range(1, len(slots) + 1)):
                    raise WorkflowError(f"分镜 {shot_index:02d} 的原片人物槽位必须从 1 连续编号。")
                actor_ids = [int(value.get("actor_id") or 0) for value in ordered_mappings]
            else:
                actor_ids = [int(value) for value in item.get("actor_ids", [])]
        except (TypeError, ValueError) as exc:
            raise WorkflowError("分镜编号或人物编号无效。") from exc
        if shot_index not in valid_shots:
            raise WorkflowError(f"找不到分镜 {shot_index:02d}。")
        if len(actor_ids) != len(set(actor_ids)):
            raise WorkflowError(f"分镜 {shot_index:02d} 的人物配置存在重复项。")
        if len(actor_ids) > 4 or any(value < 1 or value > actor_count for value in actor_ids):
            raise WorkflowError(f"分镜 {shot_index:02d} 的人物编号超出当前角色库范围。")
        parsed[shot_index] = actor_ids
    if set(parsed) != valid_shots:
        raise WorkflowError("必须为每个分镜确认出场角色；无人镜头请选择“无人镜头”。")
    return parsed


def cast_continuity_assignment_map(job: WebJob) -> dict[tuple[int, int], dict[str, Any]]:
    continuity = job.cast_continuity if isinstance(job.cast_continuity, dict) else {}
    result: dict[tuple[int, int], dict[str, Any]] = {}
    for item in continuity.get("assignments") or []:
        if not isinstance(item, dict):
            continue
        try:
            key = (int(item.get("shot_index") or 0), int(item.get("slot") or 0))
            character_id = int(item.get("character_id") or 0)
        except (TypeError, ValueError):
            continue
        if key[0] > 0 and key[1] > 0 and character_id > 0:
            result[key] = dict(item)
    return result


def validate_long_cast_continuity(
    job: WebJob,
    shot_casts: dict[int, list[int]],
    *,
    allow_manual_override: bool = False,
) -> list[str]:
    """Validate automatic C identities, optionally yielding to reviewed per-shot casts."""
    assignments = cast_continuity_assignment_map(job)
    if not assignments:
        if allow_manual_override:
            return ["未建立自动C1/C2身份；已采用人工逐镜人物映射作为最高优先级"]
        raise WorkflowError(
            "尚未完成跨镜人物身份绑定。请先重新运行“分析全部分镜的台词与表演”，"
            "系统会建立全片稳定的 C1/C2 身份后再允许付费生成。"
        )
    character_to_actor: dict[int, int] = {}
    conflicts: list[str] = []
    missing: list[str] = []
    for shot_index, actor_ids in shot_casts.items():
        for slot, actor_id in enumerate(actor_ids, start=1):
            assignment = assignments.get((shot_index, slot))
            if assignment is None:
                missing.append(f"分镜{shot_index:02d}-P{slot}")
                continue
            character_id = int(assignment["character_id"])
            previous = character_to_actor.setdefault(character_id, actor_id)
            if previous != actor_id:
                conflicts.append(
                    f"原片C{character_id}在分镜{shot_index:02d}-P{slot}被选为人物{actor_id}，"
                    f"但同一原片人物此前已绑定人物{previous}"
                )
    if missing:
        message = "自动跨镜身份分析缺少槽位：" + "、".join(missing)
        if not allow_manual_override:
            raise WorkflowError(message + "。请重新分析后再提交。")
        warnings = [message + "；对应槽位改用人工逐镜人物映射"]
    else:
        warnings = []
    if conflicts:
        message = "自动C身份与人工选择不一致：" + "；".join(conflicts)
        if not allow_manual_override:
            raise WorkflowError("跨镜人物身份发生互换，已阻止付费提交：" + "；".join(conflicts) + "。")
        warnings.append(message + "；已以人工逐镜选择为准")
    return warnings


def apply_cast_continuity_to_shot(job: WebJob, shot: dict[str, Any]) -> None:
    assignments = cast_continuity_assignment_map(job)
    shot_index = int(shot.get("index") or 0)
    for item in shot.get("performance", {}).get("performance") or []:
        if not isinstance(item, dict):
            continue
        assignment = assignments.get((shot_index, int(item.get("actor_slot") or 0)))
        if assignment:
            item["character_id"] = int(assignment["character_id"])


def parse_long_shot_position_locks(
    job: WebJob,
    shot_casts: dict[int, list[int]],
) -> dict[int, list[str]]:
    try:
        raw_items = json.loads(request.form.get("shot_casts", "").strip())
    except (TypeError, ValueError) as exc:
        raise WorkflowError("分镜人物位置锁定格式无效，请刷新页面后重试。") from exc
    raw_by_index = {
        int(item.get("index") or 0): item
        for item in raw_items
        if isinstance(item, dict)
    }
    shot_by_index = {int(shot.get("index") or 0): shot for shot in job.shots}
    results: dict[int, list[str]] = {}
    for shot_index, actor_ids in shot_casts.items():
        item = raw_by_index.get(shot_index, {})
        raw_mappings = item.get("actor_mappings")
        ordered = (
            sorted(raw_mappings, key=lambda value: int(value.get("slot") or 0))
            if isinstance(raw_mappings, list)
            else []
        )
        locks: list[str] = []
        for slot_index in range(1, len(actor_ids) + 1):
            value = ""
            if slot_index <= len(ordered) and isinstance(ordered[slot_index - 1], dict):
                value = str(ordered[slot_index - 1].get("position_lock") or "").strip()
            if not value:
                value = long_shot_performance_slot_anchor(shot_by_index[shot_index], slot_index)
            value = " ".join(value.split())
            if len(value) > 220:
                raise WorkflowError(f"分镜 {shot_index:02d} 的人物位置锁定每项最多 220 字。")
            locks.append(value)
        results[shot_index] = locks
    return results


def parse_real_long_manual_identity_override_shots(job: WebJob) -> set[int]:
    """Read per-shot manual identity overrides without changing virtual-project behavior."""
    try:
        raw_items = json.loads(request.form.get("shot_casts", "").strip())
    except (TypeError, ValueError) as exc:
        raise WorkflowError("分镜人物人工覆盖配置无效，请刷新页面后重试。") from exc
    valid_shots = {int(shot.get("index") or 0) for shot in job.shots}
    return {
        int(item.get("index") or 0)
        for item in raw_items
        if isinstance(item, dict)
        and int(item.get("index") or 0) in valid_shots
        and item.get("manual_identity_override") is True
    }


def parse_long_regeneration_request(
    job: WebJob,
    *,
    allow_missing_unforced_outputs: bool = False,
) -> tuple[str, set[int]]:
    mode = request.form.get("regeneration_mode", "normal").strip().lower() or "normal"
    if mode not in {"normal", "all", "selected"}:
        raise WorkflowError("重新生成模式无效，请刷新页面后再试。")
    raw_value = request.form.get("force_shots", "[]").strip() or "[]"
    try:
        raw_indices = json.loads(raw_value)
    except (TypeError, ValueError) as exc:
        raise WorkflowError("重新生成的分镜编号格式无效。") from exc
    if not isinstance(raw_indices, list):
        raise WorkflowError("重新生成的分镜编号必须是列表。")
    try:
        force_indices = {int(value) for value in raw_indices if not isinstance(value, bool)}
    except (TypeError, ValueError) as exc:
        raise WorkflowError("重新生成的分镜编号无效。") from exc
    if len(force_indices) != len(raw_indices):
        raise WorkflowError("重新生成的分镜编号存在重复项或无效值。")
    valid_indices = {int(shot.get("index") or 0) for shot in job.shots}
    skipped_indices = {
        int(shot.get("index") or 0)
        for shot in job.shots
        if is_real_person_long_job(job) and real_long_shot_is_skipped(shot)
    }
    unknown = force_indices - valid_indices
    if unknown:
        raise WorkflowError("找不到要重新生成的分镜：" + ", ".join(f"{value:02d}" for value in sorted(unknown)))
    if mode == "normal" and force_indices:
        raise WorkflowError("普通生成不能指定强制重新生成的分镜。")
    if mode == "all":
        force_indices = valid_indices - skipped_indices
    if mode == "selected" and len(force_indices) != 1:
        raise WorkflowError("单镜重新生成时必须且只能选择一个分镜。")
    if mode == "selected":
        selected_index = next(iter(force_indices))
        if selected_index in skipped_indices:
            raise WorkflowError(
                f"分镜 {selected_index:02d} 已标记为跳过；请先点击“恢复此镜”后再重新生成。"
            )
        selected_shot = next(
            shot for shot in job.shots if int(shot.get("index") or 0) == selected_index
        )
        if (
            not Path(str(selected_shot.get("output_path") or "")).is_file()
            and not is_real_person_long_job(job)
        ):
            raise WorkflowError(f"分镜 {selected_index:02d} 尚无成片，不能执行重新生成。")
        missing_other_outputs = [
            int(shot.get("index") or 0)
            for shot in job.shots
            if int(shot.get("index") or 0) not in force_indices
            and not (
                is_real_person_long_job(job)
                and real_long_shot_is_skipped(shot)
            )
            and not Path(str(shot.get("output_path") or "")).is_file()
        ]
        if missing_other_outputs and not allow_missing_unforced_outputs:
            raise WorkflowError(
                "其他分镜尚无可复用成片，不能只重新生成单镜；缺少："
                + ", ".join(f"{value:02d}" for value in missing_other_outputs)
            )
    return mode, force_indices


def long_white_model_generation_count(shot: dict[str, Any]) -> int:
    """Return the persisted paid-attempt count, including legacy white-model records."""
    try:
        stored = int(shot.get("white_model_total_generation_count") or 0)
    except (TypeError, ValueError):
        stored = 0
    if stored > 0:
        return stored
    white_path = Path(str(shot.get("white_model_path") or ""))
    has_completed_result = bool(shot.get("white_model_task_id")) or white_path.is_file()
    if not has_completed_result:
        return 0
    try:
        automatic_retry_count = max(0, int(shot.get("white_model_retry_count") or 0))
    except (TypeError, ValueError):
        automatic_retry_count = 0
    return automatic_retry_count + 1


def parse_long_white_model_regeneration_request(job: WebJob) -> tuple[str, set[int]]:
    mode = request.form.get("white_regeneration_mode", "normal").strip().lower() or "normal"
    if mode not in {"normal", "selected"}:
        raise WorkflowError("白模重新生成模式无效，请刷新页面后再试。")
    raw_value = request.form.get("force_white_shots", "[]").strip() or "[]"
    try:
        raw_indices = json.loads(raw_value)
    except (TypeError, ValueError) as exc:
        raise WorkflowError("白模重新生成的分镜编号格式无效。") from exc
    if not isinstance(raw_indices, list):
        raise WorkflowError("白模重新生成的分镜编号必须是列表。")
    try:
        force_indices = {int(value) for value in raw_indices if not isinstance(value, bool)}
    except (TypeError, ValueError) as exc:
        raise WorkflowError("白模重新生成的分镜编号无效。") from exc
    if len(force_indices) != len(raw_indices):
        raise WorkflowError("白模重新生成的分镜编号存在重复项或无效值。")
    valid_indices = {int(shot.get("index") or 0) for shot in job.shots}
    unknown = force_indices - valid_indices
    if unknown:
        raise WorkflowError("找不到要重新生成白模的分镜：" + ", ".join(f"{value:02d}" for value in sorted(unknown)))
    if mode == "normal" and force_indices:
        raise WorkflowError("普通白模生成不能指定强制重新生成的分镜。")
    if mode == "selected" and len(force_indices) != 1:
        raise WorkflowError("单镜白模重新生成时必须且只能选择一个分镜。")
    if mode == "selected":
        missing_outputs = [
            int(shot.get("index") or 0)
            for shot in job.shots
            if not Path(str(shot.get("white_model_path") or "")).is_file()
        ]
        if missing_outputs:
            raise WorkflowError(
                "存在尚无可复用白模的分镜，不能只重新生成单镜；缺少："
                + ", ".join(f"{value:02d}" for value in missing_outputs)
            )
    return mode, force_indices


def save_long_actor_references(
    job: WebJob,
    *,
    actor_count: int,
    used_actor_ids: set[int],
    require_real_derivatives: bool = False,
) -> list[dict[str, Any]]:
    if not 1 <= actor_count <= 4:
        raise WorkflowError("项目 6 的统一角色库支持 1–4 位人物。")
    if require_real_derivatives and actor_count > 2:
        raise WorkflowError("真实人物角色库模式当前支持1–2位真人。")
    previous = {int(actor.get("id") or 0): actor for actor in job.actors}
    actors: list[dict[str, Any]] = []
    for index in range(1, actor_count + 1):
        existing = previous.get(index, {})
        role = _clean_long_role(
            request.form.get(f"role_description_{index}", "") or str(existing.get("role") or ""),
            index,
        )
        source_mode = request.form.get(f"person_source_mode_{index}", "").strip()
        person_asset = request.form.get(f"person_asset_{index}", "").strip()
        person_upload = request.files.get(f"person_image_{index}")
        trusted_asset_uri = str(
            existing.get("trusted_asset_uri")
            or (
                existing.get("ark_library_asset_uri")
                if str(existing.get("ark_library_upload_status") or "") == "Active"
                else ""
            )
            or ""
        )
        if person_asset:
            if not person_asset.startswith("asset://"):
                raise WorkflowError(f"人物 {index} 的授权素材 ID 必须以 asset:// 开头。")
            person_source = person_asset
            trusted_asset_uri = person_asset
        elif person_upload is not None and person_upload.filename:
            person_source = str(save_upload(person_upload, job.run_dir, f"long_person_{index}"))
            trusted_asset_uri = ""
        else:
            existing_person = str(existing.get("person_source") or "")
            if source_mode in {"local_dual", "local_library"} and existing_person.startswith("asset://"):
                person_source = ""
                trusted_asset_uri = ""
            elif existing_person.startswith("asset://") or (existing_person and Path(existing_person).is_file()):
                person_source = existing_person
            else:
                person_source = ""

        original_person_source = str(existing.get("original_person_source") or "")
        local_person_source = str(existing.get("local_person_source") or "")
        masked_person_source = str(existing.get("masked_person_source") or "")
        sketch_person_source = str(existing.get("sketch_person_source") or "")
        reference_layout = str(existing.get("reference_layout") or "")
        if require_real_derivatives:
            # Real-person final generation accepts only an Active Ark character
            # asset. Local uploads are staged by the explicit library-upload step.
            if trusted_asset_uri:
                original_person_source = trusted_asset_uri
                masked_person_source = ""
                sketch_person_source = ""
                reference_layout = "trusted_asset"
            elif person_upload is not None and person_upload.filename:
                original_person_source = person_source
                masked_person_source = ""
                sketch_person_source = ""
                reference_layout = ""
            elif source_mode in {"local_dual", "local_library"} and original_person_source.startswith("asset://"):
                original_person_source = ""
            elif not original_person_source:
                original_person_source = person_source
            if person_upload is not None and person_upload.filename and not person_source.startswith("asset://"):
                local_person_source = person_source
            elif not local_person_source and original_person_source and not original_person_source.startswith("asset://"):
                local_person_source = original_person_source

        clothing_upload = request.files.get(f"clothing_image_{index}")
        if clothing_upload is not None and clothing_upload.filename:
            clothing_source = str(save_upload(clothing_upload, job.run_dir, f"long_clothing_{index}"))
        else:
            existing_clothing = str(existing.get("clothing_source") or "")
            clothing_source = existing_clothing if existing_clothing and Path(existing_clothing).is_file() else ""

        if index in used_actor_ids:
            if not person_source:
                raise WorkflowError(f"分镜中使用了人物 {index}，请上传其人物图片或填写授权素材 ID。")
            if not clothing_source:
                raise WorkflowError(f"分镜中使用了人物 {index}，请上传其服装参考图。")
            if require_real_derivatives:
                if trusted_asset_uri:
                    person_source = trusted_asset_uri
                else:
                    raise WorkflowError(
                        f"人物 {index} 尚未上传并通过火山角色库审核；"
                        "请先点击“上传并绑定火山角色库”。"
                    )
        actors.append(
            {
                "id": index,
                "role": role,
                "person_source": person_source,
                "clothing_source": clothing_source,
                "original_person_source": original_person_source,
                "masked_person_source": masked_person_source,
                "sketch_person_source": sketch_person_source,
                "reference_layout": reference_layout,
                "trusted_asset_uri": trusted_asset_uri,
                **(
                    {"local_person_source": local_person_source}
                    if require_real_derivatives
                    else {}
                ),
            }
        )
    job.actors = actors
    first = next((actor for actor in actors if actor["id"] in used_actor_ids), None)
    if first:
        first_person = str(first["person_source"])
        job.person_path = Path(first_person) if first_person and not first_person.startswith("asset://") else None
        first_clothing = str(first["clothing_source"])
        job.clothing_path = Path(first_clothing) if first_clothing else None
    return actors


def save_real_person_actor_uploads(job: WebJob, *, actor_count: int) -> list[dict[str, Any]]:
    if not 1 <= actor_count <= 2:
        raise WorkflowError("真实人物角色库模式当前支持 1–2 位人物。")
    previous = {int(actor.get("id") or 0): actor for actor in job.actors}
    actors: list[dict[str, Any]] = []
    for index in range(1, actor_count + 1):
        existing = previous.get(index, {})
        role = _clean_long_role(
            request.form.get(f"role_description_{index}", "") or str(existing.get("role") or ""),
            index,
        )
        source_mode = request.form.get(f"person_source_mode_{index}", "").strip()
        person_asset = request.form.get(f"person_asset_{index}", "").strip()
        upload = request.files.get(f"person_image_{index}")
        new_local_upload = bool(upload is not None and upload.filename)
        ark_upload_requested = not bool(person_asset)
        ark_group_id = request.form.get(f"ark_group_id_{index}", "").strip()
        ark_asset_name = " ".join(
            request.form.get(f"ark_asset_name_{index}", "").split()
        )[:64]
        if ark_upload_requested:
            if not re.fullmatch(r"group-[A-Za-z0-9_-]{6,120}", ark_group_id):
                raise WorkflowError(f"人物 {index} 请选择有效的火山角色组。")
            if not ark_asset_name:
                raise WorkflowError(f"请填写人物 {index} 的火山角色素材名称。")
        if person_asset:
            if not re.fullmatch(r"asset://asset-[A-Za-z0-9_-]{6,120}", person_asset):
                raise WorkflowError(f"人物 {index} 的火山人物素材 ID 无效。")
            original_source = person_asset
            reference_layout = "trusted_asset"
            trusted_asset_uri = person_asset
            local_person_source = str(existing.get("local_person_source") or "")
        elif new_local_upload:
            original = save_upload(upload, job.run_dir, f"real_person_{index}_original")
            original_source = str(original)
            local_person_source = str(original)
            reference_layout = ""
            trusted_asset_uri = ""
        else:
            local_person_source = str(existing.get("local_person_source") or "")
            if not local_person_source:
                legacy_source = str(existing.get("original_person_source") or existing.get("person_source") or "")
                if legacy_source and not legacy_source.startswith("asset://"):
                    local_person_source = legacy_source
            original_source = local_person_source
            reference_layout = str(existing.get("reference_layout") or "")
            trusted_asset_uri = str(existing.get("trusted_asset_uri") or "")
            if source_mode == "ark_asset" and trusted_asset_uri.startswith("asset://"):
                original_source = trusted_asset_uri
            elif not local_person_source:
                original_source = ""
                trusted_asset_uri = ""
        if not original_source or (
            not original_source.startswith("asset://") and not Path(original_source).is_file()
        ):
            raise WorkflowError(f"请上传人物 {index} 的已授权真人参考图片。")
        clothing_upload = request.files.get(f"clothing_image_{index}")
        if clothing_upload is not None and clothing_upload.filename:
            clothing_source = str(save_upload(clothing_upload, job.run_dir, f"real_clothing_{index}"))
        else:
            clothing_source = str(existing.get("clothing_source") or "")
        previous_ark_asset_uri = str(existing.get("ark_library_asset_uri") or "")
        previous_ark_status = str(existing.get("ark_library_upload_status") or "")
        previous_ark_fingerprint = str(existing.get("ark_library_source_fingerprint") or "")
        current_fingerprint = (
            _reference_fingerprint(local_person_source)
            if local_person_source and Path(local_person_source).is_file()
            else ""
        )
        same_library_source = bool(
            current_fingerprint
            and previous_ark_fingerprint
            and current_fingerprint == previous_ark_fingerprint
        )
        actors.append(
            {
                "id": index,
                "role": role,
                "person_source": original_source,
                "original_person_source": original_source,
                "local_person_source": local_person_source,
                "masked_person_source": "",
                "sketch_person_source": "",
                "reference_layout": reference_layout,
                "trusted_asset_uri": trusted_asset_uri,
                "clothing_source": clothing_source,
                "ark_library_upload_requested": ark_upload_requested,
                "ark_library_group_id": ark_group_id or str(
                    existing.get("ark_library_group_id") or ""
                ),
                "ark_library_asset_name": ark_asset_name or str(
                    existing.get("ark_library_asset_name") or ""
                ),
                "ark_library_asset_uri": previous_ark_asset_uri if same_library_source else (
                    person_asset if person_asset else ""
                ),
                "ark_library_upload_status": (
                    previous_ark_status if same_library_source else (
                        "Active" if person_asset else "等待提交"
                    )
                ),
                "ark_library_upload_error": "",
                "ark_library_source_fingerprint": (
                    previous_ark_fingerprint if same_library_source else ""
                ),
            }
        )
    job.actors = actors
    return actors


@dataclass
class ArkCharacterUploadSource:
    """Public, short-lived source used while Ark ingests one character image."""

    url: str
    channel: str
    tos: TosMediaStore | None = None
    object_key: str = ""
    file_server: TemporaryFileServer | None = None
    tunnel: TemporaryPublicTunnel | None = None
    temporary_store: TempFileMediaStore | None = None
    temporary_file_id: str = ""

    def close(self) -> None:
        if self.tos is not None and self.object_key:
            try:
                self.tos.delete(self.object_key)
            except Exception:
                pass
            self.object_key = ""
        if self.tunnel is not None:
            self.tunnel.close()
            self.tunnel = None
        if self.file_server is not None:
            self.file_server.close()
            self.file_server = None
        if self.temporary_store is not None and self.temporary_file_id:
            try:
                self.temporary_store.delete(self.temporary_file_id)
            except Exception:
                pass
            self.temporary_file_id = ""


@dataclass
class SeedanceVideoReferenceSource:
    """Task-scoped stable reference video source used by 衣装智换."""

    url: str
    channel: str
    tos: TosMediaStore | None = None
    object_key: str = ""
    video_server: TemporaryVideoServer | None = None
    tunnel: TemporaryPublicTunnel | None = None
    temporary_store: TempFileMediaStore | None = None
    temporary_file_id: str = ""

    def close(self, *, delete_remote: bool = True) -> None:
        if delete_remote and self.tos is not None and self.object_key:
            try:
                self.tos.delete(self.object_key)
            except Exception:
                pass
            self.object_key = ""
        if self.tunnel is not None:
            self.tunnel.close()
            self.tunnel = None
        if self.video_server is not None:
            self.video_server.close()
            self.video_server = None
        if delete_remote and self.temporary_store is not None and self.temporary_file_id:
            try:
                self.temporary_store.delete(self.temporary_file_id)
            except Exception:
                pass
            self.temporary_file_id = ""


def prepare_seedance_stable_video_reference(
    source: Path,
    *,
    on_log: Any | None = None,
) -> SeedanceVideoReferenceSource:
    """Prefer configured TOS; otherwise expose the MP4 through the project tunnel."""

    tos_error = ""
    if TosMediaStore.configured():
        try:
            tos = TosMediaStore()
            uploaded = tos.upload_video(source)
            if on_log:
                on_log("参考视频已上传私有 TOS，正在准备 Seedance 任务。")
            return SeedanceVideoReferenceSource(
                url=uploaded.signed_url,
                channel="tos",
                tos=tos,
                object_key=uploaded.object_key,
            )
        except Exception as exc:
            tos_error = str(exc)
            if on_log:
                on_log(f"TOS 暂时不可用，自动切换项目一次性加密视频通道：{exc}")
    elif on_log:
        on_log("TOS 配置不完整，衣装智换将使用项目一次性加密视频通道。")

    video_server = TemporaryVideoServer(source, uuid.uuid4().hex)
    tunnel: TemporaryPublicTunnel | None = None
    try:
        video_server.start()
        tunnel = TemporaryPublicTunnel(video_server.local_origin, on_log=on_log)
        public_origin = tunnel.start()
        public_url = f"{public_origin}{video_server.route_path}"
        tunnel.wait_until_reachable(public_url)
        if on_log:
            on_log("项目一次性加密视频地址已就绪；不会使用 tempfile.org。")
        return SeedanceVideoReferenceSource(
            url=public_url,
            channel="project_tunnel",
            video_server=video_server,
            tunnel=tunnel,
        )
    except Exception as exc:
        if tunnel is not None:
            tunnel.close()
        video_server.close()
        tunnel_error = str(exc)
        if on_log:
            on_log(f"项目一次性加密视频通道不可用，正在切换限时临时MP4直链：{exc}")

    temporary_store = TempFileMediaStore()
    try:
        uploaded = temporary_store.upload_video(source, expires_hours=1)
        if on_log:
            on_log("限时临时MP4直链已生成并通过公网检查；将在任务结束后删除。")
        return SeedanceVideoReferenceSource(
            url=uploaded.signed_url,
            channel="temporary",
            temporary_store=temporary_store,
            temporary_file_id=uploaded.object_key,
        )
    except Exception as temporary_exc:
        detail = f"；TOS 原因：{tos_error}" if tos_error else "；TOS 未配置或服务未开通"
        raise WorkflowError(
            "衣装智换三条视频通道均不可用，付费任务尚未提交："
            f"项目通道：{tunnel_error}；临时MP4通道：{temporary_exc}{detail}"
        ) from temporary_exc


def prepare_ark_character_upload_source(
    source: Path,
    *,
    on_log: Any | None = None,
) -> ArkCharacterUploadSource:
    """Prefer TOS, then project tunnel, then a short-lived public image URL."""

    tos_error = ""
    if TosMediaStore.configured():
        try:
            tos = TosMediaStore()
            uploaded = tos.upload_file(source, expires=86_400)
            if on_log:
                on_log("人物图片已上传私有 TOS，正在提交火山角色库。")
            return ArkCharacterUploadSource(
                url=uploaded.signed_url,
                channel="tos",
                tos=tos,
                object_key=uploaded.object_key,
            )
        except Exception as exc:
            tos_error = str(exc)
            if on_log:
                on_log(f"TOS 暂时不可用，自动切换项目一次性加密通道：{exc}")

    file_server = TemporaryFileServer(
        source,
        uuid.uuid4().hex,
        public_name=source.name,
    )
    tunnel: TemporaryPublicTunnel | None = None
    try:
        file_server.start()
        tunnel = TemporaryPublicTunnel(file_server.local_origin, on_log=on_log)
        public_origin = tunnel.start()
        public_url = f"{public_origin}{file_server.route_path}"
        tunnel.wait_until_reachable(public_url)
        if on_log:
            on_log("项目一次性加密人物图片通道已就绪。")
        return ArkCharacterUploadSource(
            url=public_url,
            channel="project_tunnel",
            file_server=file_server,
            tunnel=tunnel,
        )
    except Exception as exc:
        if tunnel is not None:
            tunnel.close()
        file_server.close()
        tunnel_error = str(exc)
        if on_log:
            on_log(f"项目人物图片通道不可用，正在切换限时临时图片直链：{exc}")

    temporary_store = TempFileMediaStore()
    try:
        uploaded = temporary_store.upload_file(source, expires_hours=6, attempts=5)
        if on_log:
            on_log("限时临时人物图片直链已生成并通过公网检查；审核结束后自动删除。")
        return ArkCharacterUploadSource(
            url=uploaded.signed_url,
            channel="temporary_image",
            temporary_store=temporary_store,
            temporary_file_id=uploaded.object_key,
        )
    except Exception as temporary_exc:
        tos_detail = f"；TOS 原因：{tos_error}" if tos_error else "；TOS_BUCKET 未配置"
        raise WorkflowError(
            "人物图片三条上传通道均不可用，角色库审核尚未提交："
            f"项目通道：{tunnel_error}；临时图片通道：{temporary_exc}{tos_detail}"
        ) from temporary_exc


def submit_real_actor_to_character_library(job: WebJob, actor: dict[str, Any]) -> str:
    """Upload one authorized person image and bind the resulting Active Ark asset."""
    if not bool(actor.get("ark_library_upload_requested")):
        uri = str(actor.get("trusted_asset_uri") or actor.get("ark_library_asset_uri") or "")
        if uri.startswith("asset://"):
            return uri
        raise WorkflowError("人物没有可用的火山角色库素材。")
    index = int(actor.get("id") or 0)
    source = Path(str(actor.get("local_person_source") or actor.get("original_person_source") or ""))
    upload_source: ArkCharacterUploadSource | None = None
    try:
        if not source.is_file():
            raise WorkflowError(f"人物 {index} 的原始图片不存在。")
        if source.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise WorkflowError("火山角色库只接受 JPG、PNG 或 WEBP。")
        if source.stat().st_size > 30 * 1024 * 1024:
            raise WorkflowError("火山角色库图片不能超过 30 MB。")
        if not ark_assets_configured():
            raise WorkflowError("尚未配置火山素材库 AK/SK。")
        group_id = str(actor.get("ark_library_group_id") or "").strip()
        name = " ".join(str(actor.get("ark_library_asset_name") or "").split())[:64]
        if not re.fullmatch(r"group-[A-Za-z0-9_-]{6,120}", group_id):
            raise WorkflowError("所选火山角色组无效。")
        if not name:
            raise WorkflowError("火山角色素材名称为空。")
        fingerprint = _reference_fingerprint(str(source))
        existing_uri = str(actor.get("ark_library_asset_uri") or "")
        reuse_existing = (
            fingerprint
            and fingerprint == str(actor.get("ark_library_source_fingerprint") or "")
            and re.fullmatch(r"asset://asset-[A-Za-z0-9_-]{6,120}", existing_uri)
        )
        if reuse_existing:
            asset_id = existing_uri.removeprefix("asset://")
            job.log(f"人物 {index} 与上次入库图片相同，复用 {existing_uri} 并检查审核状态。")
        else:
            actor["ark_library_upload_status"] = "正在提交"
            actor["ark_library_upload_error"] = ""
            _persist_long_job(job)
            upload_source = prepare_ark_character_upload_source(source, on_log=job.log)
            asset_id = ark_assets_client().create_asset(
                group_id=group_id,
                url=upload_source.url,
                name=name,
                asset_type="Image",
            )
        actor["ark_library_asset_uri"] = f"asset://{asset_id}"
        actor["ark_library_source_fingerprint"] = fingerprint
        actor["ark_library_upload_status"] = "Processing"
        actor["ark_library_upload_error"] = ""
        _persist_long_job(job)
        if not reuse_existing:
            job.log(f"人物 {index} 已提交火山角色库：{name}（asset://{asset_id}），正在等待审核。")
        client = ark_assets_client()
        for _attempt in range(60):
            raw = client.get_asset(asset_id)
            status = str(_ark_asset_value(raw, "Status", "status") or "Processing").strip()
            actor["ark_library_upload_status"] = status
            _persist_long_job(job)
            if status == "Active":
                uri = f"asset://{asset_id}"
                actor["person_source"] = uri
                actor["original_person_source"] = uri
                actor["trusted_asset_uri"] = uri
                actor["reference_layout"] = "trusted_asset"
                actor["masked_person_source"] = ""
                actor["sketch_person_source"] = ""
                actor["ark_library_upload_requested"] = False
                actor["ark_library_upload_error"] = ""
                job.log(f"人物 {index} 已通过火山角色库审核并绑定：{uri}。")
                _persist_long_job(job)
                return uri
            if status == "Failed":
                raise WorkflowError(f"人物 {index} 的火山角色素材审核失败，请更换图片后重试。")
            time.sleep(5)
        raise WorkflowError(
            f"人物 {index} 已提交火山角色库但仍在审核中；系统已保留Asset ID，稍后再次点击即可继续查询，不会重复上传。"
        )
    except Exception as exc:
        if str(actor.get("ark_library_upload_status") or "") != "Processing":
            actor["ark_library_upload_status"] = "上传失败"
        actor["ark_library_upload_error"] = str(exc)
        job.log(f"人物 {index} 无法完成火山角色库绑定：{exc}")
        _persist_long_job(job)
        raise
    finally:
        if upload_source is not None:
            upload_source.close()


def run_real_person_reference_preparation(
    job: WebJob,
) -> None:
    try:
        total = len(job.actors)
        for ordinal, actor in enumerate(job.actors, start=1):
            index = int(actor.get("id") or ordinal)
            trusted_asset_uri = str(
                actor.get("trusted_asset_uri") or actor.get("original_person_source") or ""
            )
            if trusted_asset_uri.startswith("asset://"):
                actor["person_source"] = trusted_asset_uri
                actor["original_person_source"] = trusted_asset_uri
                actor["trusted_asset_uri"] = trusted_asset_uri
                actor["masked_person_source"] = ""
                actor["sketch_person_source"] = ""
                actor["ark_library_upload_status"] = "Active"
                actor["reference_layout"] = "trusted_asset"
                job.update(
                    status="running",
                    stage=f"已确认人物 {index}/{total} 的火山授权角色",
                    progress=int(4 + 92 * ordinal / max(total, 1)),
                )
                job.log(f"人物 {index} 已绑定火山角色库 {trusted_asset_uri}。")
                _persist_long_job(job)
                continue
            job.update(
                status="running",
                stage=f"正在上传并审核人物 {index}/{total} 的火山角色素材",
                progress=int(4 + 92 * (ordinal - 1) / max(total, 1)),
            )
            submit_real_actor_to_character_library(job, actor)
        job.person_path = None
        job.update(status="succeeded", stage="人物已全部上传并绑定火山角色库", progress=100, error="")
        _persist_long_job(job)
    except Exception as exc:
        job_error(job, exc)
        _persist_long_job(job)


def save_single_real_actor_library_upload(job: WebJob, actor_index: int) -> dict[str, Any]:
    """Save one actor card without requiring the other configured actors."""
    if actor_index not in {1, 2}:
        raise WorkflowError("真实人物角色库当前只支持人物 1–2。")
    previous = {int(actor.get("id") or 0): actor for actor in job.actors}
    existing = dict(previous.get(actor_index, {}))
    role = _clean_long_role(
        request.form.get("role_description", "")
        or str(existing.get("role") or ""),
        actor_index,
    )
    group_id = request.form.get("ark_group_id", "").strip()
    asset_name = " ".join(request.form.get("ark_asset_name", "").split())[:64]
    if not re.fullmatch(r"group-[A-Za-z0-9_-]{6,120}", group_id):
        raise WorkflowError(f"人物 {actor_index} 请选择有效的火山角色组。")
    if not asset_name:
        raise WorkflowError(f"请填写人物 {actor_index} 的角色素材名称。")
    upload = request.files.get("person_image")
    if upload is not None and upload.filename:
        local_source = str(
            save_upload(upload, job.run_dir, f"real_person_{actor_index}_original")
        )
    else:
        local_source = str(existing.get("local_person_source") or "")
        if not local_source:
            legacy = str(existing.get("original_person_source") or existing.get("person_source") or "")
            if legacy and not legacy.startswith("asset://"):
                local_source = legacy
    if not local_source or not Path(local_source).is_file():
        raise WorkflowError(f"请先为人物 {actor_index} 选择已授权的真人图片。")
    clothing_upload = request.files.get("clothing_image")
    if clothing_upload is not None and clothing_upload.filename:
        clothing_source = str(
            save_upload(clothing_upload, job.run_dir, f"real_clothing_{actor_index}")
        )
    else:
        clothing_source = str(existing.get("clothing_source") or "")
    fingerprint = _reference_fingerprint(local_source)
    previous_fingerprint = str(existing.get("ark_library_source_fingerprint") or "")
    same_source = bool(fingerprint and fingerprint == previous_fingerprint)
    actor = {
        **existing,
        "id": actor_index,
        "role": role,
        "person_source": local_source,
        "original_person_source": local_source,
        "local_person_source": local_source,
        "masked_person_source": "",
        "sketch_person_source": "",
        "reference_layout": "",
        "trusted_asset_uri": "",
        "clothing_source": clothing_source,
        "ark_library_upload_requested": True,
        "ark_library_group_id": group_id,
        "ark_library_asset_name": asset_name,
        "ark_library_asset_uri": str(existing.get("ark_library_asset_uri") or "") if same_source else "",
        "ark_library_upload_status": str(existing.get("ark_library_upload_status") or "等待提交") if same_source else "等待提交",
        "ark_library_upload_error": "",
        "ark_library_source_fingerprint": previous_fingerprint if same_source else "",
    }
    previous[actor_index] = actor
    job.actors = [previous[index] for index in sorted(previous)]
    return actor


def run_single_real_actor_library_upload(job: WebJob, actor_index: int) -> None:
    try:
        actor = next(
            (item for item in job.actors if int(item.get("id") or 0) == actor_index),
            None,
        )
        if actor is None:
            raise WorkflowError(f"找不到人物 {actor_index} 的配置。")
        job.update(
            status="running",
            stage=f"正在上传并审核人物 {actor_index} 的火山角色素材",
            progress=8,
            error="",
        )
        submit_real_actor_to_character_library(job, actor)
        job.update(
            status="succeeded",
            stage=f"人物 {actor_index} 已上传并绑定火山角色库",
            progress=100,
            error="",
        )
        _persist_long_job(job)
    except Exception as exc:
        job_error(job, exc)
        _persist_long_job(job)


def save_long_scene_library(
    job: WebJob,
    *,
    require_assignments: bool = True,
) -> tuple[dict[int, str], dict[int, dict[str, Any]]]:
    """Save a reusable multi-image scene library.

    Per-shot generation requires one scene assignment for every shot. Whole-video
    generation selects one original scene image directly and therefore stores the
    same library without forcing redundant per-shot assignments.
    """
    valid_suffixes = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    try:
        raw_groups = json.loads(request.form.get("scene_library", ""))
        raw_assignments = json.loads(request.form.get("scene_assignments", ""))
    except (TypeError, ValueError) as exc:
        raise WorkflowError("场景库或分镜场景映射格式无效，请刷新页面后重新选择。") from exc
    if not isinstance(raw_groups, list) or not raw_groups:
        raise WorkflowError("请至少建立一个新场景组，并上传场景参考图。")
    if not isinstance(raw_assignments, list):
        raise WorkflowError("分镜场景映射必须是列表。")

    existing_groups = {
        str(group.get("id") or ""): group
        for group in job.scene_groups
        if isinstance(group, dict)
    }
    saved_groups: list[dict[str, Any]] = []
    groups_by_id: dict[str, dict[str, Any]] = {}
    total_images = 0
    for position, raw_group in enumerate(raw_groups, start=1):
        if not isinstance(raw_group, dict):
            raise WorkflowError("场景库中存在无效记录。")
        group_id = str(raw_group.get("id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", group_id) or group_id in groups_by_id:
            raise WorkflowError(f"场景组 {position} 的编号无效或重复。")
        name = " ".join(str(raw_group.get("name") or f"场景 {position}").split())[:60]
        description = " ".join(str(raw_group.get("description") or "").split())[:240]
        try:
            image_count = int(raw_group.get("image_count") or 0)
        except (TypeError, ValueError) as exc:
            raise WorkflowError(f"场景组“{name}”的图片数量无效。") from exc
        if not 1 <= image_count <= 6:
            raise WorkflowError(f"场景组“{name}”需要上传 1–6 张场景参考图。")
        total_images += image_count
        if total_images > 30:
            raise WorkflowError("项目 6 的场景库最多保存 30 张参考图。")

        existing_images = list(existing_groups.get(group_id, {}).get("images") or [])
        images: list[str] = []
        for image_index in range(1, image_count + 1):
            upload = request.files.get(f"scene_group_{group_id}_{image_index}")
            if upload is not None and upload.filename:
                path = save_upload(upload, job.run_dir, f"long_scene_{group_id}_{image_index}")
                if path.suffix.lower() not in valid_suffixes:
                    raise WorkflowError(f"场景组“{name}”第 {image_index} 张必须是 JPG、PNG、WEBP 或 BMP 图片。")
            elif image_index <= len(existing_images) and Path(str(existing_images[image_index - 1])).is_file():
                path = Path(str(existing_images[image_index - 1]))
            else:
                raise WorkflowError(f"场景组“{name}”缺少第 {image_index} 张参考图，请重新选择图片。")
            images.append(str(path))
        group = {"id": group_id, "name": name, "description": description, "images": images}
        saved_groups.append(group)
        groups_by_id[group_id] = group

    valid_shots = {int(shot.get("index") or 0) for shot in job.shots}
    assignments: dict[int, dict[str, Any]] = {}
    results: dict[int, str] = {}
    for raw_assignment in raw_assignments:
        if not isinstance(raw_assignment, dict):
            raise WorkflowError("分镜场景映射中存在无效记录。")
        try:
            shot_index = int(raw_assignment.get("index"))
            image_index = int(raw_assignment.get("image_index"))
        except (TypeError, ValueError) as exc:
            raise WorkflowError("分镜场景映射中的分镜编号或图片编号无效。") from exc
        group_id = str(raw_assignment.get("group_id") or "").strip()
        if shot_index not in valid_shots:
            raise WorkflowError(f"找不到分镜 {shot_index:02d}。")
        group = groups_by_id.get(group_id)
        if group is None or not 1 <= image_index <= len(group["images"]):
            if not require_assignments:
                continue
            raise WorkflowError(f"分镜 {shot_index:02d} 没有匹配到有效的新场景图片。")
        assignments[shot_index] = {
            "group_id": group_id,
            "group_name": group["name"],
            "image_index": image_index,
        }
        results[shot_index] = str(group["images"][image_index - 1])
    if require_assignments and set(assignments) != valid_shots:
        missing = sorted(valid_shots - set(assignments))
        raise WorkflowError(f"所有分镜都必须匹配新场景；尚未匹配：{', '.join(f'{value:02d}' for value in missing)}。")
    job.scene_groups = saved_groups
    return results, assignments


def long_shot_performance_slot_anchor(shot: dict[str, Any], slot: int) -> str:
    if shot.get("position_binding_manual"):
        for mapping in shot.get("actor_mappings") or []:
            if isinstance(mapping, dict) and int(mapping.get("slot") or 0) == int(slot):
                locked = str(mapping.get("source_position") or "").strip()
                if locked:
                    return sanitize_motion_evidence(locked)[:220]
    performance = shot.get("performance")
    if isinstance(performance, dict):
        semantic_slots = {
            int(item.get("actor_slot") or 0)
            for item in performance.get("performance") or []
            if isinstance(item, dict) and int(item.get("actor_slot") or 0) > 0
        }
        detector_count = long_shot_stable_detected_people_count(shot)
        items = [
            item
            for item in performance.get("performance") or []
            if (
                len(semantic_slots) >= detector_count
                and isinstance(item, dict)
                and int(item.get("actor_slot") or 0) == int(slot)
            )
        ]
        if items:
            evidence = "；".join(
                str(item.get("visible_evidence") or "").strip()
                for item in items[:2]
                if str(item.get("visible_evidence") or "").strip()
            )
            intent = "；".join(
                str(item.get("core_intent") or "").strip()
                for item in items[:2]
                if str(item.get("core_intent") or "").strip()
            )
            anchor = evidence or intent
            if anchor:
                return sanitize_motion_evidence(anchor)[:220]
    person_slots = list(shot.get("person_slots") or [])
    if 0 < slot <= len(person_slots) and isinstance(person_slots[slot - 1], dict):
        position = str(person_slots[slot - 1].get("position") or "").strip()
        if position:
            return position
    return f"表演槽位 P{slot}"


def long_shot_stable_detected_people_count(shot: dict[str, Any]) -> int:
    """Ignore one-frame detector spikes while retaining people seen in multiple samples."""
    counts = [
        max(0, min(4, int(value)))
        for value in shot.get("sample_actor_counts") or []
        if isinstance(value, (int, float))
    ]
    if counts:
        repeated = sorted({value for value in counts if counts.count(value) >= 2})
        if repeated:
            return repeated[-1]
        return sorted(counts)[len(counts) // 2]
    return max(
        int(shot.get("suggested_actor_count") or 0),
        len(shot.get("person_slots") or []),
    )


def _sample_face_scale_profile(
    video_path: Path,
    *,
    positions: tuple[float, ...] = (0.12, 0.5, 0.88),
    robust_primary: bool = False,
) -> dict[str, Any]:
    detector = YuNetFaceDetector(ensure_face_model(), score_threshold=0.45)
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise WorkflowError(f"无法读取构图验收视频：{video_path.name}")
    frame_count = max(1, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 1))
    frames: list[dict[str, Any]] = []
    try:
        for position in positions:
            capture.set(cv2.CAP_PROP_POS_FRAMES, min(frame_count - 1, int(frame_count * position)))
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            height, width = frame.shape[:2]
            boxes = detector.detect(frame)
            raw_faces = [
                {
                    "center_x": round((x + face_width / 2) / width, 4),
                    "center_y": round((y + face_height / 2) / height, 4),
                    "width": round(face_width / width, 4),
                    "height": round(face_height / height, 4),
                    "area": round((face_width * face_height) / (width * height), 5),
                }
                for x, y, face_width, face_height in boxes
            ]
            if robust_primary:
                largest_area = max((float(face["area"]) for face in raw_faces), default=0.0)
                # Product labels and background textures occasionally trigger tiny
                # YuNet boxes. They distorted real-person QA by up to 30x.
                faces = [
                    face for face in raw_faces
                    if largest_area <= 0 or float(face["area"]) >= largest_area * 0.12
                ]
            else:
                faces = raw_faces
            faces.sort(key=lambda face: float(face["center_x"]))
            frames.append(
                {
                    "position": position,
                    "faces": faces,
                }
            )
    finally:
        capture.release()
    primary_faces = [
        max(frame["faces"], key=lambda face: float(face["area"]))
        for frame in frames
        if frame["faces"]
    ]
    measured_faces = primary_faces if robust_primary else [
        face for frame in frames for face in frame["faces"]
    ]
    return {
        "frames": frames,
        "median_face_height": round(float(np.median([face["height"] for face in measured_faces])), 4) if measured_faces else 0.0,
        "median_face_area": round(float(np.median([face["area"] for face in measured_faces])), 5) if measured_faces else 0.0,
        "median_face_count": int(round(float(np.median([len(frame["faces"]) for frame in frames])))) if frames else 0,
        "primary_center_x": round(float(np.median([face["center_x"] for face in primary_faces])), 4) if primary_faces else 0.0,
        "primary_center_y": round(float(np.median([face["center_y"] for face in primary_faces])), 4) if primary_faces else 0.0,
    }


def _sample_person_layout_profile(
    video_path: Path,
    *,
    positions: tuple[float, ...] = (0.12, 0.5, 0.88),
) -> dict[str, Any]:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        return {"frames": [], "median_count": 0, "max_count": 0, "median_largest_area": 0.0}
    frame_count = max(1, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 1))
    frames: list[dict[str, Any]] = []
    try:
        for position in positions:
            capture.set(cv2.CAP_PROP_POS_FRAMES, min(frame_count - 1, int(frame_count * position)))
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            height, width = frame.shape[:2]
            detections = detect_people_boxes(frame, confidence_threshold=0.28, max_people=4)
            people = [
                {
                    "center_x": round((box[0] + box[2]) / (2 * width), 4),
                    "center_y": round((box[1] + box[3]) / (2 * height), 4),
                    "width": round((box[2] - box[0]) / width, 4),
                    "height": round((box[3] - box[1]) / height, 4),
                    "area": round(((box[2] - box[0]) * (box[3] - box[1])) / (width * height), 5),
                }
                for box, _confidence in detections
            ]
            frames.append({"position": position, "people": people})
    finally:
        capture.release()
    counts = [len(frame["people"]) for frame in frames]
    largest_areas = [
        max(float(person["area"]) for person in frame["people"])
        for frame in frames
        if frame["people"]
    ]
    return {
        "frames": frames,
        "median_count": int(round(float(np.median(counts)))) if counts else 0,
        "max_count": max(counts, default=0),
        "median_largest_area": round(float(np.median(largest_areas)), 5) if largest_areas else 0.0,
    }


def _sample_motion_profile(video_path: Path) -> float:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        return 0.0
    frame_count = max(2, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 2))
    frames: list[np.ndarray] = []
    try:
        for position in np.linspace(0.05, 0.95, 8):
            capture.set(cv2.CAP_PROP_POS_FRAMES, min(frame_count - 1, int(frame_count * position)))
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            height, width = frame.shape[:2]
            scale = 320.0 / max(height, width)
            resized = cv2.resize(frame, (max(1, int(width * scale)), max(1, int(height * scale))))
            frames.append(cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY))
    finally:
        capture.release()
    magnitudes: list[float] = []
    for previous, current in zip(frames, frames[1:], strict=False):
        flow = cv2.calcOpticalFlowFarneback(previous, current, None, 0.5, 3, 21, 3, 5, 1.2, 0)
        magnitudes.append(float(np.median(np.linalg.norm(flow, axis=2))) / math.hypot(*previous.shape))
    return round(float(np.median(magnitudes)), 6) if magnitudes else 0.0


def _sample_global_motion_vector(video_path: Path) -> dict[str, float]:
    """Estimate global camera-flow direction without treating it as an exact tracker."""
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        return {"x": 0.0, "y": 0.0, "magnitude": 0.0}
    frame_count = max(2, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 2))
    frames: list[np.ndarray] = []
    try:
        for position in np.linspace(0.08, 0.92, 7):
            capture.set(cv2.CAP_PROP_POS_FRAMES, min(frame_count - 1, int(frame_count * position)))
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            height, width = frame.shape[:2]
            scale = 320.0 / max(height, width)
            resized = cv2.resize(frame, (max(1, int(width * scale)), max(1, int(height * scale))))
            frames.append(cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY))
    finally:
        capture.release()
    vectors: list[tuple[float, float]] = []
    for previous, current in zip(frames, frames[1:], strict=False):
        flow = cv2.calcOpticalFlowFarneback(previous, current, None, 0.5, 3, 21, 3, 5, 1.2, 0)
        vectors.append((float(np.median(flow[..., 0])), float(np.median(flow[..., 1]))))
    if not vectors:
        return {"x": 0.0, "y": 0.0, "magnitude": 0.0}
    x = float(np.median([value[0] for value in vectors]))
    y = float(np.median([value[1] for value in vectors]))
    return {"x": round(x, 4), "y": round(y, 4), "magnitude": round(math.hypot(x, y), 4)}


def _text_like_regions(frame: np.ndarray) -> list[dict[str, float]]:
    """Locate high-confidence text rows locally; designed for captions, timers and badges.

    This is deliberately conservative. It catches repeated aligned glyph groups while
    leaving ordinary shelves, hair and fabric texture as non-blocking visual detail.
    """
    height, width = frame.shape[:2]
    if height < 32 or width < 32:
        return []
    scale = min(1.0, 720.0 / max(height, width))
    sample = cv2.resize(frame, (max(32, int(width * scale)), max(32, int(height * scale))))
    sample_height, sample_width = sample.shape[:2]
    gray = cv2.cvtColor(sample, cv2.COLOR_BGR2GRAY)
    gradient = cv2.convertScaleAbs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3))
    _threshold, binary = cv2.threshold(gradient, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    close_width = max(5, int(round(sample_width * 0.018)))
    close_height = max(2, int(round(sample_height * 0.004)))
    closed = cv2.morphologyEx(
        binary,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (close_width, close_height)),
    )
    regions: list[dict[str, float]] = []
    contours, _hierarchy = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x, y, box_width, box_height = cv2.boundingRect(contour)
        width_ratio = box_width / sample_width
        height_ratio = box_height / sample_height
        aspect = box_width / max(box_height, 1)
        if not (0.025 <= width_ratio <= 0.78 and 0.008 <= height_ratio <= 0.13 and aspect >= 1.35):
            continue
        roi = closed[y : y + box_height, x : x + box_width]
        density = float(np.count_nonzero(roi)) / max(1.0, float(box_width * box_height))
        # Text needs several vertical stroke transitions. Long scene edges have too few.
        columns = np.count_nonzero(roi, axis=0) > max(1, int(box_height * 0.12))
        transitions = int(np.count_nonzero(columns[1:] != columns[:-1])) if columns.size > 1 else 0
        if not (0.10 <= density <= 0.82 and transitions >= 4):
            continue
        regions.append(
            {
                "x": round(x / sample_width, 4),
                "y": round(y / sample_height, 4),
                "width": round(width_ratio, 4),
                "height": round(height_ratio, 4),
                "confidence": round(min(1.0, transitions / 14.0 + density * 0.35), 3),
            }
        )
    return sorted(regions, key=lambda item: float(item["confidence"]), reverse=True)[:8]


def _rapid_ocr_regions(frame: np.ndarray) -> list[dict[str, Any]] | None:
    """Return locally recognized text, or None when the optional OCR is unavailable."""
    global _RAPID_OCR_ENGINE, _RAPID_OCR_UNAVAILABLE
    if _RAPID_OCR_UNAVAILABLE:
        return None
    with _RAPID_OCR_LOCK:
        try:
            if _RAPID_OCR_ENGINE is None:
                from rapidocr_onnxruntime import RapidOCR

                _RAPID_OCR_ENGINE = RapidOCR()
            raw_result, _elapsed = _RAPID_OCR_ENGINE(frame)
        except Exception:
            _RAPID_OCR_UNAVAILABLE = True
            return None
    regions: list[dict[str, Any]] = []
    for item in raw_result or []:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        box, text_value, confidence_value = item[0], str(item[1] or ""), float(item[2] or 0.0)
        visible_text = re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", text_value)
        if not visible_text or confidence_value < 0.55:
            continue
        points = np.asarray(box, dtype=np.float32).reshape(-1, 2)
        x, y, width, height = cv2.boundingRect(points.astype(np.int32))
        regions.append(
            {
                "text": visible_text[:40],
                "confidence": round(confidence_value, 3),
                "box": [[round(float(point[0]), 1), round(float(point[1]), 1)] for point in points],
                "x": x,
                "y": y,
                "width": width,
                "height": height,
            }
        )
    return regions


def _is_likely_overlay_text_region(region: dict[str, Any], frame_width: int, frame_height: int) -> bool:
    """Separate screen-fixed subtitle graphics from text that belongs to the photographed scene."""
    x = float(region.get("x") or 0.0)
    y = float(region.get("y") or 0.0)
    width = float(region.get("width") or 0.0)
    height = float(region.get("height") or 0.0)
    if max(abs(x), abs(y), abs(width), abs(height)) <= 1.01:
        x *= frame_width
        width *= frame_width
        y *= frame_height
        height *= frame_height
    width_ratio = width / max(1.0, float(frame_width))
    height_ratio = height / max(1.0, float(frame_height))
    center_x = (x + width / 2.0) / max(1.0, float(frame_width))
    center_y = (y + height / 2.0) / max(1.0, float(frame_height))
    if width_ratio < 0.055 or height_ratio > 0.14:
        return False
    box = region.get("box")
    if box:
        points = np.asarray(box, dtype=np.float32).reshape(-1, 2)
        if len(points) >= 2:
            delta_x = float(points[1][0] - points[0][0])
            delta_y = float(points[1][1] - points[0][1])
            if abs(delta_y) / max(1.0, abs(delta_x)) > 0.14:
                return False
    lower_caption = center_y >= 0.56 and width_ratio >= 0.075
    top_corner_overlay = center_y <= 0.18 and (center_x <= 0.28 or center_x >= 0.72)
    centered_title = 0.18 < center_y < 0.56 and width_ratio >= 0.28
    return bool(lower_caption or top_corner_overlay or centered_title)


def _sample_text_profile(
    video_path: Path,
    *,
    positions: tuple[float, ...] = (0.12, 0.32, 0.52, 0.72, 0.88),
    overlay_only: bool = False,
) -> dict[str, Any]:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        return {"detected": False, "stable_frames": 0, "frames": []}
    frame_count = max(1, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 1))
    frames: list[dict[str, Any]] = []
    try:
        for position in positions:
            capture.set(cv2.CAP_PROP_POS_FRAMES, min(frame_count - 1, int(frame_count * position)))
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            ocr_regions = _rapid_ocr_regions(frame)
            if ocr_regions is None:
                regions = _text_like_regions(frame)
                if overlay_only:
                    regions = [
                        region for region in regions
                        if _is_likely_overlay_text_region(region, frame.shape[1], frame.shape[0])
                    ]
                frames.append({"position": position, "regions": regions, "ocr": False})
            else:
                if overlay_only:
                    regions = [
                        region for region in ocr_regions
                        if _is_likely_overlay_text_region(region, frame.shape[1], frame.shape[0])
                    ]
                else:
                    regions = ocr_regions
                frames.append({"position": position, "regions": regions, "ocr": True})
    finally:
        capture.release()
    stable_frames = sum(1 for item in frames if item["regions"])
    used_ocr = any(bool(item.get("ocr")) for item in frames)
    meaningful_frames = sum(
        1
        for item in frames
        if any(
            len(str(region.get("text") or "")) >= 2
            and float(region.get("confidence") or 0.0) >= 0.6
            for region in item["regions"]
        )
    )
    high_confidence_single = any(
        float(region.get("confidence") or 0.0) >= 0.78
        and len(str(region.get("text") or "")) >= 2
        for item in frames
        for region in item["regions"]
    )
    return {
        "detected": bool(used_ocr and (meaningful_frames >= 2 or high_confidence_single)),
        "suspected": bool((not used_ocr and stable_frames >= 3) or (used_ocr and stable_frames >= 2 and meaningful_frames == 0)),
        "ocr_available": used_ocr,
        "stable_frames": stable_frames,
        "meaningful_frames": meaningful_frames,
        "sample_count": len(frames),
        "frames": frames,
    }


TEXT_SANITIZER_VERSION = 3


def _text_region_mask(frame: np.ndarray, regions: list[dict[str, Any]]) -> np.ndarray:
    """Build a padded mask that includes glyph strokes, outlines and subtitle shadows."""
    height, width = frame.shape[:2]
    mask = np.zeros((height, width), dtype=np.uint8)
    for region in regions:
        box = region.get("box")
        if box:
            points = np.asarray(box, dtype=np.float32).reshape(-1, 2).astype(np.int32)
            cv2.fillPoly(mask, [points], 255)
            x, y, box_width, box_height = cv2.boundingRect(points)
            # Generated signs and subtitles often contain several adjacent lines,
            # while OCR recognizes only one. Clear the whole local text panel so
            # unrecognized Korean/Japanese lines or subtitle shadows cannot remain.
            x_padding = max(5, int(round(box_width * 0.20)))
            y_padding = max(5, int(round(box_height * 0.55)))
            cv2.rectangle(
                mask,
                (max(0, x - x_padding), max(0, y - y_padding)),
                (min(width - 1, x + box_width + x_padding), min(height - 1, y + box_height + y_padding)),
                255,
                -1,
            )
            continue
        x_value = float(region.get("x") or 0.0)
        y_value = float(region.get("y") or 0.0)
        width_value = float(region.get("width") or 0.0)
        height_value = float(region.get("height") or 0.0)
        normalized = max(abs(x_value), abs(y_value), abs(width_value), abs(height_value)) <= 1.01
        x = int(round(x_value * width)) if normalized else int(round(x_value))
        y = int(round(y_value * height)) if normalized else int(round(y_value))
        box_width = int(round(width_value * width)) if normalized else int(round(width_value))
        box_height = int(round(height_value * height)) if normalized else int(round(height_value))
        if box_width > 0 and box_height > 0:
            cv2.rectangle(
                mask,
                (max(0, x), max(0, y)),
                (min(width - 1, x + box_width), min(height - 1, y + box_height)),
                255,
                -1,
            )
    if np.count_nonzero(mask):
        padding = max(5, int(round(min(height, width) * 0.008)))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (padding * 2 + 1, padding * 2 + 1))
        mask = cv2.dilate(mask, kernel, iterations=1)
    return mask


def sanitize_video_visible_text(
    source_path: str | Path,
    output_path: str | Path,
    *,
    preserve_audio: bool,
    overlay_only: bool = False,
    samples_per_second: float = 6.0,
) -> tuple[Path, dict[str, Any]]:
    """Remove visible text locally and preserve motion, timing and optionally generated audio.

    OCR is sampled several times per second. Each frame uses the surrounding two OCR
    masks so changing dialogue subtitles are removed between samples as well. The
    original file is retained as an audit artifact; a cleaned H.264 MP4 is returned.
    """
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.is_file():
        raise WorkflowError(f"找不到需要去字的视频：{source.name}")
    if source == output:
        raise WorkflowError("去字输出不能覆盖原视频。")
    output.parent.mkdir(parents=True, exist_ok=True)
    cache_path = output.with_suffix(output.suffix + ".text-clean.json")
    source_stat = source.stat()
    cache_key = {
        "version": TEXT_SANITIZER_VERSION,
        "source": str(source),
        "size": source_stat.st_size,
        "mtime_ns": source_stat.st_mtime_ns,
        "preserve_audio": bool(preserve_audio),
        "overlay_only": bool(overlay_only),
        "samples_per_second": round(float(samples_per_second), 2),
    }
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            cached = {}
        if isinstance(cached, dict) and cached.get("cache_key") == cache_key:
            cached_output = Path(str(cached.get("output_path") or source))
            if cached_output.is_file():
                return cached_output, dict(cached.get("profile") or {})

    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise WorkflowError(f"无法读取需要去字的视频：{source.name}")
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 24.0)
    frame_count = max(1, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 1))
    width = max(1, int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 1))
    height = max(1, int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1))
    interval = max(1, int(round(fps / max(1.0, float(samples_per_second)))))
    sampled_indices = sorted(set(range(0, frame_count, interval)) | {frame_count - 1})
    sampled_regions: dict[int, list[dict[str, Any]]] = {}
    try:
        for frame_index in sampled_indices:
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = capture.read()
            if not ok or frame is None:
                sampled_regions[frame_index] = []
                continue
            regions = _rapid_ocr_regions(frame)
            if regions is None:
                regions = [
                    region for region in _text_like_regions(frame)
                    if float(region.get("confidence") or 0.0) >= 0.55
                ]
            else:
                regions = [
                    region for region in regions
                    if (
                        len(str(region.get("text") or "")) >= 2
                        or re.search(r"[\u3400-\u9fff]", str(region.get("text") or ""))
                    )
                    and float(region.get("confidence") or 0.0) >= (0.55 if overlay_only else 0.6)
                ]
            if overlay_only:
                regions = [
                    region for region in regions
                    if _is_likely_overlay_text_region(region, width, height)
                ]
            sampled_regions[frame_index] = list(regions)
    finally:
        capture.release()

    detected_regions = sum(len(regions) for regions in sampled_regions.values())
    profile: dict[str, Any] = {
        "sample_count": len(sampled_indices),
        "detected_region_count": detected_regions,
        "changed": bool(detected_regions),
    }
    if not detected_regions:
        cache_path.write_text(
            json.dumps({"cache_key": cache_key, "output_path": str(source), "profile": profile}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return source, profile

    raw_video = output.with_name(f"{output.stem}_video_only.mp4")
    writer = cv2.VideoWriter(
        str(raw_video),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    if not writer.isOpened():
        raise WorkflowError("无法创建本地无字幕视频。")
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        writer.release()
        raise WorkflowError(f"无法重新读取需要去字的视频：{source.name}")
    changed_frames = 0
    sample_pointer = 0
    try:
        frame_index = 0
        while True:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            while sample_pointer + 1 < len(sampled_indices) and sampled_indices[sample_pointer + 1] <= frame_index:
                sample_pointer += 1
            neighbor_indices = {sampled_indices[sample_pointer]}
            if sample_pointer + 1 < len(sampled_indices):
                neighbor_indices.add(sampled_indices[sample_pointer + 1])
            if sample_pointer > 0:
                neighbor_indices.add(sampled_indices[sample_pointer - 1])
            mask = np.zeros((height, width), dtype=np.uint8)
            for neighbor in neighbor_indices:
                regions = sampled_regions.get(neighbor) or []
                if regions:
                    mask = cv2.bitwise_or(mask, _text_region_mask(frame, regions))
            if np.count_nonzero(mask):
                frame = cv2.inpaint(frame, mask, 5, cv2.INPAINT_TELEA)
                changed_frames += 1
            writer.write(frame)
            frame_index += 1
    finally:
        capture.release()
        writer.release()
    if not raw_video.is_file() or raw_video.stat().st_size == 0:
        raise WorkflowError("本地字幕区域已识别，但没有生成有效的无字幕画面。")

    command = [
        str(resolve_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(raw_video),
    ]
    if preserve_audio:
        command += ["-i", str(source), "-map", "0:v:0", "-map", "1:a?"]
    else:
        command += ["-map", "0:v:0", "-an"]
    command += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
    ]
    if preserve_audio:
        command += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
    command += ["-movflags", "+faststart", str(output)]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        detail = (completed.stderr or completed.stdout or "未知错误").strip()
        raise WorkflowError(f"本地无字幕视频编码失败：{detail[-500:]}")
    profile["changed_frames"] = changed_frames
    profile["output_path"] = str(output)
    cache_path.write_text(
        json.dumps({"cache_key": cache_key, "output_path": str(output), "profile": profile}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output, profile


def sanitize_video_visible_text_if_needed(
    source_path: str | Path,
    output_path: str | Path,
    *,
    preserve_audio: bool,
    overlay_only: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """Run the expensive renderer only when dense local sampling sees possible text."""
    source = Path(source_path).expanduser().resolve()
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        return source, {"changed": False, "unreadable": True}
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 24.0)
    frame_count = max(1, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 1))
    capture.release()
    sample_count = max(5, min(90, int(math.ceil(frame_count / max(1.0, fps) * 6.0))))
    positions = tuple(float(value) for value in np.linspace(0.01, 0.99, sample_count))
    profile = _sample_text_profile(source, positions=positions, overlay_only=overlay_only)
    if not bool(profile.get("detected")) and not bool(profile.get("suspected")):
        return source, {"changed": False, "preflight": profile}
    return sanitize_video_visible_text(
        source,
        output_path,
        preserve_audio=preserve_audio,
        overlay_only=overlay_only,
    )


def _sample_white_model_profile(
    video_path: Path,
    *,
    positions: tuple[float, ...] = (0.12, 0.32, 0.52, 0.72, 0.88),
) -> dict[str, Any]:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise WorkflowError(f"无法读取白模验收视频：{video_path.name}")
    frame_count = max(1, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 1))
    frames: list[dict[str, float]] = []
    try:
        for position in positions:
            capture.set(cv2.CAP_PROP_POS_FRAMES, min(frame_count - 1, int(frame_count * position)))
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            green = cv2.inRange(hsv, np.array([35, 55, 35]), np.array([95, 255, 255]))
            green_ratio = float(np.count_nonzero(green)) / float(green.size)
            subject = green == 0
            subject_count = int(np.count_nonzero(subject))
            if subject_count:
                subject_saturation = hsv[..., 1][subject]
                subject_value = hsv[..., 2][subject]
                neutral_bright = np.logical_and(subject_saturation <= 68, subject_value >= 72)
                colored = subject_saturation >= 105
                neutral_bright_ratio = float(np.count_nonzero(neutral_bright)) / subject_count
                colored_ratio = float(np.count_nonzero(colored)) / subject_count
            else:
                neutral_bright_ratio = 0.0
                colored_ratio = 0.0
            frames.append(
                {
                    "position": float(position),
                    "green_ratio": round(green_ratio, 4),
                    "subject_ratio": round(1.0 - green_ratio, 4),
                    "neutral_bright_ratio": round(neutral_bright_ratio, 4),
                    "colored_subject_ratio": round(colored_ratio, 4),
                }
            )
    finally:
        capture.release()
    median = lambda key: round(float(np.median([item[key] for item in frames])), 4) if frames else 0.0
    return {
        "frames": frames,
        "median_green_ratio": median("green_ratio"),
        "median_subject_ratio": median("subject_ratio"),
        "median_neutral_bright_ratio": median("neutral_bright_ratio"),
        "median_colored_subject_ratio": median("colored_subject_ratio"),
    }


def _shot_scale_label(face_height: float, person_area: float) -> tuple[str, str]:
    """Describe the framing that the real-person scene plate must preserve."""
    if face_height >= 0.34:
        return "人物特写", "只允许出现原结构蓝图可见的头肩范围；严禁展示人物腰部、腿部、地面或完整货架纵深"
    if face_height >= 0.22:
        return "近景", "保持头肩近景裁切；严禁主动拉远为半身、全身或环境展示镜头"
    if face_height >= 0.14 or person_area >= 0.24:
        return "中近景", "保持原有上半身范围与背景裁切，不得为了展示场景扩大视野"
    if face_height >= 0.08 or person_area >= 0.10:
        return "中景", "保持原有人物占位与可见场景范围，不得改成全景或建立镜头"
    return "远景/全景", "保持结构蓝图中的远近关系和完整环境范围"


def build_real_scene_geometry_prompt(motion_video: Path) -> str:
    """Build an absolute, measured shot-layout rule for real-person scene plates."""
    try:
        faces = _sample_face_scale_profile(motion_video, robust_primary=True)
        people = _sample_person_layout_profile(motion_video)
    except Exception:
        return (
            "真实人物分镜构图硬约束：图2至图4的灰色人体占位和透视线是唯一机位依据；"
            "不得为了展示图1场景而拉远、改焦段或扩大可见区域。"
        )
    face_height = float(faces.get("median_face_height") or 0.0)
    person_area = float(people.get("median_largest_area") or 0.0)
    center_x = float(faces.get("primary_center_x") or 0.5)
    center_y = float(faces.get("primary_center_y") or 0.5)
    scale_label, visibility_rule = _shot_scale_label(face_height, person_area)
    metrics: list[str] = [f"景别={scale_label}"]
    if face_height > 0:
        metrics.append(f"主脸高度约占画面{face_height * 100:.0f}%")
        metrics.append(f"主脸中心约在画面({center_x * 100:.0f}%,{center_y * 100:.0f}%)")
    if person_area > 0:
        metrics.append(f"最大人物占画面面积约{person_area * 100:.0f}%")
    return (
        "真实人物分镜构图硬约束（优先级高于图1场景展示）："
        + "，".join(metrics)
        + f"；{visibility_rule}。图2至图4只负责锁定这个机位、焦段、裁切、人物占位和遮挡；"
        "输出虽然是空场景板，但取景范围必须为灰色人体仍在场时的同一画框，禁止改成广角、建立镜头或展示更多空间。"
    )


def _read_reference_image(path: Path) -> np.ndarray | None:
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None


def _write_reference_image(path: Path, image: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        raise WorkflowError(f"无法保存分镜专属参考图：{path.name}")
    encoded.tofile(str(path))
    return path


def sanitize_reference_badges(image: np.ndarray) -> tuple[np.ndarray, int]:
    """Remove small high-contrast name badges before they can leak text into video.

    This does not attempt to redesign garments. Only compact pale label plates with
    dark internal strokes are inpainted; ordinary collars and large light garments
    are intentionally excluded.
    """
    height, width = image.shape[:2]
    if height < 64 or width < 64:
        return image, 0
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    pale = cv2.inRange(hsv, np.array([0, 0, 145]), np.array([179, 105, 255]))
    pale = cv2.morphologyEx(
        pale,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, width // 220), max(3, height // 220))),
    )
    contours, _hierarchy = cv2.findContours(pale, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    erase = np.zeros((height, width), dtype=np.uint8)
    removed = 0
    ocr_regions = _rapid_ocr_regions(image)
    for region in ocr_regions or []:
        points = np.asarray(region.get("box") or [], dtype=np.int32).reshape(-1, 2)
        if len(points) < 3:
            continue
        cv2.fillPoly(erase, [points], 255)
        removed += 1
    if removed:
        padding = max(3, int(round(min(height, width) * 0.008)))
        erase = cv2.dilate(
            erase,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (padding * 2 + 1, padding * 2 + 1)),
        )
    image_area = float(height * width)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    for contour in contours:
        x, y, box_width, box_height = cv2.boundingRect(contour)
        area_ratio = (box_width * box_height) / image_area
        aspect = box_width / max(box_height, 1)
        if not (0.00025 <= area_ratio <= 0.035 and 1.15 <= aspect <= 6.5):
            continue
        # Identity faces occupy the upper part of portrait boards; badges normally
        # sit below the neck. Avoid touching eyes, teeth and facial highlights.
        if y + box_height / 2 < height * 0.26:
            continue
        roi_gray = gray[y : y + box_height, x : x + box_width]
        roi_pale = pale[y : y + box_height, x : x + box_width]
        dark_fraction = float(np.count_nonzero(roi_gray < 115)) / max(1.0, float(roi_gray.size))
        pale_fraction = float(np.count_nonzero(roi_pale)) / max(1.0, float(roi_pale.size))
        if not (0.015 <= dark_fraction <= 0.42 and pale_fraction >= 0.42):
            continue
        padding_x = max(2, int(round(box_width * 0.08)))
        padding_y = max(2, int(round(box_height * 0.14)))
        left = max(0, x - padding_x)
        top = max(0, y - padding_y)
        right = min(width, x + box_width + padding_x)
        bottom = min(height, y + box_height + padding_y)
        erase[top:bottom, left:right] = 255
        removed += 1
    if not removed:
        return image, 0
    radius = max(3, int(round(min(height, width) * 0.008)))
    return cv2.inpaint(image, erase, radius, cv2.INPAINT_TELEA), removed


REAL_PERSON_REFERENCE_MAX_ASPECT = 2.40


def pad_real_person_reference_to_safe_aspect(
    image: np.ndarray,
    *,
    max_aspect: float = REAL_PERSON_REFERENCE_MAX_ASPECT,
) -> np.ndarray:
    """Pad Ark image inputs into a safe aspect range without scaling or cropping people."""
    if image is None or image.size == 0:
        raise WorkflowError("真人参考图为空，无法进行画面比例适配。")
    height, width = image.shape[:2]
    if height <= 0 or width <= 0:
        raise WorkflowError("真人参考图尺寸无效，无法进行画面比例适配。")
    limit = max(1.01, float(max_aspect))
    target_width = max(width, int(math.ceil(height / limit)))
    target_height = max(height, int(math.ceil(width / limit)))
    if target_width == width and target_height == height:
        return image

    # Use the median colour of the outer border so sketches stay white and
    # photographic references gain unobtrusive side/top padding. The original
    # pixels remain untouched and centred; only canvas space is added.
    border_size = max(1, min(height, width) // 40)
    border_pixels = np.concatenate(
        (
            image[:border_size, :].reshape(-1, image.shape[2]),
            image[-border_size:, :].reshape(-1, image.shape[2]),
            image[:, :border_size].reshape(-1, image.shape[2]),
            image[:, -border_size:].reshape(-1, image.shape[2]),
        ),
        axis=0,
    )
    fill = tuple(int(value) for value in np.median(border_pixels, axis=0))
    left = (target_width - width) // 2
    right = target_width - width - left
    top = (target_height - height) // 2
    bottom = target_height - height - top
    return cv2.copyMakeBorder(
        image,
        top,
        bottom,
        left,
        right,
        cv2.BORDER_CONSTANT,
        value=fill,
    )


def crop_real_person_closeup_reference(
    source: Path,
    output: Path,
    *,
    clothing: bool = False,
    closeup: bool = True,
    panel_index: int = 0,
    portrait_three_view: bool = False,
    identity_portrait: bool = False,
) -> Path:
    """Remove reference-board layout cues and optionally keep only the upper body."""
    image = _read_reference_image(source)
    if image is None:
        raise WorkflowError(f"无法读取真人分镜参考图：{source.name}")
    height, width = image.shape[:2]
    horizontal_board = width / max(height, 1) >= 1.12
    if horizontal_board:
        if portrait_three_view and not clothing:
            if identity_portrait:
                # New real-person board: large facial portrait occupies the left
                # two-fifths. This is the strongest identity reference.
                left_ratio, width_ratio = 0.00, 0.40
                crop_height_ratio = 0.70
            else:
                # The remaining area contains front / side / back full-body views.
                panel_index = max(0, min(2, int(panel_index)))
                panel_ranges = ((0.40, 0.22), (0.61, 0.21), (0.81, 0.19))
                left_ratio, width_ratio = panel_ranges[panel_index]
                crop_height_ratio = 0.52
            crop_width = max(1, int(round(width * width_ratio)))
            left = min(width - crop_width, int(round(width * left_ratio)))
            crop_height = max(1, int(round(height * crop_height_ratio))) if closeup else height
        else:
            # Classic user-supplied boards are front / side / back from left to right.
            panel_index = max(0, min(2, int(panel_index)))
            crop_width = max(1, int(round(width * 0.36)))
            left = min(width - crop_width, int(round(width * (0.32 * panel_index))))
            crop_height = (
                max(1, int(round(height * (0.60 if clothing else 0.52))))
                if closeup
                else height
            )
        crop = image[:crop_height, left : left + crop_width]
    else:
        crop_height = (
            max(1, int(round(height * (0.68 if clothing else 0.58))))
            if closeup
            else height
        )
        crop = image[:crop_height, :]
    sanitized, _removed_badges = sanitize_reference_badges(crop)
    safe = pad_real_person_reference_to_safe_aspect(sanitized)
    return _write_reference_image(output, safe)


def real_person_shot_character_sources(
    actors: list[dict[str, Any]],
    *,
    shot_dir: Path,
    motion_video: Path,
) -> tuple[list[tuple[str, str]], bool]:
    """Use one Ark character asset plus one shot-oriented clothing reference."""
    sources = real_person_character_sources(actors)
    try:
        face_height = float(
            _sample_face_scale_profile(motion_video, robust_primary=True).get("median_face_height") or 0.0
        )
    except Exception:
        face_height = 0.0
    closeup = face_height >= 0.22
    localized: list[tuple[str, str]] = []
    reference_dir = shot_dir / "real_closeup_references"
    for ordinal, (identity_asset, clothing) in enumerate(sources, start=1):
        anchor = str(actors[ordinal - 1].get("position_anchor") or "")
        if any(token in anchor for token in ("背对", "背影", "背部", "后脑")):
            pose_panel = 2
        elif any(token in anchor for token in ("侧面", "侧身", "侧脸", "侧向")):
            pose_panel = 1
        else:
            pose_panel = 0
        localized.append(
            (
                identity_asset,
                str(
                    crop_real_person_closeup_reference(
                        Path(clothing),
                        reference_dir / f"actor_{ordinal:02d}_clothing.jpg",
                        clothing=True,
                        closeup=closeup,
                        panel_index=pose_panel,
                    )
                ),
            )
        )
    return localized, closeup


def build_real_composition_retry_scene_plate(
    scene_path: Path,
    output_path: Path,
    qa: dict[str, Any],
    *,
    retry_number: int,
) -> tuple[Path, float]:
    """Create a materially different scene control image for underscaled real-person retries."""
    image = _read_reference_image(scene_path)
    if image is None:
        raise WorkflowError(f"无法读取构图纠偏场景板：{scene_path.name}")
    ratio = float(qa.get("face_scale_ratio") or 0.0)
    if ratio <= 0:
        person_ratio = float(qa.get("person_area_ratio") or 0.0)
        ratio = math.sqrt(person_ratio) if person_ratio > 0 else 1.0
    if ratio >= 0.82:
        return scene_path, 1.0
    zoom = min(2.8, max(1.15, (1.0 / max(ratio, 0.2)) * (1.0 + 0.10 * max(0, retry_number - 1))))
    source_profile = qa.get("source") if isinstance(qa.get("source"), dict) else {}
    center_x = float(source_profile.get("primary_center_x") or 0.5)
    center_y = float(source_profile.get("primary_center_y") or 0.45)
    height, width = image.shape[:2]
    crop_width = max(32, min(width, int(round(width / zoom))))
    crop_height = max(32, min(height, int(round(height / zoom))))
    left = int(round(center_x * width - crop_width / 2))
    top = int(round(center_y * height - crop_height / 2))
    left = max(0, min(width - crop_width, left))
    top = max(0, min(height - crop_height, top))
    crop = image[top : top + crop_height, left : left + crop_width]
    corrected = cv2.resize(crop, (width, height), interpolation=cv2.INTER_LANCZOS4)
    return _write_reference_image(output_path, corrected), round(zoom, 3)


def _people_primary_layout(profile: dict[str, Any]) -> dict[str, float]:
    primary = [
        max(frame.get("people") or [], key=lambda item: float(item.get("area") or 0.0))
        for frame in profile.get("frames") or []
        if frame.get("people")
    ]
    return {
        "center_x": round(float(np.median([item["center_x"] for item in primary])), 4) if primary else 0.0,
        "center_y": round(float(np.median([item["center_y"] for item in primary])), 4) if primary else 0.0,
        "area": round(float(np.median([item["area"] for item in primary])), 5) if primary else 0.0,
    }


def _motion_direction_delta(source: dict[str, float], output: dict[str, float]) -> float | None:
    source_magnitude = float(source.get("magnitude") or 0.0)
    output_magnitude = float(output.get("magnitude") or 0.0)
    if source_magnitude < 0.18 or output_magnitude < 0.12:
        return None
    cosine = (
        float(source.get("x") or 0.0) * float(output.get("x") or 0.0)
        + float(source.get("y") or 0.0) * float(output.get("y") or 0.0)
    ) / max(1e-6, source_magnitude * output_magnitude)
    return round(max(-1.0, min(1.0, cosine)), 3)


def validate_white_model(
    source_path: Path,
    white_model_path: Path,
    *,
    expected_actor_count: int,
) -> dict[str, Any]:
    """Gate the white-model artifact before it can become a paid final reference."""
    reasons: list[str] = []
    warnings: list[str] = []
    source_people = _sample_person_layout_profile(source_path)
    white_people = _sample_person_layout_profile(white_model_path)
    source_layout = _people_primary_layout(source_people)
    white_layout = _people_primary_layout(white_people)
    source_motion = _sample_motion_profile(source_path)
    white_motion = _sample_motion_profile(white_model_path)
    source_vector = _sample_global_motion_vector(source_path)
    white_vector = _sample_global_motion_vector(white_model_path)
    visual = _sample_white_model_profile(white_model_path)
    text = _sample_text_profile(white_model_path)
    source_dimensions = ""
    white_dimensions = ""
    aspect_ratio_delta = 0.0
    try:
        source_info = inspect_video(source_path)
        white_info = inspect_video(white_model_path)
        source_dimensions = f"{source_info.width}×{source_info.height}"
        white_dimensions = f"{white_info.width}×{white_info.height}"
        source_aspect = source_info.width / source_info.height
        white_aspect = white_info.width / white_info.height
        aspect_ratio_delta = abs(math.log(white_aspect / source_aspect))
        if aspect_ratio_delta > 0.08:
            reasons.append(
                f"白模画面比例与原片不一致（白模 {white_dimensions}，原片 {source_dimensions}）"
            )
    except Exception:
        warnings.append("无法校验白模与原片的画面比例，建议人工确认")

    if float(visual.get("median_green_ratio") or 0.0) < 0.16:
        reasons.append("白模背景没有形成稳定标准绿幕")
    if not 0.01 <= float(visual.get("median_subject_ratio") or 0.0) <= 0.9:
        reasons.append("白模人体占位异常，可能为空镜或几乎覆盖整个画面")
    if float(visual.get("median_neutral_bright_ratio") or 0.0) < 0.5:
        reasons.append("白模人体不是稳定的中性白色材质，疑似保留头发、服装或原人物颜色")
    elif float(visual.get("median_neutral_bright_ratio") or 0.0) < 0.68:
        warnings.append("白模中性白色材质比例偏低，建议人工检查是否残留服装或头发")
    if float(visual.get("median_colored_subject_ratio") or 0.0) > 0.2:
        reasons.append("白模人体区域仍有大面积彩色内容，疑似服装或皮肤残留")
    if bool(text.get("detected")):
        reasons.append("白模多帧检测到疑似字幕、时间码、Logo或文字")
    elif bool(text.get("suspected")):
        warnings.append("白模检测到单字符或纹理疑似文字，未自动拦截，建议人工预览")

    source_count = int(source_people.get("max_count") or 0)
    white_count = int(white_people.get("max_count") or 0)
    if expected_actor_count > 0 and source_count >= expected_actor_count and white_count < expected_actor_count:
        reasons.append("白模缺少原分镜中已确认的人物")
    source_area = float(source_layout.get("area") or 0.0)
    white_area = float(white_layout.get("area") or 0.0)
    area_ratio = white_area / source_area if source_area > 0 and white_area > 0 else 0.0
    if area_ratio:
        if area_ratio < 0.42 or area_ratio > 2.35:
            reasons.append(f"白模主要人物占比为原片的 {area_ratio:.2f} 倍，景别偏差过大")
        elif area_ratio < 0.64 or area_ratio > 1.62:
            warnings.append(f"白模主要人物占比为原片的 {area_ratio:.2f} 倍，存在可见偏差")
    center_delta = 0.0
    if source_layout.get("center_x") and white_layout.get("center_x"):
        center_delta = math.hypot(
            float(source_layout["center_x"]) - float(white_layout["center_x"]),
            float(source_layout["center_y"]) - float(white_layout["center_y"]),
        )
        if center_delta > 0.2:
            reasons.append("白模主要人物中心位置偏移过大")
        elif center_delta > 0.13:
            warnings.append("白模主要人物中心位置存在可见偏移")
    motion_ratio = white_motion / source_motion if source_motion >= 0.0015 else 0.0
    if source_motion >= 0.0015:
        if motion_ratio < 0.3:
            reasons.append("白模运动强度明显低于原片，动作或运镜可能丢失")
        elif motion_ratio < 0.5:
            warnings.append("白模运动强度低于原片")
    motion_cosine = _motion_direction_delta(source_vector, white_vector)
    if motion_cosine is not None and motion_cosine < -0.15:
        reasons.append("白模的主要运动方向与原片相反")

    return {
        "passed": not reasons,
        "reasons": reasons,
        "warnings": warnings,
        "source_people": source_people,
        "white_people": white_people,
        "white_visual": visual,
        "text_profile": text,
        "person_area_ratio": round(area_ratio, 3) if area_ratio else 0.0,
        "primary_center_delta": round(center_delta, 3),
        "source_motion": source_motion,
        "white_motion": white_motion,
        "motion_ratio": round(motion_ratio, 3) if motion_ratio else 0.0,
        "source_motion_vector": source_vector,
        "white_motion_vector": white_vector,
        "motion_direction_cosine": motion_cosine,
        "source_dimensions": source_dimensions,
        "white_model_dimensions": white_dimensions,
        "aspect_ratio_delta": round(aspect_ratio_delta, 4),
    }


def build_white_model_correction_prompt(qa: dict[str, Any]) -> str:
    reasons = "；".join(str(value) for value in qa.get("reasons") or [])
    directives: list[str] = []
    if "人物" in reasons or "景别" in reasons:
        directives.append("恢复@视频1全部人物的数量、画面占比、中心位置、前后景和局部入画")
    if "运动" in reasons or "运镜" in reasons:
        directives.append("恢复@视频1原动作速度、移动方向和镜头轨迹")
    if "绿幕" in reasons:
        directives.append("背景改为均匀纯色#00B140并清除全部原场景")
    if "彩色" in reasons or "服装" in reasons or "头发" in reasons:
        directives.append("彻底删除头发、服装、皮肤和配饰，只保留连续密封纯白哑光素体")
    if "文字" in reasons or "字幕" in reasons or "Logo" in reasons:
        directives.append("删除全部字幕、数字、时间码、Logo和水印，任何帧都不得出现字符")
    if not directives:
        directives.append("严格恢复@视频1人物占位与动作，并输出无文字纯白素体绿幕")
    return "白模自动验收纠偏：" + "；".join(dict.fromkeys(directives)) + "。不得沿用上一版错误。"


def validate_final_composition(
    source_path: Path,
    output_path: Path,
    *,
    expected_actor_count: int,
    real_person_mode: bool = False,
    white_model_path: Path | None = None,
) -> dict[str, Any]:
    """Reject severe shot-scale drift before a paid result can enter the final merge."""
    source = _sample_face_scale_profile(source_path, robust_primary=real_person_mode)
    output = _sample_face_scale_profile(output_path, robust_primary=real_person_mode)
    result: dict[str, Any] = {"passed": True, "source": source, "output": output, "reasons": [], "warnings": []}
    source_height = float(source["median_face_height"])
    output_height = float(output["median_face_height"])
    if source_height > 0 and output_height > 0:
        ratio = output_height / source_height
        result["face_scale_ratio"] = round(ratio, 3)
        if ratio < (0.64 if real_person_mode else 0.58) or ratio > (1.58 if real_person_mode else 1.72):
            result["reasons"].append(
                f"人物脸部尺度仅为原片的 {ratio:.2f} 倍，景别发生严重变化"
            )
    source_center_x = float(source.get("primary_center_x") or 0.0)
    source_center_y = float(source.get("primary_center_y") or 0.0)
    output_center_x = float(output.get("primary_center_x") or 0.0)
    output_center_y = float(output.get("primary_center_y") or 0.0)
    if source_center_x and output_center_x:
        center_delta = math.hypot(output_center_x - source_center_x, output_center_y - source_center_y)
        result["primary_center_delta"] = round(center_delta, 3)
        if center_delta > (0.18 if real_person_mode else 0.2):
            result["reasons"].append("主要人物在画面中的中心位置偏移过大，原构图未被保留")
    if expected_actor_count >= 2:
        if source["median_face_count"] >= 2 and output["median_face_count"] < 2:
            result["reasons"].append("原片为稳定双人构图，但成片多帧未保留两张可见人脸")
        paired_source = next((frame["faces"] for frame in source["frames"] if len(frame["faces"]) >= 2), [])
        paired_output = next((frame["faces"] for frame in output["frames"] if len(frame["faces"]) >= 2), [])
        if paired_source and paired_output:
            source_pair = (
                sorted(
                    sorted(paired_source, key=lambda face: float(face.get("area") or 0), reverse=True)[:2],
                    key=lambda face: float(face["center_x"]),
                )
                if real_person_mode
                else paired_source
            )
            output_pair = (
                sorted(
                    sorted(paired_output, key=lambda face: float(face.get("area") or 0), reverse=True)[:2],
                    key=lambda face: float(face["center_x"]),
                )
                if real_person_mode
                else paired_output
            )
            source_gap = source_pair[-1]["center_x"] - source_pair[0]["center_x"]
            output_gap = output_pair[-1]["center_x"] - output_pair[0]["center_x"]
            result["face_gap_delta"] = round(abs(source_gap - output_gap), 3)
            if abs(source_gap - output_gap) > 0.24:
                result["reasons"].append("双人横向间距与原片差异过大，构图被重新设计")
    source_people = _sample_person_layout_profile(source_path)
    output_people = _sample_person_layout_profile(output_path)
    result["source_people"] = source_people
    result["output_people"] = output_people
    if (
        expected_actor_count > 0
        and int(source_people.get("max_count") or 0) >= expected_actor_count
        and int(output_people.get("max_count") or 0) < expected_actor_count
    ):
        result["reasons"].append("成片未保留原分镜中已确认的全部人物，存在人物缺失")
    source_person_area = float(source_people.get("median_largest_area") or 0.0)
    output_person_area = float(output_people.get("median_largest_area") or 0.0)
    if source_person_area > 0 and output_person_area > 0:
        person_area_ratio = output_person_area / source_person_area
        result["person_area_ratio"] = round(person_area_ratio, 3)
        if person_area_ratio < (0.42 if real_person_mode else 0.32) or person_area_ratio > (2.4 if real_person_mode else 3.1):
            result["reasons"].append(
                f"主要人物画面占比仅为原片的 {person_area_ratio:.2f} 倍，人物尺度严重不符"
            )
    source_motion = _sample_motion_profile(source_path)
    output_motion = _sample_motion_profile(output_path)
    result["source_motion"] = source_motion
    result["output_motion"] = output_motion
    if source_motion >= 0.0015:
        motion_ratio = output_motion / source_motion if source_motion else 0.0
        result["motion_ratio"] = round(motion_ratio, 3)
        if motion_ratio < 0.38:
            result["reasons"].append("成片镜头运动强度明显低于原片，原有运镜可能被改成近似固定镜头")
    source_vector = _sample_global_motion_vector(source_path)
    output_vector = _sample_global_motion_vector(output_path)
    result["source_motion_vector"] = source_vector
    result["output_motion_vector"] = output_vector
    direction_cosine = _motion_direction_delta(source_vector, output_vector)
    result["motion_direction_cosine"] = direction_cosine
    if direction_cosine is not None and direction_cosine < -0.18:
        result["reasons"].append("成片主要运动方向与原片相反，运镜或人物移动方向错误")
    if real_person_mode:
        text_profile = _sample_text_profile(output_path, overlay_only=True)
        result["text_profile"] = text_profile
        if bool(text_profile.get("detected")):
            result["reasons"].append("成片多帧检测到疑似字幕、数字、Logo或水印")
        elif bool(text_profile.get("suspected")):
            result["warnings"].append("成片检测到单字符或纹理疑似文字，未达到自动拦截阈值")
    if real_person_mode and white_model_path is not None and white_model_path.is_file():
        white_people = _sample_person_layout_profile(white_model_path)
        white_layout = _people_primary_layout(white_people)
        output_layout = _people_primary_layout(output_people)
        result["white_people"] = white_people
        white_area = float(white_layout.get("area") or 0.0)
        final_area = float(output_layout.get("area") or 0.0)
        if white_area > 0 and final_area > 0:
            white_area_ratio = final_area / white_area
            result["white_person_area_ratio"] = round(white_area_ratio, 3)
            if white_area_ratio < 0.5 or white_area_ratio > 2.0:
                result["reasons"].append(
                    f"成片主要人物占位仅为白模的 {white_area_ratio:.2f} 倍，最终生成忽略了白模景别"
                )
        if white_layout.get("center_x") and output_layout.get("center_x"):
            white_center_delta = math.hypot(
                float(white_layout["center_x"]) - float(output_layout["center_x"]),
                float(white_layout["center_y"]) - float(output_layout["center_y"]),
            )
            result["white_primary_center_delta"] = round(white_center_delta, 3)
            if white_center_delta > 0.18:
                result["reasons"].append("成片人物中心位置明显偏离白模母版")
        white_motion = _sample_motion_profile(white_model_path)
        result["white_motion"] = white_motion
        if white_motion >= 0.0015:
            final_white_motion_ratio = output_motion / white_motion
            result["white_motion_ratio"] = round(final_white_motion_ratio, 3)
            if final_white_motion_ratio < 0.34:
                result["reasons"].append("成片运动量明显低于白模，动作或运镜在最终阶段丢失")
    result["passed"] = not result["reasons"]
    return result


def build_composition_correction_prompt(
    qa: dict[str, Any],
    *,
    retry_number: int,
    absolute_white_model: bool = False,
) -> str:
    """Turn local QA measurements into a short paid-retry instruction without truncating dialogue."""
    directives: list[str] = []
    ratio = float(qa.get("face_scale_ratio") or 0.0)
    if ratio > 0:
        target = max(0.55, min(2.2, 1.0 / ratio))
        if ratio < 0.8:
            if absolute_white_model:
                directives.append(
                    f"上一版主脸仅为白模的{ratio:.2f}倍；本次直接恢复到@视频1的100%人物尺度，不得沿用上一版中景"
                )
            else:
                directives.append(f"主要人物相对上一版放大约{target:.2f}倍")
        elif ratio > 1.25:
            if absolute_white_model:
                directives.append(
                    f"上一版主脸为白模的{ratio:.2f}倍；本次直接恢复到@视频1的100%人物尺度，不得沿用上一版特写"
                )
            else:
                directives.append(f"主要人物相对上一版缩小约{target:.2f}倍")
    reasons = "；".join(str(value) for value in qa.get("reasons") or [])
    if "可见人脸" in reasons or "人物" in reasons:
        directives.append("保留白模中的全部人物及前景局部遮挡")
    if "横向间距" in reasons or "中心位置" in reasons:
        directives.append("恢复原片左右站位、中心位置与人物间距")
    if "镜头运动" in reasons:
        directives.append("恢复白模的原始运镜强度与轨迹")
    if not directives:
        directives.append("逐帧恢复白模的人物尺度、站位、遮挡、景别和运镜")
    return (
        f"构图纠偏第{retry_number}次：上一版验收未通过；"
        + "，".join(dict.fromkeys(directives))
        + (
            "。@视频1只提供白模人物的逐帧动作、尺度、站位、遮挡、景别和运镜；"
            "场景参考图只提供当前分镜的新环境外观，不得把静态参考图的原始机位强加给成片。"
            if absolute_white_model
            else "。人物图仍只负责长相，服装图仍只负责服装，场景图不得重新构图。"
        )
    )


def build_long_shot_prompt(
    actors: list[dict[str, Any]],
    common_constraints: str,
    *,
    replace_scene: bool = False,
    white_model_reference: bool = False,
    real_person_mode: bool = False,
) -> str:
    if not 0 <= len(actors) <= 4:
        raise WorkflowError("分镜最多支持 4 位出场人物。")
    clauses = ["参考@视频1"]
    if white_model_reference:
        clauses.append(WHITE_MODEL_FINAL_MASTER_CONSTRAINT)
    prompt_actors: list[tuple[int, str, int, str]] = []
    images_per_actor = 2
    if real_person_mode and actors:
        clauses.append(
            "真人参考总规则：火山角色库素材只锁定人物身份、脸部、发型与体型；"
            "不得继承人物素材或服装图的姿势、排版、背景、光线、机位、景别或构图"
        )
    for local_index, actor in enumerate(actors, start=1):
        role = _clean_long_role(str(actor.get("role") or ""), int(actor.get("id") or local_index))
        source_slot = int(actor.get("source_slot") or local_index)
        source_character_id = int(actor.get("source_character_id") or 0)
        position_anchor = " ".join(str(actor.get("position_anchor") or "").strip().split())
        if len(position_anchor) > 220:
            raise WorkflowError(f"表演槽位 P{source_slot} 的空间动作锚点不能超过 220 个字符。")
        prompt_actors.append((local_index, role, source_slot, position_anchor))
        continuity_label = f"、原片稳定身份C{source_character_id}" if source_character_id else ""
        source_role = f"表演槽位P{source_slot}{continuity_label}「{role}」"
        first_image = (local_index - 1) * images_per_actor + 1
        if real_person_mode:
            clauses.append(
                f"@图片{first_image}是火山角色库已授权人物素材，只提供人物身份、长相、发型和体型；"
                f"@图片{first_image + 1}只提供服装、鞋履与配饰；"
                f"两项参考共同对应{source_role}"
            )
        else:
            clauses.append(
                f"将原分镜中的{source_role}替换为@图片{first_image}中的人物长相；"
                f"@图片{first_image}不得改变白模中的姿势、人物尺度、站位、景别或构图。"
                f"@图片{first_image + 1}只提供该人物的服装、鞋履与配饰，不得提供姿势、机位或构图"
            )
    scene_image = len(actors) * images_per_actor + 1
    if replace_scene:
        clauses.append(
            f"将本分镜全部背景替换为@图片{scene_image}中的新场景；"
            f"@图片{scene_image}只提供新场景外观、材质、色彩和光照；"
            + ("只是场景参考，不做必要背景；" if real_person_mode else "")
            + "不得残留、恢复或复用原场景内容；@视频1的镜头机位、景别、透视、裁切和构图不得改变，运动视差也必须保留"
        )
    else:
        clauses.append(f"原分镜场景严格参考@图片{scene_image}并完整保留")
    if actors:
        clauses.append(f"本镜头必须恰好保留 {len(actors)} 位出场人物，不得增加、遗漏或互换身份")
        bindings = []
        for local_index, role, source_slot, position_anchor in prompt_actors:
            first_image = (local_index - 1) * images_per_actor + 1
            reference_label = (
                f"火山角色库人物@图片{first_image}"
                if real_person_mode
                else f"@图片{first_image}"
            )
            binding = f"{reference_label}只能对应表演槽位P{source_slot}「{role}」"
            if position_anchor:
                binding += f"，其空间动作锚点固定为「{position_anchor}」"
            bindings.append(binding)
        clauses.append(
            "人物身份与空间位置绑定为最高优先级："
            + "；".join(bindings)
            + "；从首帧到末帧保持各自原有的左右位置、前景/中景/后景层级、远近尺度、坐姿/站姿、遮挡关系和动作轨迹，"
                "严禁人物交换位置、交换动作、交换台词、交换服装或一人占据另一人的空间"
        )
        if real_person_mode:
            clauses.append(
                "最终输出必须是正常彩色成片，不得出现参考图拼贴、分屏、素材板边框或人物库标记"
            )
    else:
        clauses.append("本镜头是无人场景，最终画面不得生成任何人物、人体、服装、玩偶或人形轮廓")
    constraints = common_constraints.strip() or DEFAULT_LONG_VIDEO_PROMPT
    clauses.append(constraints)
    return "；".join(clauses) + "。"


def _reference_fingerprint(value: str) -> str:
    source = str(value or "")
    if source.startswith("asset://"):
        return source
    path = Path(source)
    if not path.is_file():
        return source
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def long_scene_plate_signature(
    *,
    shot_index: int,
    scene_source: str,
    source_reference: str,
    motion_reference: str,
    prompt: str,
    model: str,
    signature_version: int = SCENE_PLATE_SIGNATURE_VERSION,
) -> str:
    """Cache scene plates only when both scene appearance and this shot's geometry are unchanged."""
    payload = {
        "shot_index": int(shot_index),
        "scene": _reference_fingerprint(scene_source),
        "source_reference": _reference_fingerprint(source_reference),
        "motion_reference": _reference_fingerprint(motion_reference),
        "prompt": str(prompt or ""),
        "model": str(model or ""),
        "version": int(signature_version),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def real_person_character_sources(actors: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Return one Active Ark identity asset plus clothing for each real actor."""
    sources: list[tuple[str, str]] = []
    for ordinal, actor in enumerate(actors, start=1):
        index = int(actor.get("id") or ordinal)
        trusted_asset_uri = str(
            actor.get("trusted_asset_uri") or actor.get("original_person_source") or ""
        )
        clothing = str(actor.get("clothing_source") or "")
        required = {"火山角色库人物素材": trusted_asset_uri, "服装参考图": clothing}
        for label, value in required.items():
            if not value or (not value.startswith("asset://") and not Path(value).is_file()):
                raise WorkflowError(f"人物 {index} 缺少有效的{label}。")
        if not re.fullmatch(r"asset://asset-[A-Za-z0-9_-]{6,120}", trusted_asset_uri):
            raise WorkflowError(f"人物 {index} 尚未绑定有效的火山角色库 Asset。")
        sources.append((trusted_asset_uri, clothing))
    return sources


def selected_whole_scene_reference(job: WebJob, selection: str) -> Path:
    """Resolve one original user scene image for whole-video generation."""
    raw = str(selection or "").strip()
    match = re.fullmatch(r"([A-Za-z0-9_-]{1,40}):(\d{1,2})", raw)
    if not match:
        raise WorkflowError("整段生成模式需要选择一张场景参考图。")
    group_id, raw_index = match.groups()
    image_index = int(raw_index)
    group = next(
        (item for item in job.scene_groups if str(item.get("id") or "") == group_id),
        None,
    )
    images = list(group.get("images") or []) if isinstance(group, dict) else []
    if not 1 <= image_index <= len(images):
        raise WorkflowError("整段生成模式选择的场景参考图已失效，请重新选择。")
    path = Path(str(images[image_index - 1]))
    if not path.is_file():
        raise WorkflowError("整段生成模式选择的场景参考图文件不存在，请重新上传。")
    return path


def _whole_video_dialogue_timeline(shots: list[dict[str, Any]]) -> str:
    """Convert reviewed per-shot dialogue timing to the merged video's absolute time."""
    segments: list[str] = []
    time_pattern = re.compile(r"\[(\d+(?:\.\d+)?)\s*[–—-]\s*(\d+(?:\.\d+)?)秒\]")
    for shot in shots:
        performance = shot.get("performance")
        if not isinstance(performance, dict):
            continue
        source = str(performance.get("manual_dialogue_text") or "").strip()
        if not source:
            continue
        offset = float(shot.get("start") or 0)

        def absolute_time(match: re.Match[str]) -> str:
            return f"[{float(match.group(1)) + offset:.2f}–{float(match.group(2)) + offset:.2f}秒]"

        compact = " / ".join(
            " ".join(line.split())
            for line in time_pattern.sub(absolute_time, source).splitlines()
            if line.strip()
        )
        if compact:
            segments.append(f"S{int(shot.get('index') or 0)} {compact}")
    if not segments:
        return "全片无人工确认台词，不得新增人声。"
    result = "人工确认的全片绝对时间台词：" + "；".join(segments)
    if len(result) > 760:
        raise WorkflowError(
            "整段生成模式的人工台词时间轴过长，无法在不截断台词的情况下安全提交；"
            "请精简重复语气和口型说明，但不要删除台词正文。"
        )
    return result


def _compact_whole_identity_anchor(value: Any, *, limit: int = 28) -> str:
    """Keep only the shortest useful visual cue for locating one white-model slot."""
    text = " ".join(sanitize_motion_evidence(value).split())
    if not text:
        return ""
    first_clause = re.split(r"[；。！？\n]", text, maxsplit=1)[0].strip("，, ")
    return first_clause[:limit]


def _whole_video_identity_map(
    actors: list[dict[str, Any]],
    shots: list[dict[str, Any]],
) -> str:
    """Serialize reviewed per-shot P-slot bindings into the Seedance prompt."""
    actor_images = {
        int(actor.get("id") or ordinal): (ordinal - 1) * 2 + 1
        for ordinal, actor in enumerate(actors, start=1)
    }
    shot_clauses: list[str] = []
    for shot in shots:
        shot_index = int(shot.get("index") or 0)
        raw_mappings = [
            dict(mapping)
            for mapping in shot.get("actor_mappings") or []
            if isinstance(mapping, dict)
        ]
        if not raw_mappings:
            raw_mappings = [
                {"slot": slot, "actor_id": actor_id}
                for slot, actor_id in enumerate(shot.get("actor_ids") or [], start=1)
            ]
        mappings = sorted(raw_mappings, key=lambda item: int(item.get("slot") or 0))
        if not mappings:
            shot_clauses.append(f"S{shot_index}:无人")
            continue
        slot_clauses: list[str] = []
        for mapping in mappings:
            slot = int(mapping.get("slot") or 0)
            actor_id = int(mapping.get("actor_id") or 0)
            image_index = actor_images.get(actor_id)
            if slot <= 0 or image_index is None:
                raise WorkflowError(
                    f"分镜 {shot_index:02d} 的白模人物槽位映射无效，已阻止提交付费任务。"
                )
            anchor = _compact_whole_identity_anchor(
                mapping.get("source_position") or long_shot_performance_slot_anchor(shot, slot)
            )
            anchor_text = f"[{anchor}]" if anchor else ""
            slot_clauses.append(
                f"P{slot}{anchor_text}=人物{actor_id}@图片{image_index}"
            )
        shot_clauses.append(f"S{shot_index}:" + ",".join(slot_clauses))
    return (
        "逐镜人物身份硬锁（P为@视频1中的白模人物槽位）："
        + "；".join(shot_clauses)
        + "。每个白模身体必须始终替换为指定人物及其配套服装；切镜前后不得按画面面积、前后景、"
        "说话状态或出场顺序重新分配身份，严禁换脸、换装、互换台词"
    )


def build_real_whole_video_prompt(
    actors: list[dict[str, Any]],
    shots: list[dict[str, Any]],
    common_prompt: str,
) -> str:
    """Build one continuity-first prompt for a merged white-model edit."""
    active_shots = [shot for shot in shots if not real_long_shot_is_skipped(shot)]
    cut_points = [float(shot.get("start") or 0.0) for shot in active_shots[1:]]
    cut_lock = (
        f"切镜硬锁：全片必须保持{len(active_shots)}个镜头，切点严格位于"
        + "、".join(f"{value:.2f}秒" for value in cut_points)
        + "；不得提前、延后、漏切、加切、叠化或用镜内运动替代硬切"
        if cut_points
        else "切镜硬锁：全片保持@视频1的单镜连续时长，不得新增切镜、叠化或转场"
    )
    clauses = [
        REAL_WHOLE_NO_VISIBLE_TEXT_CONSTRAINT,
        "参考优先级固定为：@视频1只锁定全片时空、剪辑、表演和镜头；人物@图片只锁定身份与外观；服装@图片只锁定服装；"
        "场景@图片只锁定环境视觉。低优先级参考不得覆盖高优先级的动作、站位、景别、构图或运镜",
        "以@视频1整段合并白模为唯一时空、剪辑和表演母版：逐帧保持全部镜头顺序、切点、时长、人物数量、"
        "人物尺度、左右站位、前后景、遮挡、动作时序、机位高度与角度、景别、焦段、裁切、透视、对焦和运镜。"
        "每个切点前后的首尾帧都必须对应@视频1；禁止拆镜、重排、变速、漏人、加人、换位、自动居中、重新取景、"
        "擅自推近拉远或把局部前景人物改成完整人物",
        cut_lock,
        "逐镜构图硬锁：每镜首帧、中间帧和尾帧的人物画面占比、左右位置、前后景、遮挡、地平线、机位高度、"
        "俯仰角、焦段、景别、裁切边界和运镜轨迹均以@视频1为准；只换人物、服装和场景外观，不重拍镜头",
        _whole_video_identity_map(actors, active_shots),
        REAL_WHOLE_WHITE_APPEARANCE_CONSTRAINT,
    ]
    for ordinal, actor in enumerate(actors, start=1):
        identity_image = (ordinal - 1) * 2 + 1
        clothing_image = identity_image + 1
        role = " ".join(str(actor.get("role") or f"人物{ordinal}").split())[:48]
        clauses.append(
            f"@图片{identity_image}是{role}的火山角色身份，只锁定脸部、发型、体型与跨镜身份；"
            f"@图片{clothing_image}只提供该人物服装。最终脸部与发型必须以@图片{identity_image}为准，"
            f"二者不得改变@视频1的动作、位置、遮挡、人物尺度或镜头"
        )
    scene_image = len(actors) * 2 + 1
    clauses.append(
        f"@图片{scene_image}是场景图。{REAL_WHOLE_SCENE_REFERENCE_CONSTRAINT}"
        "它只定义同一连续地点的空间关系、建筑结构、固定陈设、材质、色彩与灯光；"
        "必须在全片保持门窗、通道、货架、家具和方位连续，并随@视频1不同机位自然产生透视、视差、"
        "可见区域和景别变化；禁止把场景图静态贴在人物背后，禁止每个分镜重建成不同地点"
    )
    clauses.append(_whole_video_dialogue_timeline(shots))
    clauses.append(
        "有台词时由Seedance按新角色生成全新音色，逐字遵守人工时间轴并同步口型；"
        "@视频1已静音，不得复用原片对白、BGM、歌声、环境音或声纹"
    )
    compact_common = compact_long_generation_constraints(common_prompt, real_person_mode=True)
    compact_common = compact_common.replace(COMPACT_LONG_VIDEO_PROMPT, "").replace(
        REAL_PERSON_COMPACT_NO_VISIBLE_TEXT_CONSTRAINT,
        "",
    ).strip()
    if compact_common:
        clauses.append(compact_common)
    clauses.append("最终检查：全片零叠加字幕、零标题、零文字层、零平台Logo或水印；白模误生成的头发不得进入任何最终帧")
    prompt = "；".join(clauses).strip("；。") + "。"
    if len(prompt) > SEEDANCE_SAFE_PROMPT_LIMIT:
        raise WorkflowError(
            "整段生成提示词超过 Seedance 安全上限；系统没有截断人物映射或台词，"
            "请精简共用提示词后重试。"
        )
    return prompt


def long_shot_generation_signature(
    actors: list[dict[str, Any]],
    *,
    options: dict[str, Any],
    scene_prompt: str,
    image_model: str,
    scene_source: str = "",
    motion_reference: str = "",
    performance: dict[str, Any] | None = None,
    position_locks: list[str] | None = None,
    cast_character_ids: list[int] | None = None,
    signature_version: int = 14,
) -> str:
    payload = {
        "actors": [
            {
                "id": int(actor["id"]),
                "role": str(actor["role"]),
                "person": _reference_fingerprint(
                    str(actor.get("masked_person_source") or actor["person_source"])
                ),
                **(
                    {
                        "masked_person": _reference_fingerprint(str(actor.get("masked_person_source") or "")),
                        "sketch_person": _reference_fingerprint(str(actor.get("sketch_person_source") or "")),
                    }
                    if actor.get("masked_person_source") or actor.get("sketch_person_source")
                    else {}
                ),
                "clothing": _reference_fingerprint(str(actor["clothing_source"])),
            }
            for actor in actors
        ],
        "prompt": options.get("prompt"),
        "model": options.get("model"),
        "resolution": options.get("resolution"),
        "ratio": options.get("ratio"),
        "watermark": bool(options.get("watermark")),
        "generate_audio": bool(options.get("generate_audio")),
        "dialogue_voice_mode": str(options.get("dialogue_voice_mode") or "silent"),
        "scene_prompt": scene_prompt,
        "image_model": image_model,
        "scene_source": _reference_fingerprint(scene_source),
        "motion_reference": _reference_fingerprint(motion_reference),
        "performance": performance or {},
        "version": int(signature_version),
    }
    if int(signature_version) >= 8:
        payload["position_locks"] = position_locks or []
    if int(signature_version) >= 14:
        payload["cast_character_ids"] = cast_character_ids or []
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def build_scene_only_prompt() -> str:
    return (
        "参考@视频1，完整保留原视频主角的人物身份、面部、发型、体型和全部动作；人物形象严格参考@图片1，"
        "原始服装、鞋履、头饰和配饰严格参考@图片2；仅将原视频背景与空间环境替换为@图片3中的新场景。"
        "不得改变人物身份、服装设计、动作、表情、视线、人物在画面中的尺寸、站位或遮挡关系。完全复刻原视频主角的"
        "脚步、抬手、摆臂、躯干倾斜、弹跳重心变化、转向、停顿与定格姿势，所有动作起始时间、落点、强拍定格和节奏变化"
        "均与原视频帧级对齐；镜头机位、焦段、裁切、透视、景别、运镜轨迹和画面构图完全复刻原视频。将@图片3场景自然"
        "延展到完整画面，人物与新场景的接触、遮挡、透视和光照合理。@视频1仅作为深度、动作、遮挡和镜头约束，不要输出"
        "黑白深度风格；最终输出正常彩色视频。禁止出现原背景残留、人物变脸、换装、串帧、额外人物、文字、Logo或水印。"
    )


def save_scene_only_target(job: WebJob, *, reuse_job: WebJob | None = None) -> str:
    upload = request.files.get("scene_image")
    if upload is not None and upload.filename:
        scene = save_upload(upload, job.run_dir, "new_scene")
    elif reuse_job and reuse_job.scene_path and reuse_job.scene_path.is_file():
        scene = reuse_job.scene_path
        job.log("已复用项目 4 中保存的新场景参考图。")
    else:
        raise WorkflowError("缺少需要替换的新场景参考图。")
    if scene.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
        raise WorkflowError("新场景参考必须是 JPG、PNG、WEBP 或 BMP 图片。")
    job.scene_path = scene
    return str(scene)


def run_person_triview_extraction(
    job: WebJob,
    source: Path,
    *,
    prompt: str,
    model: str,
    start: int = 0,
    span: int = 100,
    reuse_existing: bool = False,
    recovery_action: str = "retry_scene_subject",
    required_constraint: str = "",
    artifact_version: str = "",
) -> Path:
    output = job.run_dir / "original_person_triview.jpg"
    version_file = job.run_dir / "original_person_triview.version"
    stored_version = ""
    if version_file.is_file():
        try:
            stored_version = version_file.read_text(encoding="utf-8").strip()
        except OSError:
            stored_version = ""
    version_matches = not artifact_version or stored_version == artifact_version
    if reuse_existing and output.is_file() and output.stat().st_size > 0 and version_matches:
        job.person_path = output
        job.log("已复用上次成功生成的原人物三视图。")
        return output
    if reuse_existing and output.is_file() and output.stat().st_size > 0 and not version_matches:
        job.log("检测到旧版人物三视图；将按项目5白色底衫规则重新生成，不复用包含原片服装的旧结果。")
    job.update(status="running", stage="正在从原视频提取人物参考帧", progress=start)
    frames = extract_scene_reference_frames(source, job.run_dir, count=3)
    enforced_prompt = "\n".join(
        item
        for item in (
            prompt.strip() or DEFAULT_PERSON_TRIVIEW_PROMPT,
            required_constraint.strip(),
            PERSON_TRIVIEW_REQUIRED_CONSTRAINT,
        )
        if item
    )
    job.update(stage="Seedream 5.0 正在生成 16:9 原人物三视图", progress=start + int(span * 0.24))
    try:
        result = api_client().generate_image(
            prompt=enforced_prompt,
            image_sources=[str(path) for path in frames],
            model=model.strip() or DEFAULT_SEEDREAM_MODEL,
            size=PERSON_TRIVIEW_SIZE,
            watermark=False,
        )
    except ArkConnectionError as exc:
        job.update(recovery_action=recovery_action)
        raise WorkflowError(
            "Seedream 在等待人物三视图时连接中断，结果未知。参考视频、深度视频和抽帧均已保留；"
            "系统没有自动重复付费请求，请手动确认后只补充缺失参考图。"
        ) from exc
    job.update(stage="正在下载 16:9 原人物三视图", progress=start + int(span * 0.84))
    download_file(result["url"], output)
    if artifact_version:
        version_file.write_text(artifact_version, encoding="utf-8")
    job.person_path = output
    job.update(progress=start + span, recovery_action="")
    job.log("16:9 原人物三视图已生成。")
    return output


def run_original_subject_extraction(
    job: WebJob,
    source: Path,
    *,
    person_prompt: str,
    clothing_prompt: str,
    model: str,
    start: int = 0,
    span: int = 100,
    reuse_existing: bool = False,
) -> tuple[Path, Path]:
    job.update(status="running", stage="正在从原视频提取人物与服装参考帧", progress=start)
    frames = extract_scene_reference_frames(source, job.run_dir, count=3)
    job.log("已从原视频前、中、后段准备三张人物与服装参考帧。")
    client = api_client()
    person_output = job.run_dir / "original_person_triview.jpg"
    clothing_output = job.run_dir / "original_clothing_triview.jpg"
    enforced_person_prompt = f"{person_prompt.strip() or DEFAULT_PERSON_TRIVIEW_PROMPT}\n{PERSON_TRIVIEW_REQUIRED_CONSTRAINT}"
    enforced_clothing_prompt = (
        f"{clothing_prompt.strip() or DEFAULT_CLOTHING_TRIVIEW_PROMPT}\n"
        f"{CLOTHING_TRIVIEW_REQUIRED_CONSTRAINT}"
    )

    if reuse_existing and person_output.is_file() and person_output.stat().st_size > 0:
        job.person_path = person_output
        job.log("已复用上次成功生成的原人物三视图。")
    else:
        job.update(
            stage="Seedream 5.0 正在生成原人物三视图",
            progress=start + int(span * 0.16),
        )
        try:
            result = client.generate_image(
                prompt=enforced_person_prompt,
                image_sources=[str(path) for path in frames],
                model=model.strip() or DEFAULT_SEEDREAM_MODEL,
                size=PERSON_TRIVIEW_SIZE,
                watermark=False,
            )
        except ArkConnectionError as exc:
            job.update(recovery_action="retry_scene_subject")
            raise WorkflowError(
                "Seedream 在等待原人物三视图时连接中断，结果未知。深度视频和参考帧已保留；"
                "系统没有自动重复付费请求，请手动确认后安全重试人物服装提取。"
            ) from exc
        job.update(stage="正在下载原人物三视图", progress=start + int(span * 0.39))
        download_file(result["url"], person_output)
        job.person_path = person_output
        job.log("原人物三视图已生成。")

    if reuse_existing and clothing_output.is_file() and clothing_output.stat().st_size > 0:
        job.clothing_path = clothing_output
        job.log("已复用上次成功生成的原服装三视图。")
    else:
        job.update(
            stage="Seedream 5.0 正在生成原服装三视图",
            progress=start + int(span * 0.55),
        )
        try:
            result = client.generate_image(
                prompt=enforced_clothing_prompt,
                image_sources=[str(path) for path in frames],
                model=model.strip() or DEFAULT_SEEDREAM_MODEL,
                size="2K",
                watermark=False,
            )
        except ArkConnectionError as exc:
            job.update(recovery_action="retry_scene_subject")
            raise WorkflowError(
                "Seedream 在等待原服装三视图时连接中断，结果未知。已成功完成的原人物三视图会保留；"
                "系统没有自动重复付费请求，请手动确认后安全重试人物服装提取。"
            ) from exc
        job.update(stage="正在下载原服装三视图", progress=start + int(span * 0.82))
        download_file(result["url"], clothing_output)
        job.clothing_path = clothing_output
        job.log("原服装三视图已生成。")

    job.update(progress=start + span, recovery_action="")
    return person_output, clothing_output


def save_optional_reference_image(job: WebJob, field_name: str, stem: str) -> Path | None:
    upload = request.files.get(field_name)
    if upload is None or not upload.filename:
        return None
    path = save_upload(upload, job.run_dir, stem)
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
        raise WorkflowError("参考图只支持 JPG、PNG、WEBP 或 BMP。")
    return path


def run_clothing_reference_extraction(
    job: WebJob,
    source: Path,
    *,
    prompt: str,
    model: str,
    start: int,
    span: int,
) -> Path:
    """Extract the original wardrobe without generating an unnecessary person board."""
    job.update(status="running", stage="正在从原片提取原服装参考", progress=start)
    frames = extract_scene_reference_frames(source, job.run_dir, count=3)
    enforced_prompt = (
        f"{prompt.strip() or DEFAULT_CLOTHING_TRIVIEW_PROMPT}\n"
        f"{CLOTHING_TRIVIEW_REQUIRED_CONSTRAINT}"
    )
    try:
        result = api_client().generate_image(
            prompt=enforced_prompt,
            image_sources=[str(path) for path in frames],
            model=model.strip() or DEFAULT_SEEDREAM_MODEL,
            size="2K",
            watermark=False,
        )
    except ArkConnectionError as exc:
        raise WorkflowError(
            "Seedream 5.0 提取原服装时网络连接中断；已保存原片和已完成素材，未自动重复付费提交。"
        ) from exc
    output = job.run_dir / "original_clothing_triview.jpg"
    job.update(stage="正在下载原服装参考", progress=start + int(span * 0.82))
    download_file(result["url"], output)
    job.clothing_path = output
    job.update(progress=start + span)
    job.log("原服装参考已由 Seedream 5.0 提取完成。")
    return output


def normalize_wardrobe_source_duration(job: WebJob, source: Path) -> Path:
    """Keep wardrobe-swap source material within the 15-second product limit."""
    info = inspect_video(source)
    if info.duration <= WARDROBE_MAX_SOURCE_SECONDS + 1e-6:
        job.log(f"原片时长 {info.duration:.2f} 秒，无需裁剪。")
        return source
    job.update(stage="原片超过 15 秒，正在自动截取前 15 秒", progress=1)
    clipped = conform_video_duration(
        source,
        job.run_dir / "reference_first_15s.mp4",
        WARDROBE_MAX_SOURCE_SECONDS,
        with_audio=False,
    )
    clipped_info = inspect_video(clipped)
    if clipped_info.duration > WARDROBE_MAX_SOURCE_SECONDS + 0.08:
        raise WorkflowError(
            f"原片自动裁剪后仍超过 15 秒，当前 {clipped_info.duration:.2f} 秒。"
        )
    job.log(
        f"原片时长 {info.duration:.2f} 秒，已自动截取前 {clipped_info.duration:.2f} 秒；"
        "后续打码、白膜、素材提取和成片均只使用该片段。"
    )
    return clipped


def conform_wardrobe_seedance_reference_image(
    source: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    """Make one local wardrobe reference image satisfy Seedance hard limits.

    The image is never cropped. Oversized images are scaled proportionally and
    invalid aspect ratios are corrected by adding a neutral border sampled from
    the source edges. Ark ``asset://`` references are already validated when
    they enter the character library and therefore never pass through here.
    """
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise WorkflowError(f"找不到衣装智换参考图：{source_path}")
    image = _read_reference_image(source_path)
    if image is None or image.size == 0:
        raise WorkflowError(f"无法读取衣装智换参考图：{source_path.name}")

    original_height, original_width = image.shape[:2]
    working = image
    height, width = original_height, original_width

    # First keep both edges under the 6000 px API ceiling.
    downscale = min(1.0, 6000.0 / max(width, height))
    if downscale < 1.0:
        width = max(1, int(round(width * downscale)))
        height = max(1, int(round(height * downscale)))
        working = cv2.resize(working, (width, height), interpolation=cv2.INTER_AREA)

    # Upscale genuinely small images only when doing so cannot break the upper
    # edge limit. Extremely narrow/wide images are padded in the next step.
    upscale = max(1.0, 300.0 / width, 300.0 / height)
    if upscale > 1.0 and max(width * upscale, height * upscale) <= 6000.0:
        width = max(1, int(round(width * upscale)))
        height = max(1, int(round(height * upscale)))
        working = cv2.resize(working, (width, height), interpolation=cv2.INTER_CUBIC)

    target_width = max(300, width)
    target_height = max(300, height)
    if target_width / target_height > 2.5:
        target_height = max(target_height, int(math.ceil(target_width / 2.5)))
    elif target_width / target_height < 0.4:
        target_width = max(target_width, int(math.ceil(target_height * 0.4)))

    dimensions_changed = (
        width != original_width
        or height != original_height
        or target_width != width
        or target_height != height
    )
    must_reencode = source_path.stat().st_size >= 30 * 1024 * 1024
    if not dimensions_changed and not must_reencode:
        return {
            "path": source_path,
            "changed": False,
            "original_width": original_width,
            "original_height": original_height,
            "width": original_width,
            "height": original_height,
        }

    if target_width != width or target_height != height:
        edge_pixels = np.concatenate(
            (working[0, :, :], working[-1, :, :], working[:, 0, :], working[:, -1, :]),
            axis=0,
        )
        background = np.median(edge_pixels, axis=0).astype(np.uint8)
        canvas = np.empty((target_height, target_width, 3), dtype=np.uint8)
        canvas[:, :] = background
        left = (target_width - width) // 2
        top = (target_height - height) // 2
        canvas[top : top + height, left : left + width] = working
        working = canvas

    target = Path(output_path).expanduser().resolve().with_suffix(".jpg")
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded: np.ndarray | None = None
    for quality in (95, 92, 88, 84, 80):
        ok, candidate = cv2.imencode(".jpg", working, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok or candidate is None:
            continue
        encoded = candidate
        if candidate.nbytes < 30 * 1024 * 1024:
            break
    if encoded is None or encoded.nbytes >= 30 * 1024 * 1024:
        raise WorkflowError(f"参考图自动规范后仍超过 30 MB：{source_path.name}")
    try:
        target.write_bytes(encoded.tobytes())
    except OSError as exc:
        raise WorkflowError(f"无法保存规范后的参考图：{target.name}（{exc}）") from exc

    final_height, final_width = working.shape[:2]
    final_ratio = final_width / final_height
    if not (
        300 <= final_width <= 6000
        and 300 <= final_height <= 6000
        and 0.4 <= final_ratio <= 2.5
    ):
        raise WorkflowError(
            "参考图自动规范失败："
            f"{final_width}×{final_height}，宽高比 {final_ratio:.3f}。"
        )
    return {
        "path": target,
        "changed": True,
        "original_width": original_width,
        "original_height": original_height,
        "width": final_width,
        "height": final_height,
    }


def prepare_wardrobe_seedance_reference(
    job: WebJob,
    source: str | Path,
    *,
    role: str,
    label: str,
) -> Path:
    """Conform and report one local reference before any paid video request."""
    result = conform_wardrobe_seedance_reference_image(
        source,
        job.run_dir / f"seedance_{role}_reference.jpg",
    )
    path = Path(result["path"])
    if result["changed"]:
        job.log(
            f"{label}参考图尺寸 {result['original_width']}×{result['original_height']} 不符合 "
            "Seedance 输入限制，已自动等比缩放/扩边为 "
            f"{result['width']}×{result['height']}；画面内容未裁剪。"
        )
    else:
        job.log(
            f"{label}参考图已通过 Seedance 尺寸检查："
            f"{result['width']}×{result['height']}。"
        )
    return path


def build_wardrobe_white_model_options(source: Path, source_duration: float) -> dict[str, Any]:
    """Build Seedance 2.0 / 480p controls with a wardrobe-only safe mannequin prompt."""
    ratio = resolved_seedance_ratio("adaptive", source)
    if not ratio:
        raise WorkflowError("无法识别原片画面比例，不能提交 Seedance 2.0 白膜任务。")
    generation_duration = match_seedance_cover_duration(source_duration)
    prompt = WARDROBE_SAFE_WHITE_MODEL_PROMPT
    if source_duration < generation_duration - 0.02:
        prompt = f"{prompt}\n{seedance_hold_timing_prompt(source_duration, generation_duration)}"
    return {
        "generation_channel": "api",
        "prompt": prompt,
        "model": DEFAULT_SEEDANCE_MODEL,
        "resolution": "480p",
        "ratio": ratio,
        "duration": generation_duration,
        "generate_audio": False,
        "watermark": False,
        "delete_tos_after": True,
        "reference_upload_strategy": "stable",
    }


def run_wardrobe_face_mosaic(job: WebJob, source: Path, *, start: int = 0, span: int = 100) -> Path:
    """Create only the local face-masked clip used by 衣装智换."""
    info = inspect_video(source)
    if info.duration > WARDROBE_MAX_SOURCE_SECONDS + 0.08:
        raise WorkflowError("衣装智换素材必须先自动裁剪到 15 秒以内。")
    mosaic = job.run_dir / "face_mosaic.mp4"
    job.update(status="running", stage="正在检测并跟踪原片全部人脸", progress=start)

    def on_mosaic_progress(current: int, total: int, maximum_faces: int) -> None:
        job.update(
            stage=f"正在生成人脸打码视频 · 当前最多 {maximum_faces} 张脸",
            progress=start + int(span * current / max(total, 1)),
        )

    stats = render_face_mosaic_video(
        source,
        mosaic,
        block_size=22,
        score_threshold=0.72,
        on_progress=on_mosaic_progress,
        on_log=job.log,
    )
    job.mosaic_path = mosaic
    job.log(
        f"人脸打码完成：{stats.get('frames_with_faces', 0)}/{stats.get('frames', 0)} 帧检测到人脸。"
    )
    job.update(source_duration=info.duration, progress=start + span)
    return mosaic


def run_wardrobe_white_model(job: WebJob, source: Path, *, start: int = 0, span: int = 70) -> Path:
    """Use an existing face-masked clip to create the white-model master.

    The legacy combined endpoint may still call this without a mosaic. In that
    case the local mosaic is created once for backward compatibility. The new
    wardrobe UI calls the two stages separately and therefore never pays for a
    white model before the user has previewed the mosaic.
    """
    info = inspect_video(source)
    if info.duration > WARDROBE_MAX_SOURCE_SECONDS + 0.08:
        raise WorkflowError("衣装智换素材必须先自动裁剪到 15 秒以内。")
    mosaic = job.mosaic_path if job.mosaic_path and job.mosaic_path.is_file() else None
    if mosaic is None:
        mosaic_span = max(1, int(span * 0.25))
        mosaic = run_wardrobe_face_mosaic(job, source, start=start, span=mosaic_span)
    else:
        job.log("已复用人工可预览的人脸打码视频，不会重复执行本地打码。")
    clean_mosaic, text_cleanup = sanitize_video_visible_text_if_needed(
        mosaic,
        job.run_dir / "face_mosaic_no_overlay_text.mp4",
        preserve_audio=False,
        overlay_only=True,
    )
    if text_cleanup.get("changed"):
        job.log("已在本地清除打码视频中的字幕和叠加文字后再生成白膜；场景内自然文字不受影响。")
    silent_reference = strip_video_audio(clean_mosaic, job.run_dir / "face_mosaic_silent.mp4")
    white_reference = silent_reference
    generation_duration = match_seedance_cover_duration(info.duration)
    if inspect_video(silent_reference).duration < generation_duration - 0.02:
        white_reference = extend_video_with_trailing_hold(
            silent_reference,
            job.run_dir / "face_mosaic_seedance_timed.mp4",
            target_duration=float(generation_duration),
            with_audio=False,
        )
        job.log(
            f"原片动作保持 {info.duration:.2f} 秒不变，仅将结束姿势定格补足到 "
            f"{generation_duration} 秒供 Seedance 2.0 生成。"
        )

    white_dir = job.run_dir / "white_model_task"
    white_dir.mkdir(parents=True, exist_ok=True)
    white_job = WebJob(
        id=f"{job.id}-white",
        kind="wardrobe_white_model",
        project=WARDROBE_SWAP_PROJECT,
        run_dir=white_dir,
        depth_path=white_reference,
    )
    with JOBS_LOCK:
        JOBS[white_job.id] = white_job
    white_options = build_wardrobe_white_model_options(source, info.duration)
    job.update(stage="Seedance 2.0 · 480p 正在生成白膜动作母版", progress=start + max(1, int(span * 0.08)))
    run_generation(
        white_job,
        depth_path=white_reference,
        depth_reference="",
        person_source="",
        clothing_source="",
        scene_source="",
        options=white_options,
        progress_start=5,
        video_only=True,
    )
    if white_job.status != "succeeded" or not white_job.output_path or not white_job.output_path.is_file():
        raise WorkflowError(white_job.error or "白膜视频生成未完成。")
    white_output = conform_video_duration(
        white_job.output_path,
        job.run_dir / "white_model.mp4",
        info.duration,
        with_audio=False,
    )
    job.white_model_path = white_output
    job.update(
        source_duration=info.duration,
        depth_duration=inspect_video(white_output).duration,
        generation_duration=generation_duration,
        progress=start + span,
    )
    job.log("白膜视频已校正回原片时长，可作为最终成片的动作、构图和运镜母版。")
    return white_output


def build_clothing_only_prompt(custom_prompt: str = "") -> str:
    prompt = custom_prompt.strip() or DEFAULT_CLOTHING_ONLY_PROMPT
    if CLOTHING_ONLY_GENERATION_REQUIRED_CONSTRAINT not in prompt:
        prompt = f"{prompt}\n{CLOTHING_ONLY_GENERATION_REQUIRED_CONSTRAINT}"
    return prompt


def project_five_person_reference_is_current(job: WebJob) -> bool:
    version_file = job.run_dir / "original_person_triview.version"
    try:
        return version_file.read_text(encoding="utf-8").strip() == CLOTHING_PERSON_TRIVIEW_VERSION
    except OSError:
        return False


def save_new_clothing(job: WebJob, *, reuse_job: WebJob | None = None) -> str:
    upload = request.files.get("clothing_image")
    if upload is not None and upload.filename:
        path = save_upload(upload, job.run_dir, "new_clothing")
    elif reuse_job and reuse_job.clothing_path and reuse_job.clothing_path.is_file():
        path = reuse_job.clothing_path
        job.log("已复用项目 5 保存的新服装参考图。")
    else:
        raise WorkflowError("缺少需要替换的新服装参考图。")
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
        raise WorkflowError("新服装参考必须是 JPG、PNG、WEBP 或 BMP 图片。")
    job.clothing_path = path
    return str(path)


def run_clothing_only_extraction(
    job: WebJob,
    source: Path,
    *,
    person_prompt: str,
    scene_prompt: str,
    model: str,
    start: int = 0,
    span: int = 100,
    reuse_existing: bool = False,
) -> tuple[Path, Path]:
    person_span = max(1, int(span * 0.52))
    project_person_prompt = person_prompt.strip() or DEFAULT_CLOTHING_PERSON_TRIVIEW_PROMPT
    person = run_person_triview_extraction(
        job,
        source,
        prompt=project_person_prompt,
        model=model,
        start=start,
        span=person_span,
        reuse_existing=reuse_existing,
        recovery_action="retry_clothing_references",
        required_constraint=CLOTHING_PERSON_TRIVIEW_REQUIRED_CONSTRAINT,
        artifact_version=CLOTHING_PERSON_TRIVIEW_VERSION,
    )
    existing_scene = job.run_dir / "scene_reference.jpg"
    if reuse_existing and existing_scene.is_file() and existing_scene.stat().st_size > 0:
        job.scene_path = existing_scene
        job.log("已复用上次成功生成的原片干净场景。")
        scene = existing_scene
    else:
        scene = run_scene_extraction(
            job,
            source,
            prompt=scene_prompt,
            model=model,
            start=start + person_span,
            span=span - person_span,
            recovery_action="retry_clothing_references",
        )
    job.update(progress=start + span, recovery_action="")
    return person, scene


def save_reference_images(job: WebJob) -> tuple[str, str, str]:
    person_asset = request.form.get("person_asset", "").strip()
    if person_asset:
        if not person_asset.startswith("asset://"):
            raise WorkflowError("真人授权素材 ID 必须以 asset:// 开头。")
        person_source = person_asset
    else:
        person_source = str(save_upload(request.files.get("person_image"), job.run_dir, "person"))
    clothing_source = str(save_upload(request.files.get("clothing_image"), job.run_dir, "clothing"))
    scene_source = str(save_upload(request.files.get("scene_image"), job.run_dir, "scene"))
    return person_source, clothing_source, scene_source


def build_multi_prompt(role_descriptions: list[str]) -> str:
    if not 2 <= len(role_descriptions) <= 4:
        raise WorkflowError("多人复刻当前支持 2–4 位人物。")
    clauses = ["参考@视频1"]
    for index, raw_description in enumerate(role_descriptions, start=1):
        description = " ".join(raw_description.strip().split())
        if not description:
            description = f"原片中的人物{index}"
        if len(description) > 200:
            raise WorkflowError(f"人物 {index} 的原片角色定位不能超过 200 个字符。")
        person_image = 2 * index - 1
        clothing_image = 2 * index
        clauses.append(
            f"将原视频中的「{description}」替换为@图片{person_image}中的人物形象，"
            f"该人物的服装严格参考@图片{clothing_image}"
        )
    scene_image = len(role_descriptions) * 2 + 1
    clauses.append(f"将视频背景更换为@图片{scene_image}中的场景")
    clauses.append(
        "完全复刻原视频中所有人物各自的全部动作、表情、视线、站位和人物之间的互动关系；"
        "精准匹配每个人的脚步、抬手、摆臂、躯干倾斜、弹跳重心变化、转向、停顿、接触动作与定格姿势；"
        "所有动作的起始时间、落点、强拍定格和节奏变化均与原视频帧级对齐，镜头运镜与画面构图完全复刻原视频；"
        "人物身份必须从头到尾分别保持一致，禁止串脸、互换服装、合并肢体、增加人物或遗漏人物；"
        "深度视频只用于空间、动作、遮挡和镜头约束，不要生成黑白深度图风格，最终输出正常彩色视频"
    )
    return "；".join(clauses) + "。"


def save_multi_reference_images(
    job: WebJob,
) -> tuple[list[tuple[str, str]], str, list[str]]:
    actor_count = form_int("actor_count", 2)
    if not 2 <= actor_count <= 4:
        raise WorkflowError("多人复刻当前支持 2–4 位人物。")
    character_sources: list[tuple[str, str]] = []
    role_descriptions: list[str] = []
    default_roles = ["画面左侧人物", "画面右侧人物", "画面中间人物", "画面后方人物"]
    for index in range(1, actor_count + 1):
        role = request.form.get(f"role_description_{index}", "").strip() or default_roles[index - 1]
        role_descriptions.append(role)
        person_asset = request.form.get(f"person_asset_{index}", "").strip()
        if person_asset:
            if not person_asset.startswith("asset://"):
                raise WorkflowError(f"人物 {index} 的授权素材 ID 必须以 asset:// 开头。")
            person_source = person_asset
        else:
            person_source = str(
                save_upload(request.files.get(f"person_image_{index}"), job.run_dir, f"person_{index}")
            )
        clothing_source = str(
            save_upload(request.files.get(f"clothing_image_{index}"), job.run_dir, f"clothing_{index}")
        )
        character_sources.append((person_source, clothing_source))
    scene_source = str(save_upload(request.files.get("scene_image"), job.run_dir, "scene"))
    return character_sources, scene_source, role_descriptions


def generation_options() -> dict[str, Any]:
    return {
        "generation_channel": request.form.get("generation_channel", "api").strip() or "api",
        "prompt": request.form.get("prompt", DEFAULT_PROMPT).strip() or DEFAULT_PROMPT,
        "model": request.form.get("model", DEFAULT_SEEDANCE_MODEL).strip() or DEFAULT_SEEDANCE_MODEL,
        "resolution": request.form.get("resolution", "720p"),
        "ratio": request.form.get("ratio", "adaptive"),
        "duration": form_int("duration", 15),
        "generate_audio": form_bool("generate_audio"),
        "watermark": form_bool("watermark"),
        "delete_tos_after": form_bool("delete_tos_after", True),
    }


def validate_real_final_video_options(options: dict[str, Any]) -> None:
    model = str(options.get("model") or "").strip()
    resolution = str(options.get("resolution") or "").strip()
    spec = REAL_FINAL_VIDEO_MODELS.get(model)
    if spec is None:
        raise WorkflowError(
            "真实人物成片只允许选择已接入的 Seedance 2.0 或 Seedance 2.5 模型。"
        )
    if resolution not in spec["resolutions"]:
        allowed = "、".join(spec["resolutions"])
        raise WorkflowError(f"{spec['label']} 只支持 {allowed}；当前选择为 {resolution or '空'}。")


def normalize_real_final_video_options(
    options: dict[str, Any],
    *,
    video_editing: bool = False,
) -> dict[str, Any]:
    """Use Seedance 2.5's required controls whenever a white-model video is edited."""
    normalized = dict(options)
    if str(normalized.get("model") or "").strip() == DEFAULT_SEEDANCE_25_MODEL:
        normalized["ratio"] = "adaptive"
        if video_editing:
            normalized["duration"] = -1
    return normalized


def run_web_generation(
    job: WebJob,
    *,
    depth_path: Path | None,
    person_source: str,
    clothing_source: str,
    scene_source: str,
    options: dict[str, Any],
    progress_start: int = 0,
) -> None:
    try:
        if depth_path is None or not depth_path.is_file():
            raise WorkflowError("Seedance 网页直传需要本地深度视频文件。")
        validate_seedance_reference_video(depth_path)
        job.update(
            status="running",
            stage="正在向 Seedance 网页上传本地素材",
            progress=max(progress_start, 58),
        )
        job.log("正在上传深度视频、人物图、服装图和场景图到 Seedance 官方网页。")
        last_reported_stage = ""

        def report_web_progress(progress: int, stage: str) -> None:
            nonlocal last_reported_stage
            job.update(status="running", stage=stage, progress=max(progress_start, progress))
            if stage != last_reported_stage:
                job.log(f"{stage}（总进度 {max(progress_start, progress)}%）")
                last_reported_stage = stage

        result = SEEDANCE_WEB.prepare_materials(
            depth_video=depth_path,
            person_image=person_source,
            clothing_image=clothing_source,
            scene_image=scene_source,
            prompt=options["prompt"],
            submit=True,
            wait_for_result=True,
            output_dir=job.run_dir,
            on_progress=report_web_progress,
        )
        if not result.get("submitted"):
            raise WorkflowError("Seedance 网页素材已准备，但生成任务尚未提交。")
        job.task_id = str(result.get("task_id") or "")
        job.log("Seedance 网页已接收全部本地素材和提示词，生成任务已提交。")
        output_path_text = str(result.get("output_path") or "").strip()
        if output_path_text:
            output_path = Path(output_path_text).expanduser().resolve()
            if not output_path.is_file():
                raise WorkflowError("Seedance 显示生成完成，但本地未找到下载的成片。")
            job.output_path = output_path
            job.update(status="succeeded", stage="成片已自动下载到本机", progress=100)
            job.log(f"最终成片已保存：{output_path.name}")
            return
        job.update(
            status="submitted",
            stage=(
                "等待成片超时；Seedance 任务仍可能在专用窗口继续生成"
                if result.get("timed_out")
                else "Seedance 已开始生成，请在专用窗口查看成片"
            ),
            progress=95 if result.get("timed_out") else 62,
        )
    except Exception as exc:
        job_error(job, exc)
        if job.task_id:
            persist_cloud_job(job, status="failed", error=str(exc))


def run_generation(
    job: WebJob,
    *,
    depth_path: Path | None,
    depth_reference: str,
    person_source: str,
    clothing_source: str,
    scene_source: str,
    options: dict[str, Any],
    progress_start: int = 0,
    video_only: bool = False,
    on_finished: Callable[[WebJob], None] | None = None,
) -> None:
    uploaded_key = ""
    store: TosMediaStore | None = None
    free_file_id = ""
    free_store: TempFileMediaStore | None = None
    stable_reference: SeedanceVideoReferenceSource | None = None
    try:
        client = api_client()
        job.update(stage="正在检查火山方舟 API 连接", progress=max(progress_start, 48))
        job.log("正在进行只读 API 连接预检；临时网络错误会安全重试。")
        credential_message = client.check_credentials()
        job.log(credential_message)

        if not depth_reference:
            if depth_path is None:
                raise WorkflowError("缺少深度视频文件或公网 URL。")
            info = validate_seedance_reference_video(depth_path)
            if options.get("reference_upload_strategy") == "stable":
                job.update(stage="正在建立衣装智换稳定视频通道", progress=max(progress_start, 52))
                stable_reference = prepare_seedance_stable_video_reference(
                    depth_path,
                    on_log=job.log,
                )
                depth_reference = stable_reference.url
            else:
                if info.size_bytes > MAX_FREE_UPLOAD_BYTES:
                    raise WorkflowError(
                        "纯 API 免费通道要求深度视频不超过 95 MB："
                        f"当前 {info.size_bytes / (1024 * 1024):.1f} MB。"
                    )
                job.update(stage="正在上传深度视频到免费临时通道", progress=max(progress_start, 52))
                job.log("正在将深度视频上传到 1 小时自动过期的临时公开直链。")
                try:
                    free_store = TempFileMediaStore()
                    uploaded = free_store.upload_video(depth_path, expires_hours=1)
                    depth_reference = uploaded.signed_url
                    free_file_id = uploaded.object_key
                    job.log("临时 MP4 直链已生成并通过公网可访问性检查。")
                except Exception as free_exc:
                    free_store = None
                    if not TosMediaStore.configured():
                        raise WorkflowError(
                            "免费临时视频服务当前不可用，纯 API 任务尚未提交，稍后重试即可。"
                            f"详情：{free_exc}"
                        ) from free_exc
                    job.log(f"免费临时服务不可用，自动切换已配置的 TOS：{free_exc}")
                    try:
                        job.update(stage="正在上传深度视频到 TOS", progress=max(progress_start, 52))
                        job.log("正在上传深度视频到私有 TOS。")
                        store = TosMediaStore()
                        uploaded = store.upload_video(depth_path)
                        depth_reference = uploaded.signed_url
                        uploaded_key = uploaded.object_key
                        job.log("TOS 上传完成，临时签名 URL 已生成。")
                    except Exception as exc:
                        store = None
                        raise WorkflowError(f"免费临时服务和 TOS 均不可用：{exc}") from exc

        if video_only:
            payload = build_video_reference_seedance_payload(
                prompt=options["prompt"],
                video_reference=depth_reference,
                model=options["model"],
                resolution=options["resolution"],
                ratio=options["ratio"],
                duration=options["duration"],
                generate_audio=options["generate_audio"],
                watermark=options["watermark"],
            )
        elif person_source or clothing_source:
            if not person_source or not clothing_source:
                raise WorkflowError("人物图和服装图必须同时提供。")
            payload = build_seedance_payload(
                prompt=options["prompt"],
                person_source=person_source,
                clothing_source=clothing_source,
                scene_source=scene_source,
                depth_video_reference=depth_reference,
                model=options["model"],
                resolution=options["resolution"],
                ratio=options["ratio"],
                duration=options["duration"],
                generate_audio=options["generate_audio"],
                watermark=options["watermark"],
            )
        else:
            payload = build_scene_seedance_payload(
                prompt=options["prompt"],
                scene_source=scene_source,
                depth_video_reference=depth_reference,
                model=options["model"],
                resolution=options["resolution"],
                ratio=options["ratio"],
                duration=options["duration"],
                generate_audio=options["generate_audio"],
                watermark=options["watermark"],
            )
        expected_ratio = resolved_seedance_ratio(options["ratio"], depth_path)
        seedance_label = (
            "Seedance 2.5"
            if str(options.get("model") or "").strip() == DEFAULT_SEEDANCE_25_MODEL
            else "Seedance 2.0"
        )
        job.update(stage=f"正在提交 {seedance_label} 任务", progress=max(progress_start, 58))
        known_task_ids: set[str] = set()
        try:
            known_task_ids = {
                str(item.get("id") or "").strip()
                for item in client.list_tasks(page_size=50)
                if str(item.get("id") or "").strip()
            }
        except ArkAPIError:
            # Some regional deployments authenticate correctly but do not expose task lists.
            pass
        created_after = time.time()
        job.update(task_id="", cloud_status="", error="", recovery_action="")
        persist_cloud_job(
            job,
            status="submitting",
            submitted_at=created_after,
            model=options["model"],
            resolution=options["resolution"],
            requested_ratio=options["ratio"],
            expected_ratio=expected_ratio,
            duration=options["duration"],
            generate_audio=options["generate_audio"],
            requested_signature=str(options.get("requested_signature") or ""),
            known_task_ids=sorted(known_task_ids),
            error="",
        )
        job.log("正在提交付费任务；创建请求只发送一次，避免重复计费。")
        try:
            task_id = client.create_task(payload)
        except ArkConnectionError as exc:
            created_before = time.time()
            job.update(stage="提交响应中断，正在安全确认任务是否已创建", progress=max(progress_start, 60))
            job.log("创建连接中断；正在通过只读任务列表寻找刚刚创建的任务，不会重复提交。")
            task_id = client.recover_created_task(
                known_task_ids,
                model=options["model"],
                created_after=created_after,
                created_before=created_before,
                resolution=options["resolution"],
                ratio=expected_ratio,
                duration=options["duration"],
                generate_audio=options["generate_audio"],
                attempts=12,
                poll_interval=5.0,
            )
            if task_id:
                job.log(f"已从方舟任务列表找回创建成功的任务：{task_id}")
            else:
                job.update(recovery_action="recover_seedance_submission")
                persist_cloud_job(
                    job,
                    status="ambiguous",
                    submitted_at=created_after,
                    created_before=created_before,
                    model=options["model"],
                    resolution=options["resolution"],
                    requested_ratio=options["ratio"],
                    expected_ratio=expected_ratio,
                    duration=options["duration"],
                    generate_audio=options["generate_audio"],
                    known_task_ids=sorted(known_task_ids),
                    recovery_action="recover_seedance_submission",
                    error=str(exc),
                )
                raise WorkflowError(
                    "提交响应因网络中断而无法确认，且任务列表中未找到唯一匹配的新任务。"
                    "为避免重复计费，系统没有自动再次提交；请先在下方用方舟任务 ID 恢复查询，"
                    f"或确认任务列表后再重试。原始错误：{exc}"
                ) from exc
        submitted_at = time.time()
        job.update(
            task_id=task_id,
            progress=max(progress_start, 63),
            cloud_status="queued",
            cloud_started_at=submitted_at,
            cloud_updated_at=submitted_at,
            recovery_action="",
        )
        job.log(f"Seedance 任务已提交：{task_id}")
        persist_cloud_job(
            job,
            status="running",
            model=options["model"],
            resolution=options["resolution"],
            ratio=options["ratio"],
            prompt=options["prompt"],
            duration=options["duration"],
            generate_audio=options["generate_audio"],
            requested_signature=str(options.get("requested_signature") or ""),
        )

        def on_status(task: dict[str, Any]) -> None:
            status = str(task.get("status") or "unknown")
            progress = 65 if status == "queued" else 68 if status == "running" else 89
            job.update(
                stage=f"Seedance 状态：{status}",
                progress=max(progress_start, progress),
                cloud_status=status,
                cloud_updated_at=time.time(),
            )
            job.log(f"Seedance 状态更新：{status}")
            persist_cloud_job(job, status="running")

        task = wait_for_seedance_task_with_real_pause(
            job,
            client,
            task_id,
            on_status=on_status,
        )
        video_url = str((task.get("content") or {}).get("video_url") or "")
        if not video_url:
            raise WorkflowError("任务成功，但响应中没有成片 URL。")
        output = job.run_dir / f"生成成片_{task_id}.mp4"
        job.update(stage="正在下载生成成片", progress=90, cloud_status="succeeded")

        def on_download(downloaded: int, total: int) -> None:
            if total > 0:
                job.update(progress=min(99, 90 + int(9 * downloaded / total)))

        download_file(
            video_url,
            output,
            on_progress=on_download,
            on_retry=lambda attempt, total, error: job.log(
                f"Seedance 成片下载连接中断，正在从断点自动重试 {attempt}/{total}：{error}"
            ),
        )
        job.output_path = output
        save_job_record(
            job.run_dir,
            {
                "local_job_id": job.id,
                "kind": job.kind,
                "project": job.project,
                "task_id": task_id,
                "status": "succeeded",
                "cloud_status": "succeeded",
                "output": str(output),
                "depth": str(job.depth_path) if job.depth_path else "",
                "scene": str(job.scene_path) if job.scene_path else "",
                "person": str(job.person_path) if job.person_path else "",
                "clothing": str(job.clothing_path) if job.clothing_path else "",
                "submitted_at": job.cloud_started_at,
                "created_at": job.created_at,
                "model": options["model"],
                "resolution": options["resolution"],
                "ratio": options["ratio"],
                "duration": options["duration"],
                "prompt": options["prompt"],
                "requested_signature": str(options.get("requested_signature") or ""),
                "usage": task.get("usage"),
            },
        )
        job.log(f"成片已保存：{output.name}")
        job.update(status="succeeded", stage="全部完成", progress=100, cloud_status="succeeded")
    except RealLongGenerationPaused:
        job.update(
            status="paused",
            stage="已暂停本地跟进；云端任务仍可能完成",
            error="",
        )
        job.log(f"已暂停本地跟进 Seedance 任务：{job.task_id or '尚未返回任务ID'}。")
        if job.task_id:
            persist_cloud_job(job, status="paused")
    except Exception as exc:
        job_error(job, exc)
        if job.task_id:
            persist_cloud_job(job, status="failed", error=str(exc))
        elif job.recovery_action != "recover_seedance_submission":
            # A pre-submit/upload/validation failure must replace the temporary
            # `submitting` marker. Otherwise a restart revives a dead task as a
            # permanent fake-running job and blocks every retry.
            persist_cloud_job(job, status="failed", error=str(exc))
    finally:
        if stable_reference is not None:
            stable_channel = stable_reference.channel
            stable_reference.close(
                delete_remote=bool(options.get("delete_tos_after")) and job.status != "paused"
            )
            if stable_channel == "tos":
                job.log("衣装智换 TOS 临时视频已清理。")
            else:
                job.log("衣装智换项目一次性加密视频通道已关闭。")
        if free_file_id and options.get("delete_tos_after") and free_store is not None and job.status != "paused":
            try:
                free_store.delete(free_file_id)
                job.log("免费临时深度视频已立即删除。")
            except Exception as exc:
                job.log(f"临时视频未能立即删除，将在 1 小时后自动过期：{exc}")
        if uploaded_key and options.get("delete_tos_after") and store is not None and job.status != "paused":
            try:
                store.delete(uploaded_key)
                job.log("TOS 临时深度视频已删除。")
            except Exception as exc:
                job.log(f"TOS 临时文件未能自动删除：{exc}")
        if on_finished is not None:
            try:
                on_finished(job)
            except Exception as exc:
                # The paid generation result must remain usable even if the local
                # project index cannot be updated at this exact moment.
                job.log(f"成片任务已经结束，但项目存档回写失败：{exc}")


def run_multi_generation(
    job: WebJob,
    *,
    depth_path: Path | None,
    depth_reference: str,
    character_sources: list[tuple[str, ...]],
    scene_source: str,
    options: dict[str, Any],
    progress_start: int = 0,
    include_scene_reference: bool = True,
) -> None:
    uploaded_key = ""
    store: TosMediaStore | None = None
    free_file_id = ""
    free_store: TempFileMediaStore | None = None
    try:
        client = api_client()
        job.update(status="running", stage="正在检查火山方舟 API 连接", progress=max(progress_start, 48))
        job.log("正在进行只读 API 连接预检；创建付费任务只会提交一次。")
        job.log(client.check_credentials())

        if not depth_reference:
            if depth_path is None:
                raise WorkflowError("缺少深度视频文件或公网 URL。")
            info = validate_seedance_reference_video(depth_path)
            if info.size_bytes > MAX_FREE_UPLOAD_BYTES:
                raise WorkflowError(
                    "纯 API 免费通道要求深度视频不超过 195 MB；"
                    f"当前 {info.size_bytes / (1024 * 1024):.1f} MB。"
                )
            job.update(stage="正在上传深度视频到免费临时通道", progress=max(progress_start, 52))
            job.log("正在生成 1 小时自动过期的临时公开 MP4 地址。")
            try:
                free_store = TempFileMediaStore()
                uploaded = free_store.upload_video(depth_path, expires_hours=1)
                depth_reference = uploaded.signed_url
                free_file_id = uploaded.object_key
                job.log("临时 MP4 地址已生成并通过公网可访问性检查。")
            except Exception as free_exc:
                free_store = None
                if not TosMediaStore.configured():
                    raise WorkflowError(
                        "免费临时视频服务当前不可用，付费任务尚未提交，请稍后重试。"
                        f"详情：{free_exc}"
                    ) from free_exc
                job.log(f"免费临时服务不可用，自动切换到已配置的 TOS：{free_exc}")
                try:
                    job.update(stage="正在上传深度视频到 TOS", progress=max(progress_start, 52))
                    store = TosMediaStore()
                    uploaded = store.upload_video(depth_path)
                    depth_reference = uploaded.signed_url
                    uploaded_key = uploaded.object_key
                    job.log("TOS 上传完成，临时签名地址已生成。")
                except Exception as exc:
                    store = None
                    raise WorkflowError(f"免费临时服务和 TOS 均不可用：{exc}") from exc

        payload = build_multi_seedance_payload(
            prompt=options["prompt"],
            character_sources=character_sources,
            scene_source=scene_source,
            depth_video_reference=depth_reference,
            model=options["model"],
            resolution=options["resolution"],
            ratio=options["ratio"],
            duration=options["duration"],
            generate_audio=options["generate_audio"],
            watermark=options["watermark"],
            include_scene_reference=include_scene_reference,
        )
        expected_ratio = resolved_seedance_ratio(options["ratio"], depth_path)
        seedance_label = (
            "Seedance 2.5"
            if str(options.get("model") or "").strip() == DEFAULT_SEEDANCE_25_MODEL
            else "Seedance 2.0"
        )
        job.update(stage=f"正在提交 {seedance_label} 多人复刻任务", progress=max(progress_start, 58))
        known_task_ids: set[str] = set()
        try:
            known_task_ids = {
                str(item.get("id") or "").strip()
                for item in client.list_tasks(page_size=50)
                if str(item.get("id") or "").strip()
            }
        except ArkAPIError:
            pass
        created_after = time.time()
        persist_cloud_job(
            job,
            status="submitting",
            submitted_at=created_after,
            actor_count=len(character_sources),
            model=options["model"],
            resolution=options["resolution"],
            requested_ratio=options["ratio"],
            expected_ratio=expected_ratio,
            duration=options["duration"],
            generate_audio=options["generate_audio"],
            requested_signature=str(options.get("requested_signature") or ""),
            known_task_ids=sorted(known_task_ids),
        )
        job.log(
            f"正在提交 {len(character_sources)} 人复刻付费任务；"
            f"共使用 {sum(len(sources) for sources in character_sources) + (1 if include_scene_reference else 0)} "
            "张参考图和 1 段时空控制视频。"
        )
        try:
            task_id = client.create_task(payload)
        except ArkConnectionError as exc:
            created_before = time.time()
            job.update(
                stage="提交响应中断，正在安全确认任务是否已经创建",
                progress=max(progress_start, 60),
            )
            task_id = client.recover_created_task(
                known_task_ids,
                model=options["model"],
                created_after=created_after,
                created_before=created_before,
                resolution=options["resolution"],
                ratio=expected_ratio,
                duration=options["duration"],
                generate_audio=options["generate_audio"],
                attempts=12,
                poll_interval=5.0,
            )
            if task_id:
                job.log(f"已从方舟任务列表找回创建成功的任务：{task_id}")
            else:
                job.update(recovery_action="recover_seedance_submission")
                persist_cloud_job(
                    job,
                    status="ambiguous",
                    submitted_at=created_after,
                    created_before=created_before,
                    actor_count=len(character_sources),
                    model=options["model"],
                    resolution=options["resolution"],
                    requested_ratio=options["ratio"],
                    expected_ratio=expected_ratio,
                    duration=options["duration"],
                    generate_audio=options["generate_audio"],
                    known_task_ids=sorted(known_task_ids),
                    recovery_action="recover_seedance_submission",
                    error=str(exc),
                )
                raise WorkflowError(
                    "提交响应中断且无法确认任务是否创建。为避免重复计费，系统没有自动再次提交；"
                    f"原始错误：{exc}"
                ) from exc

        submitted_at = time.time()
        job.update(
            task_id=task_id,
            progress=max(progress_start, 63),
            cloud_status="queued",
            cloud_started_at=submitted_at,
            cloud_updated_at=submitted_at,
            recovery_action="",
        )
        job.log(f"Seedance 多人复刻任务已提交：{task_id}")
        persist_cloud_job(
            job,
            status="running",
            actor_count=len(character_sources),
            model=options["model"],
            resolution=options["resolution"],
            ratio=options["ratio"],
            prompt=options["prompt"],
            duration=options["duration"],
            generate_audio=options["generate_audio"],
            requested_signature=str(options.get("requested_signature") or ""),
        )

        def on_status(task: dict[str, Any]) -> None:
            status = str(task.get("status") or "unknown")
            progress = 65 if status == "queued" else 68 if status == "running" else 89
            job.update(
                stage=f"Seedance 状态：{status}",
                progress=max(progress_start, progress),
                cloud_status=status,
                cloud_updated_at=time.time(),
            )
            job.log(f"Seedance 状态更新：{status}")
            persist_cloud_job(job, status="running")

        task = wait_for_seedance_task_with_real_pause(
            job,
            client,
            task_id,
            on_status=on_status,
        )
        video_url = str((task.get("content") or {}).get("video_url") or "")
        if not video_url:
            raise WorkflowError("任务成功，但响应中没有最终成片 URL。")
        output = job.run_dir / f"多人复刻成片_{task_id}.mp4"
        job.update(stage="正在下载多人复刻成片", progress=90, cloud_status="succeeded")

        def on_download(downloaded: int, total: int) -> None:
            if total > 0:
                job.update(progress=min(99, 90 + int(9 * downloaded / total)))

        download_file(
            video_url,
            output,
            on_progress=on_download,
            on_retry=lambda attempt, total, error: job.log(
                f"多人复刻成片下载连接中断，正在从断点自动重试 {attempt}/{total}：{error}"
            ),
        )
        job.output_path = output
        save_job_record(
            job.run_dir,
            {
                "local_job_id": job.id,
                "kind": job.kind,
                "project": "multi_person_replication",
                "actor_count": len(character_sources),
                "task_id": task_id,
                "status": "succeeded",
                "cloud_status": "succeeded",
                "output": str(output),
                "depth": str(job.depth_path) if job.depth_path else "",
                "scene": str(job.scene_path) if job.scene_path else "",
                "submitted_at": job.cloud_started_at,
                "created_at": job.created_at,
                "model": options["model"],
                "resolution": options["resolution"],
                "ratio": options["ratio"],
                "duration": options["duration"],
                "prompt": options["prompt"],
                "requested_signature": str(options.get("requested_signature") or ""),
                "usage": task.get("usage"),
            },
        )
        job.log(f"多人复刻成片已保存：{output.name}")
        job.update(status="succeeded", stage="多人复刻全部完成", progress=100, cloud_status="succeeded")
    except RealLongGenerationPaused:
        job.update(
            status="paused",
            stage="已暂停本地跟进；云端任务仍可能完成",
            error="",
        )
        job.log(f"已暂停本地跟进 Seedance 多人任务：{job.task_id or '尚未返回任务ID'}。")
        if job.task_id:
            persist_cloud_job(job, status="paused")
    except Exception as exc:
        job_error(job, exc)
        if job.task_id:
            persist_cloud_job(job, status="failed", error=str(exc))
    finally:
        if free_file_id and options.get("delete_tos_after") and free_store is not None and job.status != "paused":
            try:
                free_store.delete(free_file_id)
                job.log("免费临时深度视频已删除。")
            except Exception as exc:
                job.log(f"临时视频将在 1 小时后自动过期：{exc}")
        if uploaded_key and options.get("delete_tos_after") and store is not None and job.status != "paused":
            try:
                store.delete(uploaded_key)
                job.log("TOS 临时深度视频已删除。")
            except Exception as exc:
                job.log(f"TOS 临时文件未能自动删除：{exc}")


@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "projects.html")


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "depthflow", "time": datetime.now().isoformat(timespec="seconds")})


@app.get("/projects/single")
@app.get("/single")
def single_person_project():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/projects/multi")
@app.get("/multi")
def multi_person_project():
    return send_from_directory(WEB_DIR, "multi.html")


@app.get("/projects/person")
@app.get("/person-only")
def person_only_project():
    return send_from_directory(WEB_DIR, "person.html")


@app.get("/projects/scene")
@app.get("/scene-only")
def scene_only_project():
    return send_from_directory(WEB_DIR, "scene.html")


@app.get("/projects/clothing")
@app.get("/clothing-only")
def clothing_only_project():
    return send_from_directory(WEB_DIR, "clothing.html")


@app.get("/projects/wardrobe")
@app.get("/wardrobe-swap")
def wardrobe_swap_project():
    return send_from_directory(WEB_DIR, "wardrobe.html")


@app.get("/projects/long-video")
@app.get("/long-video")
def long_video_project():
    return send_from_directory(WEB_DIR, "long_video.html")


@app.get("/projects/real-long-video")
@app.get("/real-long-video")
def real_long_video_project():
    return send_from_directory(WEB_DIR, "real_long_video.html")


@app.get("/api/config")
def config():
    load_env_file(override=True)
    latest_depth = restore_latest_depth_job()
    latest_scene = restore_latest_scene_job()
    latest_cloud = restore_latest_cloud_job(resume=not app.config.get("TESTING", False))
    latest_person_retry = restore_latest_person_retry_job()
    latest_person_submission = restore_latest_person_submission_job()
    latest_scene_only = restore_latest_scene_only_job()
    latest_clothing_only = restore_latest_clothing_only_job()
    latest_long_video = restore_latest_long_video_job()
    latest_real_long_video = restore_latest_long_video_job(REAL_PERSON_LONG_PROJECT)
    return jsonify(
        {
            "ark_ready": bool(os.getenv("ARK_API_KEY", "").strip()),
            "tos_ready": TosMediaStore.configured(),
            "temporary_upload_ready": TempFileMediaStore.available(),
            "temporary_tunnel_ready": True,
            "latest_depth": latest_depth.public() if latest_depth else None,
            "latest_scene": latest_scene.public() if latest_scene else None,
            "latest_cloud": latest_cloud.public() if latest_cloud else None,
            "latest_person_retry": latest_person_retry.public() if latest_person_retry else None,
            "latest_person_submission": latest_person_submission.public() if latest_person_submission else None,
            "latest_scene_only": latest_scene_only.public() if latest_scene_only else None,
            "latest_clothing_only": latest_clothing_only.public() if latest_clothing_only else None,
            "latest_long_video": latest_long_video.public() if latest_long_video else None,
            "latest_real_long_video": latest_real_long_video.public() if latest_real_long_video else None,
            "model": os.getenv("ARK_VIDEO_MODEL", DEFAULT_SEEDANCE_MODEL),
            "real_final_video_models": [
                {
                    "model": model,
                    "id": str(spec["id"]),
                    "label": str(spec["label"]),
                    "resolutions": list(spec["resolutions"]),
                }
                for model, spec in REAL_FINAL_VIDEO_MODELS.items()
            ],
            "ark_assets_ready": ark_assets_configured(),
            "ark_assets_upload_ready": ark_assets_configured() and TosMediaStore.configured(),
            "image_model": os.getenv("ARK_IMAGE_MODEL", DEFAULT_SEEDREAM_MODEL),
            "prompt": DEFAULT_PROMPT,
            "scene_prompt": DEFAULT_SCENE_EXTRACTION_PROMPT,
            "long_scene_prompt": DEFAULT_LONG_SCENE_PLATE_PROMPT,
            "person_triview_prompt": DEFAULT_PERSON_TRIVIEW_PROMPT,
            "wardrobe_person_triview_prompt": WARDROBE_NEUTRAL_PERSON_TRIVIEW_PROMPT,
            "clothing_person_triview_prompt": DEFAULT_CLOTHING_PERSON_TRIVIEW_PROMPT,
            "clothing_triview_prompt": DEFAULT_CLOTHING_TRIVIEW_PROMPT,
            "scene_only_prompt": build_scene_only_prompt(),
            "clothing_only_prompt": build_clothing_only_prompt(),
            "wardrobe_swap_model": DEFAULT_SEEDANCE_25_MODEL,
            "wardrobe_swap_resolutions": ["480p", "720p"],
            "wardrobe_swap_prompts": {
                mode: build_wardrobe_swap_prompt(mode) for mode in sorted(WARDROBE_SWAP_MODES)
            },
            "long_video_prompt": DEFAULT_LONG_VIDEO_PROMPT,
            "real_person_sketch_prompt": REAL_PERSON_SKETCH_PROMPT,
            "white_model_prompt": DEFAULT_WHITE_MODEL_PROMPT,
            "final_style_presets": [
                {"id": style_id, **preset}
                for style_id, preset in FINAL_STYLE_PRESETS.items()
            ],
            "default_final_style": "match_character",
            "performance_model": os.getenv("ARK_PERFORMANCE_MODEL", "doubao-seed-2-0-lite-260215"),
        }
    )


def _ark_asset_value(item: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in item and item[name] is not None:
            return item[name]
    return ""


def _read_real_character_library_cache() -> dict[str, Any]:
    try:
        payload = json.loads(REAL_CHARACTER_LIBRARY_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_real_character_library_cache(payload: dict[str, Any]) -> None:
    REAL_CHARACTER_LIBRARY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cached = {
        "groups": list(payload.get("groups") or []),
        "assets": list(payload.get("assets") or []),
        "project_name": str(payload.get("project_name") or ark_assets_project_name()),
        "cached_at": datetime.now().isoformat(timespec="seconds"),
    }
    atomic_write_text(REAL_CHARACTER_LIBRARY_CACHE_PATH, json.dumps(cached, ensure_ascii=False, indent=2))


def _load_ark_character_library() -> dict[str, Any]:
    if not ark_assets_configured():
        return {
            "configured": False,
            "upload_ready": False,
            "groups": [],
            "assets": [],
            "message": (
                "尚未配置火山素材库 AK/SK。可在 .env 中填写 "
                "ARK_ASSETS_ACCESS_KEY_ID 与 ARK_ASSETS_SECRET_ACCESS_KEY；"
                "账号还需开通 Seedance 高级创作入门版或更高权益。"
            ),
            "project_name": ark_assets_project_name(),
        }
    client = ark_assets_client()
    groups: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    errors: list[str] = []
    remote_errors: list[str] = []
    # The real-person redraw UI intentionally uses Ark's AIGC portrait library.
    # LivenessFace groups stay isolated and are never offered by this project.
    for group_type in ("AIGC",):
        try:
            raw_groups = client.list_asset_groups(group_type=group_type)
        except Exception as exc:
            remote_errors.append(f"{group_type}: {exc}")
            continue
        group_ids: list[str] = []
        for item in raw_groups:
            group_id = str(_ark_asset_value(item, "Id", "id") or "").strip()
            if not group_id:
                continue
            group_ids.append(group_id)
            groups.append(
                {
                    "id": group_id,
                    "name": str(_ark_asset_value(item, "Name", "name") or group_id),
                    "description": str(_ark_asset_value(item, "Description", "description") or ""),
                    "shared": item.get("Shared") is True,
                    "can_delete": item.get("CanDelete") is not False,
                    "can_upload": item.get("CanUpload") is not False,
                    "group_type": str(
                        _ark_asset_value(item, "GroupType", "group_type") or group_type
                    ),
                }
            )
        if not group_ids:
            continue
        try:
            raw_assets = client.list_assets(
                group_type=group_type,
                group_ids=group_ids,
                statuses=["Active", "Processing", "Failed"],
            )
        except Exception as exc:
            remote_errors.append(f"{group_type}素材: {exc}")
            continue
        for item in raw_assets:
            asset_id = str(_ark_asset_value(item, "Id", "id") or "").strip()
            if not asset_id:
                continue
            assets.append(
                {
                    "id": asset_id,
                    "uri": f"asset://{asset_id}",
                    "group_id": str(_ark_asset_value(item, "GroupId", "group_id") or ""),
                    "name": str(_ark_asset_value(item, "Name", "name") or asset_id),
                    "asset_type": str(_ark_asset_value(item, "AssetType", "asset_type") or ""),
                    "status": str(_ark_asset_value(item, "Status", "status") or ""),
                    "url": str(_ark_asset_value(item, "URL", "Url", "url") or ""),
                    "shared": item.get("Shared") is True,
                    "can_delete": item.get("CanDelete") is not False,
                    "preview_error": str(item.get("PreviewError") or ""),
                }
            )
    storage_mode = "platform" if getattr(client, "storage_mode", "") == "platform" else "tos" if TosMediaStore.configured() else "project_tunnel"
    stale = False
    cached_at = ""
    if remote_errors:
        cached = _read_real_character_library_cache()
        cached_groups = list(cached.get("groups") or [])
        cached_assets = list(cached.get("assets") or [])
        if cached_groups or cached_assets:
            groups = cached_groups
            assets = cached_assets
            cached_at = str(cached.get("cached_at") or "")
            stale = True
            errors.append(
                "火山角色库本次同步失败，已继续显示最近一次成功缓存"
                + (f"（{cached_at}）" if cached_at else "")
            )
        errors.extend(remote_errors)
    if storage_mode == "project_tunnel":
        errors.append("TOS 尚未开通，上传新人物时将自动使用项目一次性加密通道")
    result = {
        "configured": True,
        "upload_ready": True,
        "storage_mode": storage_mode,
        "groups": groups,
        "assets": assets,
        "message": "；".join(errors),
        "read_error": bool(remote_errors),
        "project_name": ark_assets_project_name(),
        "stale": stale,
        "cached_at": cached_at,
    }
    if not remote_errors:
        try:
            _write_real_character_library_cache(result)
        except OSError:
            pass
    return result


@app.get("/api/real-long-video/character-library")
@app.get("/api/character-library")
def list_real_long_character_library():
    try:
        return jsonify(_load_ark_character_library())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/real-long-video/character-library/groups")
@app.post("/api/character-library/groups")
def create_real_long_virtual_character_group():
    try:
        if not ark_assets_configured():
            raise WorkflowError("尚未配置火山素材库 AK/SK，无法创建虚拟人像组。")
        data = request.get_json(silent=True) or {}
        name = " ".join(str(data.get("name") or "").split())[:64]
        description = " ".join(str(data.get("description") or "").split())[:300]
        if not name:
            raise WorkflowError("请填写虚拟人像组名称。")
        group_id = ark_assets_client().create_asset_group(
            name=name,
            description=description,
            group_type="AIGC",
        )
        return jsonify(
            {
                "ok": True,
                "group_id": group_id,
                "group_type": "AIGC",
                "message": "AIGC 虚拟人像组已创建，可以上传虚拟人物图片。",
            }
        ), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/real-long-video/character-library/validation-sessions")
def create_real_long_character_validation_session():
    try:
        if not ark_assets_configured():
            raise WorkflowError("尚未配置火山素材库 AK/SK，无法创建真人认证会话。")
        data = request.get_json(silent=True) or {}
        name = " ".join(str(data.get("name") or "").split())[:64]
        if not name:
            raise WorkflowError("请先填写真人角色组名称。")
        callback_url = os.getenv("ARK_ASSETS_CALLBACK_URL", "").strip()
        if not callback_url:
            callback_url = (
                request.url_root.rstrip("/")
                + "/api/real-long-video/character-library/validation-callback"
            )
        session = ark_assets_client().create_visual_validate_session(
            callback_url=callback_url,
        )
        return jsonify(
            {
                "ok": True,
                "byted_token": session["byted_token"],
                "h5_link": session["h5_link"],
                "callback_url": session["callback_url"],
                "project_name": session["project_name"],
                "message": "真人认证会话已创建，请打开认证页面完成活体核验。",
            }
        ), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


def _visual_validation_is_pending(exc: ArkAPIError) -> bool:
    detail = f"{exc.code} {exc.raw_message}".lower()
    pending_markers = (
        "pending",
        "inprogress",
        "in_progress",
        "notfinished",
        "not_finished",
        "notcomplete",
        "not_complete",
        "未完成",
        "进行中",
    )
    return any(marker in detail for marker in pending_markers)


@app.post("/api/real-long-video/character-library/validation-sessions/result")
def get_real_long_character_validation_result():
    try:
        if not ark_assets_configured():
            raise WorkflowError("尚未配置火山素材库 AK/SK，无法查询真人认证结果。")
        data = request.get_json(silent=True) or {}
        token = str(data.get("byted_token") or "").strip()
        name = " ".join(str(data.get("name") or "").split())[:64]
        description = " ".join(str(data.get("description") or "").split())[:300]
        if not token:
            raise WorkflowError("缺少真人认证 BytedToken，请重新创建认证会话。")
        try:
            result = ark_assets_client().get_visual_validate_result(byted_token=token)
        except ArkAPIError as exc:
            if _visual_validation_is_pending(exc):
                return jsonify(
                    {
                        "ok": True,
                        "status": "pending",
                        "message": "尚未收到认证成功结果，请完成活体认证后再检查。",
                    }
                ), 202
            raise
        group_id = str(result.get("group_id") or "").strip()
        if not group_id:
            return jsonify(
                {
                    "ok": True,
                    "status": "pending",
                    "message": "认证尚未完成或结果仍在同步，请稍后再检查。",
                }
            ), 202
        if name:
            ark_assets_client().update_asset_group(
                group_id,
                name=name,
                description=description,
            )
        return jsonify(
            {
                "ok": True,
                "status": "validated",
                "group_id": group_id,
                "message": "真人认证成功，角色组已经创建并可用于上传人物。",
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/real-long-video/character-library/validation-callback")
def real_long_character_validation_callback():
    return (
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>真人认证已返回</title><body style='font-family:sans-serif;padding:32px'>"
        "<h2>真人认证页面已返回</h2><p>请回到“真实人物复刻重绘”页面，"
        "点击“我已完成认证，获取角色组”。如果本页无法在手机打开，也不影响桌面端用 BytedToken 查询结果。"
        "</p></body></html>",
        200,
        {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
    )


@app.patch("/api/real-long-video/character-library/groups/<group_id>")
@app.patch("/api/character-library/groups/<group_id>")
def update_real_long_character_group(group_id: str):
    try:
        if not ark_assets_configured():
            raise WorkflowError("尚未配置火山素材库 AK/SK，无法修改角色组。")
        data = request.get_json(silent=True) or {}
        name = " ".join(str(data.get("name") or "").split())[:64]
        description = " ".join(str(data.get("description") or "").split())[:300]
        ark_assets_client().update_asset_group(
            group_id,
            name=name,
            description=description,
        )
        return jsonify({"ok": True, "group_id": group_id})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.delete("/api/real-long-video/character-library/groups/<group_id>")
@app.delete("/api/character-library/groups/<group_id>")
def delete_real_long_character_group(group_id: str):
    try:
        if not ark_assets_configured():
            raise WorkflowError("尚未配置火山素材库 AK/SK，无法删除角色组。")
        ark_assets_client().delete_asset_group(group_id)
        return jsonify({"ok": True, "group_id": group_id})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


def _cleanup_ark_asset_upload(
    asset_id: str,
    upload_source: ArkCharacterUploadSource,
    local_path: Path,
) -> None:
    try:
        client = ark_assets_client()
        for _attempt in range(60):
            status = str(client.get_asset(asset_id).get("Status") or "").strip()
            if status in {"Active", "Failed"}:
                break
            time.sleep(5)
    except Exception:
        pass
    upload_source.close()
    try:
        local_path.unlink(missing_ok=True)
    except OSError:
        pass


@app.post("/api/real-long-video/character-library/assets")
@app.post("/api/character-library/assets")
def upload_real_long_character_asset():
    local_path: Path | None = None
    upload_source: ArkCharacterUploadSource | None = None
    try:
        if not ark_assets_configured():
            raise WorkflowError("尚未配置火山素材库 AK/SK，无法上传人物素材。")
        group_id = request.form.get("group_id", "").strip()
        if not re.fullmatch(r"group-[A-Za-z0-9_-]{6,120}", group_id):
            raise WorkflowError("请选择有效的火山人物素材组。")
        name = " ".join(request.form.get("name", "").split())[:64]
        if not name:
            raise WorkflowError("请填写人物素材名称。")
        upload = request.files.get("asset_file")
        run_dir = timestamped_run_dir("ark_character_upload")
        local_path = save_upload(upload, run_dir, "character")
        if local_path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise WorkflowError("人物库当前只允许上传 JPG、PNG 或 WEBP 图片。")
        if local_path.stat().st_size > 30 * 1024 * 1024:
            raise WorkflowError("火山人物图片不能超过 30 MB。")
        upload_source = prepare_ark_character_upload_source(local_path)
        try:
            asset_id = ark_assets_client().create_asset(
                group_id=group_id,
                url=upload_source.url,
                name=name,
                asset_type="Image",
            )
        except Exception:
            upload_source.close()
            upload_source = None
            raise
        threading.Thread(
            target=_cleanup_ark_asset_upload,
            args=(asset_id, upload_source, local_path),
            daemon=True,
            name=f"ark-asset-cleanup-{asset_id[-8:]}",
        ).start()
        channel = upload_source.channel
        upload_source = None
        local_path = None
        return jsonify(
            {
                "ok": True,
                "asset_id": asset_id,
                "uri": f"asset://{asset_id}",
                "status": "Processing",
                "storage_channel": channel,
                "message": "人物素材已提交火山入库；项目会保持临时地址直到审核完成，状态变为 Active 后即可用于成片。",
            }
        ), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        if upload_source is not None:
            upload_source.close()
        if local_path is not None and local_path.is_file():
            try:
                local_path.unlink()
            except OSError:
                pass


@app.delete("/api/real-long-video/character-library/assets/<asset_id>")
@app.delete("/api/character-library/assets/<asset_id>")
def delete_real_long_character_asset(asset_id: str):
    try:
        if not ark_assets_configured():
            raise WorkflowError("尚未配置火山素材库 AK/SK，无法删除人物素材。")
        ark_assets_client().delete_asset(asset_id)
        return jsonify({"ok": True, "asset_id": asset_id})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/check")
def check_api():
    try:
        return jsonify(
            {
                "ok": True,
                "message": api_client().check_credentials(),
                "tos_ready": TosMediaStore.configured(),
                "temporary_upload_ready": TempFileMediaStore.available(),
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.get("/api/seedance/browser")
def seedance_browser_status():
    return jsonify(SEEDANCE_WEB.status().public())


@app.post("/api/seedance/browser/open")
def open_seedance_browser():
    try:
        status = SEEDANCE_WEB.status(launch=True)
        return jsonify(status.public())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/seedance/browser/controls")
def seedance_browser_controls():
    try:
        return jsonify(SEEDANCE_WEB.inspect_controls())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/depth")
def create_depth_job():
    try:
        job = new_job("depth")
        source = save_upload(request.files.get("reference_video"), job.run_dir, "reference")
        output = job.run_dir / "depth.mp4"
        blur_range = request.form.get("blur_range", "").strip()

        def worker() -> None:
            try:
                result = run_depth(job, source, output, blur_range)
                job.depth_path = result
                job.update(status="succeeded", stage="深度视频已生成", progress=100)
            except Exception as exc:
                job_error(job, exc)

        threading.Thread(target=worker, daemon=True, name=f"depth-{job.id}").start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/generate")
def create_generation_job():
    try:
        job = new_job("generate")
        depth_job_id = request.form.get("depth_job_id", "").strip()
        depth_reference = request.form.get("depth_reference", "").strip()
        depth_path: Path | None = None
        if depth_job_id:
            depth_job = get_job(depth_job_id)
            if not depth_job.depth_path or not depth_job.depth_path.is_file():
                raise WorkflowError("所选深度任务还没有可用视频。")
            depth_path = depth_job.depth_path
            # The generation job becomes the active job in the browser. Keep the
            # reused depth file attached to it so the depth preview and download
            # remain available while Seedance is running.
            job.depth_path = depth_path
        person, clothing, scene = save_reference_images(job)
        options = generation_options()
        if depth_path is not None:
            apply_automatic_duration(job, options, depth_path)
        target = run_web_generation if options["generation_channel"] == "web" else run_generation
        target_kwargs = {
            "job": job,
            "depth_path": depth_path,
            "person_source": person,
            "clothing_source": clothing,
            "scene_source": scene,
            "options": options,
        }
        if target is run_generation:
            target_kwargs["depth_reference"] = depth_reference
        threading.Thread(
            target=target,
            kwargs=target_kwargs,
            daemon=True,
            name=f"generate-{job.id}",
        ).start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/full")
def create_full_job():
    try:
        job = new_job("full")
        source = save_upload(request.files.get("reference_video"), job.run_dir, "reference")
        person, clothing, scene = save_reference_images(job)
        options = generation_options()
        blur_range = request.form.get("blur_range", "").strip()
        manual_reference = request.form.get("depth_reference", "").strip()

        def worker() -> None:
            try:
                depth_path = run_depth(job, source, job.run_dir / "depth.mp4", blur_range, start=0, span=50)
                options["duration"] = job.generation_duration or match_seedance_duration(
                    inspect_video(depth_path).duration
                )
                if options["generation_channel"] == "web":
                    run_web_generation(
                        job,
                        depth_path=depth_path,
                        person_source=person,
                        clothing_source=clothing,
                        scene_source=scene,
                        options=options,
                        progress_start=50,
                    )
                else:
                    run_generation(
                        job,
                        depth_path=depth_path,
                        depth_reference=manual_reference,
                        person_source=person,
                        clothing_source=clothing,
                        scene_source=scene,
                        options=options,
                        progress_start=50,
                    )
            except Exception as exc:
                job_error(job, exc)

        threading.Thread(target=worker, daemon=True, name=f"full-{job.id}").start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/multi/generate")
def create_multi_generation_job():
    try:
        job = new_job("multi_generate")
        depth_job_id = request.form.get("depth_job_id", "").strip()
        depth_reference = request.form.get("depth_reference", "").strip()
        depth_path: Path | None = None
        if depth_job_id:
            depth_job = get_job(depth_job_id)
            if not depth_job.depth_path or not depth_job.depth_path.is_file():
                raise WorkflowError("所选深度任务还没有可用视频。")
            depth_path = depth_job.depth_path
            job.depth_path = depth_path
        elif not depth_reference:
            raise WorkflowError("请先生成深度视频，再执行多人复刻。")

        character_sources, scene_source, roles = save_multi_reference_images(job)
        options = generation_options()
        options["prompt"] = request.form.get("prompt", "").strip() or build_multi_prompt(roles)
        if depth_path is not None:
            apply_automatic_duration(job, options, depth_path)
        threading.Thread(
            target=run_multi_generation,
            kwargs={
                "job": job,
                "depth_path": depth_path,
                "depth_reference": depth_reference,
                "character_sources": character_sources,
                "scene_source": scene_source,
                "options": options,
            },
            daemon=True,
            name=f"multi-generate-{job.id}",
        ).start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/multi/full")
def create_multi_full_job():
    try:
        job = new_job("multi_full")
        source = save_upload(request.files.get("reference_video"), job.run_dir, "reference")
        character_sources, scene_source, roles = save_multi_reference_images(job)
        options = generation_options()
        options["prompt"] = request.form.get("prompt", "").strip() or build_multi_prompt(roles)
        blur_range = request.form.get("blur_range", "").strip()
        manual_reference = request.form.get("depth_reference", "").strip()

        def worker() -> None:
            try:
                depth_path = run_depth(
                    job,
                    source,
                    job.run_dir / "depth.mp4",
                    blur_range,
                    start=0,
                    span=50,
                )
                options["duration"] = job.generation_duration or match_seedance_duration(
                    inspect_video(depth_path).duration
                )
                run_multi_generation(
                    job,
                    depth_path=depth_path,
                    depth_reference=manual_reference,
                    character_sources=character_sources,
                    scene_source=scene_source,
                    options=options,
                    progress_start=50,
                )
            except Exception as exc:
                job_error(job, exc)

        threading.Thread(
            target=worker,
            daemon=True,
            name=f"multi-full-{job.id}",
        ).start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


def persist_wardrobe_swap_job(
    job: WebJob,
    mode: str,
    *,
    source_job_id: str = "",
) -> Path:
    white_task_record = job.run_dir / "white_model_task" / "job.json"
    white_task_id = ""
    if white_task_record.is_file():
        try:
            white_task_id = str(json.loads(white_task_record.read_text(encoding="utf-8")).get("task_id") or "")
        except (OSError, ValueError, TypeError):
            pass
    return save_shot_manifest(
        job.run_dir / "wardrobe_manifest.json",
        {
            "local_job_id": job.id,
            "kind": job.kind,
            "project": WARDROBE_SWAP_PROJECT,
            "mode": mode,
            "source_job_id": source_job_id,
            "status": job.status,
            "stage": job.stage,
            "progress": job.progress,
            "created_at": job.created_at,
            "source_duration": job.source_duration,
            "source": str(_run_artifact(job.run_dir, "reference", {".mp4", ".mov"}) or ""),
            "mosaic": str(job.mosaic_path or ""),
            "white_model": str(job.white_model_path or ""),
            "person": str(job.person_path or ""),
            "clothing": str(job.clothing_path or ""),
            "scene": str(job.scene_path or ""),
            "output": str(job.output_path or ""),
            "actors": job.actors,
            "error": job.error,
            "recovery_action": job.recovery_action,
            "white_model_task_id": white_task_id,
        },
    )


def _wardrobe_record_belongs_to_source(record: dict[str, Any], source_job: WebJob) -> bool:
    """Return whether a historical generation record belongs to one prepare job."""

    if str(record.get("source_job_id") or "").strip() == source_job.id:
        return True
    source_root = source_job.run_dir.resolve()
    for key in ("source", "mosaic", "white_model", "depth", "scene", "person", "clothing"):
        candidate = _runs_record_file(record.get(key))
        if candidate is None:
            continue
        try:
            candidate.resolve().relative_to(source_root)
            return True
        except ValueError:
            continue
    return False


def restore_wardrobe_output_link(source_job: WebJob, mode: str) -> bool:
    """Reconnect an existing local final video to its persistent prepare job."""

    if source_job.output_path and source_job.output_path.is_file():
        return False
    candidates: list[tuple[str, float, Path]] = []
    runs_root = PROJECT_DIR / "runs"
    patterns = (
        f"20*_web_wardrobe_generate_{mode}_*/wardrobe_manifest.json",
        f"20*_web_wardrobe_generate_{mode}_*/job.json",
    )
    seen: set[Path] = set()
    for pattern in patterns:
        for record_path in runs_root.glob(pattern):
            if record_path in seen:
                continue
            seen.add(record_path)
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if str(record.get("status") or "").strip().lower() != "succeeded":
                continue
            output = _runs_record_file(record.get("output"))
            if output is None or not _wardrobe_record_belongs_to_source(record, source_job):
                continue
            created_at = str(
                record.get("created_at")
                or datetime.fromtimestamp(record_path.stat().st_mtime).isoformat(timespec="seconds")
            )
            candidates.append((created_at, record_path.stat().st_mtime, output))
    if not candidates:
        return False
    _created_at, _mtime, output = max(candidates, key=lambda item: (item[0], item[1]))
    source_job.output_path = output
    source_job.log(f"已从本地存档重新关联最终成片：{output.name}")
    return True


def persist_wardrobe_generation_result(
    generated_job: WebJob,
    source_job: WebJob,
    mode: str,
) -> None:
    """Persist a generation attempt and attach successful output to its project."""

    persist_wardrobe_swap_job(generated_job, mode, source_job_id=source_job.id)
    if generated_job.status != "succeeded" or not generated_job.output_path or not generated_job.output_path.is_file():
        return
    source_job.output_path = generated_job.output_path
    source_job.update(
        status="succeeded",
        stage="最终成片已生成并保存，可随时恢复预览和下载",
        progress=100,
        error="",
    )
    source_job.log(f"最终成片已写入项目存档：{generated_job.output_path.name}")
    persist_wardrobe_swap_job(source_job, mode)


WARDROBE_CLOUD_ACTIVE_STATUSES = {"queued", "running", "submitted", "processing"}
WARDROBE_CLOUD_FAILED_STATUSES = {"failed", "expired", "cancelled", "canceled"}


def wardrobe_white_worker_alive(job_id: str) -> bool:
    """Return whether this process still owns the wardrobe white-model worker."""

    return any(
        thread.is_alive()
        and job_id in thread.name
        and thread.name.startswith(("wardrobe-white-", "wardrobe-recover-white-"))
        for thread in threading.enumerate()
    )


def _wardrobe_white_task_record(job: WebJob) -> tuple[Path, dict[str, Any]]:
    path = job.run_dir / "white_model_task" / "job.json"
    if not path.is_file():
        return path, {}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return path, {}
    return path, record if isinstance(record, dict) else {}


def _seedance_task_failure_message(task: dict[str, Any]) -> str:
    status = str(task.get("status") or "failed")
    error = task.get("error") or {}
    message = error.get("message") if isinstance(error, dict) else str(error)
    return f"Seedance 任务状态为 {status}：{message or '未提供原因'}"


def _recover_interrupted_wardrobe_task_id(record: dict[str, Any]) -> tuple[str, int]:
    """Find the one task created immediately after a locally interrupted POST."""

    try:
        submitted_at = float(record.get("submitted_at") or 0)
    except (TypeError, ValueError):
        submitted_at = 0
    if submitted_at <= 0:
        return "", 0
    known = {str(value).strip() for value in record.get("known_task_ids") or [] if str(value).strip()}
    model = str(record.get("model") or "").strip()
    resolution = str(record.get("resolution") or "").strip()
    ratio = str(record.get("expected_ratio") or record.get("requested_ratio") or "").strip()
    try:
        duration = int(float(record.get("duration") or 0))
    except (TypeError, ValueError):
        duration = 0
    generate_audio = record.get("generate_audio")
    candidates: list[str] = []
    for item in api_client().list_tasks(page_size=50):
        task_id = str(item.get("id") or "").strip()
        if not task_id or task_id in known or str(item.get("model") or "").strip() != model:
            continue
        try:
            created_at = float(item.get("created_at") or 0)
        except (TypeError, ValueError):
            created_at = 0
        item_resolution = str(item.get("resolution") or "").strip()
        item_ratio = str(item.get("ratio") or "").strip()
        try:
            item_duration = int(float(item.get("duration") or 0))
        except (TypeError, ValueError):
            item_duration = 0
        item_audio = item.get("generate_audio")
        if not (submitted_at - 5 <= created_at <= submitted_at + 120):
            continue
        if resolution and item_resolution and item_resolution != resolution:
            continue
        if ratio and item_ratio and item_ratio != ratio:
            continue
        if duration and item_duration and item_duration != duration:
            continue
        if generate_audio is not None and item_audio is not None and bool(item_audio) is not bool(generate_audio):
            continue
        candidates.append(task_id)
    return (candidates[0] if len(candidates) == 1 else ""), len(candidates)


def reconcile_wardrobe_white_task_state(job: WebJob, *, allow_network: bool = True) -> None:
    """Repair stale wardrobe parent/child state without ever resubmitting a paid task."""

    if not job.kind.startswith("wardrobe_prepare_"):
        return
    if job.white_model_path and job.white_model_path.is_file():
        return
    if wardrobe_white_worker_alive(job.id):
        return
    record_path, record = _wardrobe_white_task_record(job)
    if not record:
        # During stage 01 (local face mosaic) no white child record should
        # exist yet. Only treat an active *white-model* stage without a child
        # record as an interrupted state; otherwise normal mosaic polling
        # would flash a false system error while the local worker is healthy.
        if (
            job.status in {"queued", "running", "submitted"}
            and "白膜" in str(job.stage or "")
        ):
            job.update(
                status="failed",
                stage="上次白膜准备流程已中断",
                error="没有找到白膜云端任务记录；现有打码视频仍可复用，请重新生成白膜。",
                recovery_action="",
            )
        return

    task_id = str(record.get("task_id") or "").strip()
    record_status = str(record.get("status") or "").strip().lower()
    cloud_status = str(record.get("cloud_status") or "").strip().lower()
    if not task_id and record_status in {"submitting", "ambiguous"} and allow_network:
        try:
            task_id, candidate_count = _recover_interrupted_wardrobe_task_id(record)
        except Exception as exc:
            job.update(
                status="failed",
                stage="白膜提交状态等待核对",
                error=f"上次提交在任务号返回前中断，当前只读核对失败：{exc}。系统没有重新提交。",
                recovery_action="",
            )
            return
        if task_id:
            record["task_id"] = task_id
            record["status"] = "running"
            record["error"] = ""
            save_job_record(record_path.parent, record)
        elif candidate_count > 1:
            record.update(status="ambiguous", error="中断窗口内发现多个可能的云端任务，禁止自动重新提交。")
            save_job_record(record_path.parent, record)
            job.update(
                status="failed",
                stage="白膜任务号无法唯一确认",
                error="中断窗口内发现多个可能的 Seedance 任务；为避免重复计费，系统没有重新提交。",
                recovery_action="",
            )
            return
        else:
            try:
                submitted_at = float(record.get("submitted_at") or 0)
            except (TypeError, ValueError):
                submitted_at = 0
            if submitted_at and time.time() - submitted_at >= 180:
                message = "上次流程在付费任务号返回前中断，任务列表中未发现匹配任务；现有打码视频仍可复用。"
                record.update(status="failed", cloud_status="", error=message)
                save_job_record(record_path.parent, record)
                job.update(status="failed", stage="上次白膜提交未完成", error=message, recovery_action="")
            return

    if task_id and allow_network and cloud_status not in {"succeeded", *WARDROBE_CLOUD_FAILED_STATUSES}:
        try:
            task = api_client().get_task(task_id)
            cloud_status = str(task.get("status") or "").strip().lower()
            record["cloud_status"] = cloud_status
            if cloud_status == "succeeded":
                record["status"] = "running"
                record["error"] = ""
            elif cloud_status in WARDROBE_CLOUD_FAILED_STATUSES:
                record["status"] = "failed"
                record["error"] = _seedance_task_failure_message(task)
            else:
                record["status"] = "running"
            save_job_record(record_path.parent, record)
        except Exception:
            cloud_status = str(record.get("cloud_status") or "").strip().lower()

    error = str(record.get("error") or "").strip()
    if task_id and cloud_status == "succeeded":
        job.update(
            status="failed",
            stage="云端白膜已成功，等待恢复下载",
            error="云端白膜已经成功，仅需恢复下载，不会重新提交付费任务。",
            recovery_action="resume_wardrobe_white_download",
        )
    elif task_id and cloud_status in WARDROBE_CLOUD_ACTIVE_STATUSES:
        job.update(
            status="paused",
            stage="云端白膜仍在运行，可恢复本地跟进",
            error="本地跟进曾中断；恢复时只查询该任务，不会重新提交。",
            recovery_action="resume_wardrobe_white_download",
        )
    elif cloud_status in WARDROBE_CLOUD_FAILED_STATUSES or record_status == "failed":
        job.update(
            status="failed",
            stage="白膜任务失败",
            error=error or "Seedance 白膜任务已经失败；现有打码视频仍可复用。",
            recovery_action="",
        )


def restore_wardrobe_swap_job(
    job_id: str = "",
    *,
    mode: str = "",
    prepare_only: bool = False,
) -> WebJob | None:
    with JOBS_LOCK:
        candidates = [
            job for job in JOBS.values()
            if (
                job.project == WARDROBE_SWAP_PROJECT
                and job.kind.startswith(("wardrobe_prepare_", "wardrobe_generate_"))
                and (not job_id or job.id == job_id)
                and (not mode or wardrobe_mode_from_job(job) == mode)
                and (not prepare_only or job.kind.startswith("wardrobe_prepare_"))
            )
        ]
    cached = max(candidates, key=lambda item: item.created_at) if candidates else None
    if job_id and cached is not None:
        return cached

    manifest_entries: list[tuple[str, float, Path, dict[str, Any]]] = []
    for manifest in (PROJECT_DIR / "runs").glob("20*_web_wardrobe_*/wardrobe_manifest.json"):
        try:
            record = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        restored_id = str(record.get("local_job_id") or manifest.parent.name.rsplit("_", 1)[-1])
        if job_id and restored_id != job_id:
            continue
        record_kind = str(record.get("kind") or "")
        record_mode = str(record.get("mode") or "").strip().lower()
        if mode and record_mode != mode:
            continue
        if prepare_only and not record_kind.startswith("wardrobe_prepare_"):
            continue
        white_model = _runs_record_file(record.get("white_model"))
        mosaic = _runs_record_file(record.get("mosaic"))
        source = _runs_record_file(record.get("source"))
        if white_model is None and mosaic is None and source is None:
            continue
        created_at = str(
            record.get("created_at")
            or datetime.fromtimestamp(manifest.stat().st_mtime).isoformat(timespec="seconds")
        )
        manifest_entries.append((created_at, manifest.stat().st_mtime, manifest, record))
    if not manifest_entries:
        return cached

    created_at, _mtime, manifest, record = max(manifest_entries, key=lambda item: (item[0], item[1]))
    if cached is not None and cached.created_at >= created_at:
        return cached
    restored_id = str(record.get("local_job_id") or manifest.parent.name.rsplit("_", 1)[-1])
    white_model = _runs_record_file(record.get("white_model"))
    mosaic = _runs_record_file(record.get("mosaic"))
    restored_status = str(record.get("status") or "failed")
    if white_model and mosaic and restored_status not in {"running", "queued", "submitted"}:
        restored_status = "succeeded"
    restored = WebJob(
        id=restored_id,
        kind=str(record.get("kind") or "wardrobe_prepare_person"),
        project=WARDROBE_SWAP_PROJECT,
        run_dir=manifest.parent,
        status=restored_status,
        stage=str(record.get("stage") or ("已恢复衣装智换白膜与源素材" if white_model else "可恢复已提交的白膜任务")),
        progress=int(record.get("progress") or (100 if white_model else 0)),
        source_duration=float(record.get("source_duration") or 0),
        mosaic_path=mosaic,
        white_model_path=white_model,
        person_path=_runs_record_file(record.get("person")),
        clothing_path=_runs_record_file(record.get("clothing")),
        scene_path=_runs_record_file(record.get("scene")),
        output_path=_runs_record_file(record.get("output")),
        actors=list(record.get("actors") or []),
        error=str(record.get("error") or ""),
        recovery_action=str(record.get("recovery_action") or ""),
        created_at=created_at,
    )
    if white_model:
        restored.log("已从本地存档恢复白膜和已准备的参考素材，不会重复产生费用。")
    else:
        restored.log("已恢复未完成的衣装智换准备任务；若云端白膜已成功，可只恢复下载而不重新生成。")
    reconcile_wardrobe_white_task_state(restored)
    persist_wardrobe_swap_job(restored, wardrobe_mode_from_job(restored))
    with JOBS_LOCK:
        return JOBS.setdefault(restored.id, restored)


def restore_wardrobe_white_model_job(parent_job: WebJob) -> WebJob | None:
    """Restore the nested white-model cloud task used by one wardrobe job."""
    white_job_id = f"{parent_job.id}-white"
    with JOBS_LOCK:
        existing = JOBS.get(white_job_id)
    if existing:
        return existing
    run_dir = parent_job.run_dir / "white_model_task"
    record_path = run_dir / "job.json"
    if not record_path.is_file():
        return None
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    task_id = str(record.get("task_id") or "").strip()
    if not task_id:
        return None
    output = _local_record_file(run_dir, record.get("output")) or _runs_record_file(record.get("output"))
    depth = _runs_record_file(record.get("depth"))
    record_status = str(record.get("status") or "failed").strip().lower()
    cloud_status = str(record.get("cloud_status") or "").strip().lower()
    if output:
        restored_status = "succeeded"
        restored_stage = "白膜云端结果已恢复"
    elif cloud_status in WARDROBE_CLOUD_ACTIVE_STATUSES:
        restored_status = "paused"
        restored_stage = "白膜云端任务仍在运行，可恢复跟进"
    elif cloud_status == "succeeded":
        restored_status = "failed"
        restored_stage = "白膜已在云端成功，等待恢复下载"
    else:
        restored_status = "failed"
        restored_stage = "白膜云端任务已经失败" if record_status == "failed" else "白膜任务状态待核对"
    restored = WebJob(
        id=white_job_id,
        kind="wardrobe_white_model",
        project=WARDROBE_SWAP_PROJECT,
        run_dir=run_dir,
        status=restored_status,
        stage=restored_stage,
        progress=100 if output else 90,
        task_id=task_id,
        depth_path=depth,
        output_path=output,
        cloud_status=cloud_status or ("succeeded" if output else ""),
        cloud_started_at=float(record.get("submitted_at") or time.time()),
        generation_duration=int(record.get("duration") or 0),
        error=str(record.get("error") or ""),
        created_at=str(record.get("created_at") or datetime.now().isoformat(timespec="seconds")),
    )
    restored.log(f"已恢复 Seedance 白膜任务 {task_id}；后续只查询和下载，不会重新提交。")
    with JOBS_LOCK:
        return JOBS.setdefault(restored.id, restored)


def wardrobe_mode_from_job(job: WebJob) -> str:
    match = re.search(r"wardrobe_(?:prepare|generate)_([a-z]+)", job.kind)
    mode = match.group(1) if match else ""
    if mode not in WARDROBE_SWAP_MODES:
        raise WorkflowError("无法识别衣装智换任务模式。")
    return mode


def wardrobe_person_triview_is_current(job: WebJob) -> bool:
    """Return whether a wardrobe source-person board uses the current neutral outfit rule."""

    if not job.person_path or not job.person_path.is_file():
        return False
    version_file = job.run_dir / "original_person_triview.version"
    try:
        version = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return version == WARDROBE_NEUTRAL_PERSON_TRIVIEW_VERSION


@app.get("/api/wardrobe-swap/latest")
def latest_wardrobe_swap_job():
    mode = request.args.get("mode", "").strip().lower()
    if mode and mode not in WARDROBE_SWAP_MODES:
        return jsonify({"error": "衣装智换模式无效。"}), 400
    job = restore_wardrobe_swap_job(mode=mode, prepare_only=bool(mode))
    if job is not None and job.kind.startswith("wardrobe_prepare_"):
        restore_wardrobe_output_link(job, wardrobe_mode_from_job(job))
        reconcile_wardrobe_white_task_state(job)
        persist_wardrobe_swap_job(job, wardrobe_mode_from_job(job))
    return jsonify(job.public() if job else {"id": "", "status": "empty"})


def wardrobe_prepare_source(job: WebJob) -> Path:
    clipped = job.run_dir / "reference_first_15s.mp4"
    if clipped.is_file():
        return clipped
    source = _run_artifact(job.run_dir, "reference", {".mp4", ".mov"})
    if source is None or not source.is_file():
        raise WorkflowError("找不到该衣装智换任务的原片，请重新上传。")
    return source


def wardrobe_prepare_job_from_request() -> tuple[WebJob, str]:
    source_job_id = request.form.get("source_job_id", "").strip()
    if not source_job_id:
        raise WorkflowError("请先生成本地人脸打码视频。")
    try:
        job = get_job(source_job_id)
    except WorkflowError:
        job = restore_wardrobe_swap_job(source_job_id)
        if job is None:
            raise
    if job.project != WARDROBE_SWAP_PROJECT or not job.kind.startswith("wardrobe_prepare_"):
        raise WorkflowError("该任务不是衣装智换准备任务。")
    reconcile_wardrobe_white_task_state(job)
    persist_wardrobe_swap_job(job, wardrobe_mode_from_job(job))
    if job.status in {"queued", "running", "submitted"}:
        raise WorkflowError("当前衣装智换步骤仍在运行，请等待完成后再继续。")
    return job, wardrobe_mode_from_job(job)


@app.post("/api/wardrobe-swap/mosaic")
def create_wardrobe_swap_mosaic_job():
    """Stage 1: upload/trim the source and create only the local mosaic."""
    try:
        mode = request.form.get("mode", "").strip().lower()
        if mode not in WARDROBE_SWAP_MODES:
            raise WorkflowError("请选择只更换人物、只更换场景、只更换服装或随心换。")
        with JOBS_LOCK:
            active_duplicate = next(
                (
                    candidate
                    for candidate in JOBS.values()
                    if candidate.project == WARDROBE_SWAP_PROJECT
                    and candidate.kind == f"wardrobe_prepare_{mode}"
                    and candidate.status in {"queued", "running", "submitted"}
                ),
                None,
            )
        if active_duplicate is not None:
            active_duplicate.log("已拦截重复点击：当前人脸打码任务仍在运行，不会创建第二份任务。")
            return jsonify(active_duplicate.public()), 202
        job = new_job(f"wardrobe_prepare_{mode}")
        source = save_upload(request.files.get("reference_video"), job.run_dir, "reference")

        def worker() -> None:
            try:
                working_source = normalize_wardrobe_source_duration(job, source)
                run_wardrobe_face_mosaic(job, working_source, start=2, span=96)
                job.update(
                    status="succeeded",
                    stage="人脸打码视频已生成；请人工预览后再生成白膜",
                    progress=100,
                    error="",
                    recovery_action="",
                )
                persist_wardrobe_swap_job(job, mode)
            except Exception as exc:
                job_error(job, exc)
                persist_wardrobe_swap_job(job, mode)

        threading.Thread(target=worker, daemon=True, name=f"wardrobe-mosaic-{mode}-{job.id}").start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/wardrobe-swap/white-model")
def create_wardrobe_swap_white_model_job():
    """Stage 2: submit only the paid Seedance white-model task."""
    try:
        job, mode = wardrobe_prepare_job_from_request()
        if not job.mosaic_path or not job.mosaic_path.is_file():
            raise WorkflowError("当前任务还没有人脸打码视频，请先完成本地打码。")
        source = wardrobe_prepare_source(job)
        job.update(
            status="running",
            stage="准备提交 Seedance 2.0 · 480p 白膜任务",
            progress=1,
            error="",
            recovery_action="",
        )
        persist_wardrobe_swap_job(job, mode)

        def worker() -> None:
            try:
                run_wardrobe_white_model(job, source, start=2, span=96)
                job.update(
                    status="succeeded",
                    stage="白膜视频已生成；现在可以单独提取人物、服装与场景素材",
                    progress=100,
                    error="",
                    recovery_action="",
                )
                persist_wardrobe_swap_job(job, mode)
            except Exception as exc:
                job_error(job, exc)
                white_job = restore_wardrobe_white_model_job(job)
                if (
                    white_job
                    and white_job.task_id
                    and white_job.cloud_status == "succeeded"
                    and (not job.white_model_path or not job.white_model_path.is_file())
                ):
                    job.update(recovery_action="resume_wardrobe_white_download")
                    job.log("云端白膜任务已经成功；当前只需恢复结果下载，不需要再次付费生成。")
                persist_wardrobe_swap_job(job, mode)

        threading.Thread(target=worker, daemon=True, name=f"wardrobe-white-{mode}-{job.id}").start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/wardrobe-swap/extract-references")
def create_wardrobe_swap_reference_extraction_job():
    """Stage 3: upload/reuse or extract the three visual reference types."""
    try:
        job, mode = wardrobe_prepare_job_from_request()
        if not job.white_model_path or not job.white_model_path.is_file():
            raise WorkflowError("当前任务还没有白膜视频，请先完成并预览白膜。")
        source = wardrobe_prepare_source(job)
        original_scene = None if mode == "custom" else save_optional_reference_image(
            job, "original_scene_image", "original_scene"
        )
        original_clothing = None if mode == "custom" else save_optional_reference_image(
            job, "original_clothing_image", "original_clothing"
        )
        original_person_asset = (
            request.form.get("person_asset", "").strip()
            if mode not in {"person", "custom"}
            else ""
        )
        if original_person_asset and not re.fullmatch(r"asset://[A-Za-z0-9_-]{6,160}", original_person_asset):
            raise WorkflowError("原人物必须选择角色库中状态为 Active 的火山 Asset。")
        if original_scene:
            job.scene_path = original_scene
        if original_clothing:
            job.clothing_path = original_clothing
        if original_person_asset:
            job.actors = [{"id": 1, "role": "原片人物", "trusted_asset_uri": original_person_asset}]

        image_model = request.form.get("image_model", "").strip() or os.getenv(
            "ARK_IMAGE_MODEL", DEFAULT_SEEDREAM_MODEL
        )
        scene_prompt = request.form.get("scene_prompt", "").strip() or DEFAULT_SCENE_EXTRACTION_PROMPT
        clothing_prompt = request.form.get("clothing_prompt", "").strip() or DEFAULT_CLOTHING_TRIVIEW_PROMPT
        person_prompt = WARDROBE_NEUTRAL_PERSON_TRIVIEW_PROMPT
        extract_missing = form_bool("extract_missing", True) or mode == "custom"
        missing_scene = mode == "custom" or (mode in {"person", "clothing"} and not job.scene_path)
        missing_clothing = mode == "custom" or (mode in {"person", "scene"} and not job.clothing_path)
        # The original-person extraction and the selected Ark character are two
        # different things.  A previously selected Asset must not suppress source
        # person extraction: users still need a preview they can inspect and submit
        # to their own character library.
        missing_person = mode == "custom" or (
            mode in {"scene", "clothing"}
            and not wardrobe_person_triview_is_current(job)
        )
        if (missing_scene or missing_clothing or missing_person) and not extract_missing:
            labels = []
            if missing_scene:
                labels.append("原场景图")
            if missing_clothing:
                labels.append("原服装图")
            if missing_person:
                labels.append("原人物角色")
            raise WorkflowError("缺少" + "、".join(labels) + "；请上传/选择，或开启 Seedream 5.0 自动提取。")

        job.update(
            status="running",
            stage="正在准备单独提取人物、服装与场景素材",
            progress=1,
            error="",
            recovery_action="",
        )
        persist_wardrobe_swap_job(job, mode)

        def worker() -> None:
            try:
                tasks: list[str] = []
                if missing_scene:
                    tasks.append("scene")
                if missing_clothing:
                    tasks.append("clothing")
                if missing_person:
                    tasks.append("person")
                per_span = max(1, 96 // max(len(tasks), 1))
                cursor = 2
                for task in tasks:
                    if task == "scene":
                        run_scene_extraction(
                            job,
                            source,
                            prompt=scene_prompt,
                            model=image_model,
                            start=cursor,
                            span=per_span,
                            recovery_action="retry_wardrobe_sources",
                        )
                    elif task == "clothing":
                        run_clothing_reference_extraction(
                            job,
                            source,
                            prompt=clothing_prompt,
                            model=image_model,
                            start=cursor,
                            span=per_span,
                        )
                    else:
                        run_person_triview_extraction(
                            job,
                            source,
                            prompt=person_prompt,
                            model=image_model,
                            start=cursor,
                            span=per_span,
                            recovery_action="retry_wardrobe_sources",
                            required_constraint=WARDROBE_NEUTRAL_PERSON_TRIVIEW_REQUIRED_CONSTRAINT,
                            artifact_version=WARDROBE_NEUTRAL_PERSON_TRIVIEW_VERSION,
                        )
                    cursor += per_span
                stage = "白膜与当前模式所需素材均已准备完成"
                if mode == "custom":
                    stage = "随心换原人物、原服装和原场景均已提取；请分别选择使用原片或自行上传"
                elif missing_person:
                    stage = "已提取原人物图；请将其加入角色库并选择 Active 角色后生成成片"
                job.update(status="succeeded", stage=stage, progress=100, error="", recovery_action="")
                persist_wardrobe_swap_job(job, mode)
            except Exception as exc:
                job_error(job, exc)
                persist_wardrobe_swap_job(job, mode)

        threading.Thread(target=worker, daemon=True, name=f"wardrobe-extract-{mode}-{job.id}").start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/wardrobe-swap/extracted-person/character-library")
def upload_wardrobe_extracted_person_to_character_library():
    """Submit the extracted source-person board to an Ark AIGC character group."""

    local_path: Path | None = None
    upload_source: ArkCharacterUploadSource | None = None
    try:
        if not ark_assets_configured():
            raise WorkflowError("尚未配置火山素材库 AK/SK，无法提交提取人物。")
        if not form_bool("authorization_confirmed", False):
            raise WorkflowError("请先确认拥有原片人物素材及本次用途的合法授权。")
        source_job_id = request.form.get("source_job_id", "").strip()
        if not source_job_id:
            raise WorkflowError("找不到人物提取任务，请先完成素材提取。")
        try:
            job = get_job(source_job_id)
        except WorkflowError:
            job = restore_wardrobe_swap_job(source_job_id)
            if job is None:
                raise
        if job.project != WARDROBE_SWAP_PROJECT or not job.kind.startswith("wardrobe_prepare_"):
            raise WorkflowError("该任务不是衣装智换素材准备任务。")
        mode = wardrobe_mode_from_job(job)
        if mode not in {"scene", "clothing", "custom"}:
            raise WorkflowError("当前模式不需要提交原片人物；请在只更换场景、只更换服装或随心换中使用。")
        if job.status in {"queued", "running", "submitted"}:
            raise WorkflowError("人物提取任务仍在运行，请等待完成后再提交审核。")
        if not job.person_path or not job.person_path.is_file():
            raise WorkflowError("当前任务还没有可用的人物提取图，请先执行人物素材提取。")

        group_id = request.form.get("group_id", "").strip()
        if not re.fullmatch(r"group-[A-Za-z0-9_-]{6,120}", group_id):
            raise WorkflowError("请选择有效的火山 AIGC 人像组。")
        name = " ".join(request.form.get("name", "").split())[:64]
        if not name:
            raise WorkflowError("请填写人物素材名称。")

        upload_dir = timestamped_run_dir("wardrobe_extracted_character_upload")
        suffix = job.person_path.suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            suffix = ".png"
        local_path = upload_dir / f"extracted_person{suffix}"
        shutil.copy2(job.person_path, local_path)
        if local_path.stat().st_size > 30 * 1024 * 1024:
            raise WorkflowError("提取人物图片超过 30 MB，无法提交火山角色库。")

        upload_source = prepare_ark_character_upload_source(local_path, on_log=job.log)
        try:
            asset_id = ark_assets_client().create_asset(
                group_id=group_id,
                url=upload_source.url,
                name=name,
                asset_type="Image",
            )
        except Exception:
            upload_source.close()
            upload_source = None
            raise

        actor = next((item for item in job.actors if int(item.get("id") or 0) == 1), None)
        if actor is None:
            actor = {"id": 1, "role": "原片人物"}
            job.actors.append(actor)
        actor.update(
            {
                "ark_library_group_id": group_id,
                "ark_library_asset_name": name,
                "ark_library_asset_uri": f"asset://{asset_id}",
                "ark_library_upload_status": "Processing",
                "ark_library_upload_error": "",
                "local_person_source": str(job.person_path),
            }
        )
        persist_wardrobe_swap_job(job, mode)
        threading.Thread(
            target=_cleanup_ark_asset_upload,
            args=(asset_id, upload_source, local_path),
            daemon=True,
            name=f"wardrobe-ark-asset-cleanup-{asset_id[-8:]}",
        ).start()
        channel = upload_source.channel
        upload_source = None
        local_path = None
        return jsonify(
            {
                "ok": True,
                "asset_id": asset_id,
                "uri": f"asset://{asset_id}",
                "status": "Processing",
                "storage_channel": channel,
                "message": "提取人物已提交火山角色库审核；状态变为 Active 后即可选择用于成片。",
            }
        ), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        if upload_source is not None:
            upload_source.close()
        if local_path is not None and local_path.is_file():
            try:
                local_path.unlink()
            except OSError:
                pass


@app.post("/api/wardrobe-swap/extract-person")
def create_wardrobe_swap_person_extraction_job():
    """Retry only the original-person still extraction for relevant wardrobe modes."""

    try:
        job, mode = wardrobe_prepare_job_from_request()
        if mode not in {"scene", "clothing", "custom"}:
            raise WorkflowError("当前模式不需要提取原片人物。")
        if not job.white_model_path or not job.white_model_path.is_file():
            raise WorkflowError("当前任务还没有白膜视频，请先完成并预览白膜。")
        source = wardrobe_prepare_source(job)
        image_model = request.form.get("image_model", "").strip() or os.getenv(
            "ARK_IMAGE_MODEL", DEFAULT_SEEDREAM_MODEL
        )
        person_prompt = WARDROBE_NEUTRAL_PERSON_TRIVIEW_PROMPT
        job.update(
            status="running",
            stage="正在单独提取原片人物预览",
            progress=1,
            error="",
            recovery_action="",
        )
        persist_wardrobe_swap_job(job, mode)

        def worker() -> None:
            try:
                run_person_triview_extraction(
                    job,
                    source,
                    prompt=person_prompt,
                    model=image_model,
                    start=2,
                    span=96,
                    recovery_action="retry_wardrobe_person",
                    required_constraint=WARDROBE_NEUTRAL_PERSON_TRIVIEW_REQUIRED_CONSTRAINT,
                    artifact_version=WARDROBE_NEUTRAL_PERSON_TRIVIEW_VERSION,
                )
                job.update(
                    status="succeeded",
                    stage="原片人物已提取；请预览确认后提交角色库审核",
                    progress=100,
                    error="",
                    recovery_action="",
                )
                persist_wardrobe_swap_job(job, mode)
            except Exception as exc:
                job_error(job, exc)
                job.update(recovery_action="retry_wardrobe_person")
                persist_wardrobe_swap_job(job, mode)

        threading.Thread(
            target=worker,
            daemon=True,
            name=f"wardrobe-person-extract-{mode}-{job.id}",
        ).start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/wardrobe-swap/prepare")
def create_wardrobe_swap_prepare_job():
    try:
        mode = request.form.get("mode", "").strip().lower()
        if mode not in WARDROBE_SWAP_MODES:
            raise WorkflowError("请选择只更换人物、只更换场景、只更换服装或随心换。")
        job = new_job(f"wardrobe_prepare_{mode}")
        source = save_upload(request.files.get("reference_video"), job.run_dir, "reference")
        # 随心换必须从同一段原片重新提取人物、服装和场景，保证三个
        # “使用原片”选项都来自本次视频，而不是沿用旧上传或旧缓存。
        original_scene = None if mode == "custom" else save_optional_reference_image(
            job, "original_scene_image", "original_scene"
        )
        original_clothing = None if mode == "custom" else save_optional_reference_image(
            job, "original_clothing_image", "original_clothing"
        )
        original_person_asset = (
            request.form.get("person_asset", "").strip()
            if mode not in {"person", "custom"}
            else ""
        )
        if original_person_asset and not re.fullmatch(r"asset://[A-Za-z0-9_-]{6,160}", original_person_asset):
            raise WorkflowError("原人物必须选择角色库中状态为 Active 的火山 Asset。")
        if original_scene:
            job.scene_path = original_scene
        if original_clothing:
            job.clothing_path = original_clothing
        if original_person_asset:
            job.actors = [{"id": 1, "role": "原片人物", "trusted_asset_uri": original_person_asset}]
        image_model = request.form.get("image_model", "").strip() or os.getenv(
            "ARK_IMAGE_MODEL", DEFAULT_SEEDREAM_MODEL
        )
        scene_prompt = request.form.get("scene_prompt", "").strip() or DEFAULT_SCENE_EXTRACTION_PROMPT
        clothing_prompt = request.form.get("clothing_prompt", "").strip() or DEFAULT_CLOTHING_TRIVIEW_PROMPT
        person_prompt = WARDROBE_NEUTRAL_PERSON_TRIVIEW_PROMPT
        extract_missing = form_bool("extract_missing", True)
        if mode == "custom":
            extract_missing = True

        missing_scene = mode == "custom" or (mode in {"person", "clothing"} and job.scene_path is None)
        missing_clothing = mode == "custom" or (mode in {"person", "scene"} and job.clothing_path is None)
        missing_person = mode == "custom" or (
            mode in {"scene", "clothing"}
            and not wardrobe_person_triview_is_current(job)
        )
        if (missing_scene or missing_clothing or missing_person) and not extract_missing:
            missing_labels = []
            if missing_scene:
                missing_labels.append("原场景图")
            if missing_clothing:
                missing_labels.append("原服装图")
            if missing_person:
                missing_labels.append("原人物角色")
            raise WorkflowError("缺少" + "、".join(missing_labels) + "；请上传/选择，或开启 Seedream 5.0 自动提取。")

        def worker() -> None:
            try:
                working_source = normalize_wardrobe_source_duration(job, source)
                run_wardrobe_white_model(job, working_source, start=2, span=68)
                tasks: list[str] = []
                if missing_scene:
                    tasks.append("scene")
                if missing_clothing:
                    tasks.append("clothing")
                if missing_person:
                    tasks.append("person")
                per_span = max(1, 30 // max(len(tasks), 1))
                cursor = 70
                for task in tasks:
                    if task == "scene":
                        run_scene_extraction(
                            job,
                            working_source,
                            prompt=scene_prompt,
                            model=image_model,
                            start=cursor,
                            span=per_span,
                            recovery_action="retry_wardrobe_sources",
                        )
                    elif task == "clothing":
                        run_clothing_reference_extraction(
                            job,
                            working_source,
                            prompt=clothing_prompt,
                            model=image_model,
                            start=cursor,
                            span=per_span,
                        )
                    else:
                        run_person_triview_extraction(
                            job,
                            working_source,
                            prompt=person_prompt,
                            model=image_model,
                            start=cursor,
                            span=per_span,
                            recovery_action="retry_wardrobe_sources",
                            required_constraint=WARDROBE_NEUTRAL_PERSON_TRIVIEW_REQUIRED_CONSTRAINT,
                            artifact_version=WARDROBE_NEUTRAL_PERSON_TRIVIEW_VERSION,
                        )
                    cursor += per_span
                stage = "打码、白膜与源素材均已准备完成"
                if mode == "custom":
                    stage = "随心换原人物、原服装和原场景均已提取；请分别选择使用原片或自行上传"
                elif missing_person:
                    stage = "已提取原人物图；请将其加入角色库并选择 Active 角色后生成成片"
                job.update(status="succeeded", stage=stage, progress=100, error="")
                persist_wardrobe_swap_job(job, mode)
            except Exception as exc:
                job_error(job, exc)
                white_job = restore_wardrobe_white_model_job(job)
                if (
                    white_job
                    and white_job.task_id
                    and white_job.cloud_status == "succeeded"
                    and (not job.white_model_path or not job.white_model_path.is_file())
                ):
                    job.update(recovery_action="resume_wardrobe_white_download")
                    job.log("云端白膜任务已经成功；当前只需恢复结果下载，不需要再次付费生成。")
                persist_wardrobe_swap_job(job, mode)

        threading.Thread(target=worker, daemon=True, name=f"wardrobe-prepare-{mode}-{job.id}").start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


def resume_wardrobe_prepare_after_white_download(job: WebJob, mode: str) -> None:
    """Resume only a paid wardrobe white-model download, never extraction."""
    try:
        job.update(
            status="running",
            stage="正在恢复已成功的白膜结果下载",
            progress=max(70, job.progress),
            error="",
            recovery_action="",
        )
        white_job = restore_wardrobe_white_model_job(job)
        if white_job is None or not white_job.task_id:
            raise WorkflowError("没有找到可恢复的 Seedance 白膜任务 ID。")
        if not white_job.output_path or not white_job.output_path.is_file():
            job.log(f"正在恢复白膜任务 {white_job.task_id}；只查询和下载，不会重新提交付费任务。")
            resume_cloud_job(white_job)
        if white_job.status != "succeeded" or not white_job.output_path or not white_job.output_path.is_file():
            raise WorkflowError(
                white_job.error
                or f"Seedance 白膜任务 {white_job.task_id} 的结果暂未下载成功；系统没有重新提交。"
            )

        source = job.run_dir / "reference_first_15s.mp4"
        if not source.is_file():
            source = _run_artifact(job.run_dir, "reference", {".mp4", ".mov"})
        if source is None or not source.is_file():
            raise WorkflowError("找不到原片或已裁剪的 15 秒原片，无法继续白膜校时。")
        working_source = normalize_wardrobe_source_duration(job, source)
        source_info = inspect_video(working_source)
        job.update(stage="白膜已下载，正在校正回原片时长", progress=78)
        white_output = conform_video_duration(
            white_job.output_path,
            job.run_dir / "white_model.mp4",
            source_info.duration,
            with_audio=False,
        )
        job.white_model_path = white_output
        job.update(
            source_duration=source_info.duration,
            depth_duration=inspect_video(white_output).duration,
            generation_duration=match_seedance_cover_duration(source_info.duration),
            progress=100,
        )
        job.log("已从原 Seedance 任务恢复白膜并校正时长，没有创建新的视频生成任务。")
        job.update(
            status="succeeded",
            stage="白膜视频已恢复；现在可以单独提取人物、服装与场景素材",
            progress=100,
            error="",
            recovery_action="",
        )
        persist_wardrobe_swap_job(job, mode)
    except Exception as exc:
        job_error(job, exc)
        white_job = restore_wardrobe_white_model_job(job)
        if (
            white_job
            and white_job.task_id
            and white_job.cloud_status not in WARDROBE_CLOUD_FAILED_STATUSES
            and (not job.white_model_path or not job.white_model_path.is_file())
        ):
            job.update(recovery_action="resume_wardrobe_white_download")
        persist_wardrobe_swap_job(job, mode)


@app.post("/api/wardrobe-swap/recover-white-model")
def recover_wardrobe_white_model():
    """Resume only the result download of an already submitted wardrobe white model."""
    try:
        source_job_id = request.form.get("source_job_id", "").strip()
        if not source_job_id:
            raise WorkflowError("缺少需要恢复的衣装智换任务 ID。")
        try:
            job = get_job(source_job_id)
        except WorkflowError:
            job = restore_wardrobe_swap_job(source_job_id)
            if job is None:
                raise
        if job.project != WARDROBE_SWAP_PROJECT or not job.kind.startswith("wardrobe_prepare_"):
            raise WorkflowError("该任务不是衣装智换素材准备任务。")
        if job.status in {"queued", "running", "submitted"}:
            raise WorkflowError("该衣装智换任务正在运行，请勿重复恢复。")
        mode = wardrobe_mode_from_job(job)
        white_job = restore_wardrobe_white_model_job(job)
        if white_job is None or not white_job.task_id:
            raise WorkflowError("没有找到已提交的白膜任务 ID，不能执行免重复计费恢复。")
        if white_job.cloud_status in WARDROBE_CLOUD_FAILED_STATUSES:
            raise WorkflowError("该 Seedance 白膜任务已经失败，不能恢复下载；请复用现有打码视频重新生成白膜。")
        job.update(
            status="running",
            stage="正在恢复已成功的白膜任务",
            progress=max(70, job.progress),
            error="",
            recovery_action="",
        )
        persist_wardrobe_swap_job(job, mode)
        threading.Thread(
            target=resume_wardrobe_prepare_after_white_download,
            args=(job, mode),
            daemon=True,
            name=f"wardrobe-recover-white-{job.id}",
        ).start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/wardrobe-swap/generate")
def create_wardrobe_swap_generation_job():
    try:
        source_job_id = request.form.get("source_job_id", "").strip()
        if not source_job_id:
            raise WorkflowError("请先完成原片打码、白膜和源素材准备。")
        try:
            source_job = get_job(source_job_id)
        except WorkflowError:
            source_job = restore_wardrobe_swap_job(source_job_id)
            if source_job is None:
                raise
        if source_job.project != WARDROBE_SWAP_PROJECT:
            raise WorkflowError("所选任务不属于衣装智换。")
        mode = wardrobe_mode_from_job(source_job)
        requested_mode = request.form.get("mode", mode).strip().lower()
        if requested_mode != mode:
            raise WorkflowError("当前模式与已准备的白膜任务不一致，请重新准备该模式。")
        if not source_job.white_model_path or not source_job.white_model_path.is_file():
            raise WorkflowError("当前任务没有可用的白膜视频。")
        person_asset = ""
        if mode != "custom":
            person_asset = requested_character_asset()
            if not person_asset and mode != "person":
                person_asset = next(
                    (
                        str(actor.get("trusted_asset_uri") or "")
                        for actor in source_job.actors
                        if str(actor.get("trusted_asset_uri") or "").startswith("asset://")
                    ),
                    "",
                )
            if not person_asset:
                role_label = "新人物" if mode == "person" else "原片人物"
                raise WorkflowError(f"请先从火山角色库选择状态为 Active 的{role_label}。")

        job = new_job(f"wardrobe_generate_{mode}")
        job.white_model_path = source_job.white_model_path
        job.mosaic_path = source_job.mosaic_path
        resolution = request.form.get("resolution", "720p").strip()
        if resolution not in {"480p", "720p"}:
            raise WorkflowError("Seedance 2.5 成片分辨率只支持 480p 或 720p。")
        scene_source = ""
        clothing_source = ""
        person_source = person_asset
        if mode == "person":
            if not source_job.scene_path or not source_job.scene_path.is_file():
                raise WorkflowError("缺少原片背景图，请上传或先用 Seedream 5.0 提取。")
            if not source_job.clothing_path or not source_job.clothing_path.is_file():
                raise WorkflowError("缺少原片服装图，请上传或先用 Seedream 5.0 提取。")
            job.scene_path = source_job.scene_path
            job.clothing_path = source_job.clothing_path
            scene_source = str(job.scene_path)
            clothing_source = str(job.clothing_path)
        elif mode == "scene":
            if not source_job.clothing_path or not source_job.clothing_path.is_file():
                raise WorkflowError("缺少原片服装图，请上传或先用 Seedream 5.0 提取。")
            new_scene = save_optional_reference_image(job, "new_scene_image", "new_scene")
            if new_scene is None:
                raise WorkflowError("请选择需要替换的新场景图。")
            job.scene_path = new_scene
            job.clothing_path = source_job.clothing_path
            scene_source = str(new_scene)
            clothing_source = str(job.clothing_path)
        elif mode == "clothing":
            if not source_job.scene_path or not source_job.scene_path.is_file():
                raise WorkflowError("缺少原片场景图，请上传或先用 Seedream 5.0 提取。")
            new_clothing = save_optional_reference_image(job, "new_clothing_image", "new_clothing")
            if new_clothing is None:
                raise WorkflowError("请选择需要替换的新服装图。")
            job.scene_path = source_job.scene_path
            job.clothing_path = new_clothing
            scene_source = str(job.scene_path)
            clothing_source = str(new_clothing)
        else:
            choices = {
                "person": request.form.get("custom_person_choice", "original").strip().lower(),
                "clothing": request.form.get("custom_clothing_choice", "original").strip().lower(),
                "scene": request.form.get("custom_scene_choice", "original").strip().lower(),
            }
            invalid_choices = [name for name, choice in choices.items() if choice not in {"original", "upload"}]
            if invalid_choices:
                raise WorkflowError("随心换参考来源无效，请重新选择原片提取图或自行上传。")

            def select_custom_reference(
                role: str,
                source_path: Path | None,
                upload_field: str,
                upload_stem: str,
                label: str,
            ) -> Path:
                if choices[role] == "upload":
                    uploaded = save_optional_reference_image(job, upload_field, upload_stem)
                    if uploaded is None:
                        raise WorkflowError(f"{label}已选择自行上传，但尚未上传图片。")
                    return uploaded
                if source_path is None or not source_path.is_file():
                    raise WorkflowError(f"缺少从原片提取的{label}，请重新准备随心换素材。")
                return source_path

            job.person_path = select_custom_reference(
                "person", source_job.person_path, "custom_person_image", "custom_person", "人物图"
            )
            job.clothing_path = select_custom_reference(
                "clothing", source_job.clothing_path, "custom_clothing_image", "custom_clothing", "服装图"
            )
            job.scene_path = select_custom_reference(
                "scene", source_job.scene_path, "custom_scene_image", "custom_scene", "场景图"
            )
            person_source = str(job.person_path)
            clothing_source = str(job.clothing_path)
            scene_source = str(job.scene_path)

        # Ark validates local image geometry after the paid task is created. Do
        # the same checks locally and repair invalid boards before submission so
        # an over-wide three-view image cannot consume a failed request again.
        job.scene_path = prepare_wardrobe_seedance_reference(
            job,
            scene_source,
            role="scene",
            label="场景",
        )
        scene_source = str(job.scene_path)
        job.clothing_path = prepare_wardrobe_seedance_reference(
            job,
            clothing_source,
            role="clothing",
            label="服装",
        )
        clothing_source = str(job.clothing_path)
        if mode == "custom":
            job.person_path = prepare_wardrobe_seedance_reference(
                job,
                person_source,
                role="person",
                label="人物",
            )
            person_source = str(job.person_path)

        options = {
            "generation_channel": "api",
            "prompt": build_wardrobe_swap_prompt(mode),
            "model": DEFAULT_SEEDANCE_25_MODEL,
            "resolution": resolution,
            "ratio": "adaptive",
            "duration": -1,
            "generate_audio": form_bool("generate_audio", False),
            "watermark": False,
            "delete_tos_after": True,
            "reference_upload_strategy": "stable",
        }
        custom_prompt = request.form.get("prompt", "").strip()
        if custom_prompt and custom_prompt != options["prompt"]:
            options["prompt"] = f"{options['prompt']}\n补充要求：{custom_prompt[:500]}"
        job.log(
            f"衣装智换·{WARDROBE_MODE_LABELS[mode]}：将使用 Seedance 2.5 / {resolution}，"
            "白膜锁定时空，新旧人物、服装与场景按三张参考图严格分工。"
        )
        threading.Thread(
            target=run_generation,
            kwargs={
                "job": job,
                "depth_path": job.white_model_path,
                "depth_reference": "",
                "person_source": person_source,
                "clothing_source": clothing_source,
                "scene_source": scene_source,
                "options": options,
                "on_finished": lambda completed_job: persist_wardrobe_generation_result(
                    completed_job,
                    source_job,
                    mode,
                ),
            },
            daemon=True,
            name=f"wardrobe-generate-{mode}-{job.id}",
        ).start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/scene-only/prepare")
def create_scene_only_prepare_job():
    try:
        job = new_job("scene_prepare")
        source = save_upload(request.files.get("reference_video"), job.run_dir, "reference")
        blur_range = request.form.get("blur_range", "").strip()
        person_prompt = request.form.get("person_prompt", "").strip() or DEFAULT_PERSON_TRIVIEW_PROMPT
        clothing_prompt = request.form.get("clothing_prompt", "").strip() or DEFAULT_CLOTHING_TRIVIEW_PROMPT
        image_model = request.form.get("image_model", "").strip() or os.getenv(
            "ARK_IMAGE_MODEL", DEFAULT_SEEDREAM_MODEL
        )

        def worker() -> None:
            try:
                run_depth(job, source, job.run_dir / "depth.mp4", blur_range, start=0, span=55)
                run_original_subject_extraction(
                    job,
                    source,
                    person_prompt=person_prompt,
                    clothing_prompt=clothing_prompt,
                    model=image_model,
                    start=55,
                    span=45,
                )
                job.update(status="succeeded", stage="深度、原人物与原服装参考均已准备完成", progress=100)
            except Exception as exc:
                job_error(job, exc)

        threading.Thread(target=worker, daemon=True, name=f"scene-prepare-{job.id}").start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/scene-only/generate")
def create_scene_only_generation_job():
    try:
        job = new_job("scene_generate")
        source_job_id = request.form.get("source_job_id", "").strip()
        if not source_job_id:
            raise WorkflowError("请先生成深度视频、原人物三视图和原服装三视图。")
        source_job = get_job(source_job_id)
        if source_job.project != "scene_only_replacement":
            raise WorkflowError("所选素材任务不属于项目 4。")
        if not source_job.depth_path or not source_job.depth_path.is_file():
            raise WorkflowError("所选任务没有可用的深度视频。")
        if not source_job.person_path or not source_job.person_path.is_file():
            raise WorkflowError("所选任务没有可用的原人物三视图。")
        if not source_job.clothing_path or not source_job.clothing_path.is_file():
            raise WorkflowError("所选任务没有可用的原服装三视图。")
        job.depth_path = source_job.depth_path
        job.person_path = source_job.person_path
        job.clothing_path = source_job.clothing_path
        person_source = requested_character_asset() or str(job.person_path)
        scene_source = save_scene_only_target(job, reuse_job=source_job)
        options = generation_options()
        options["prompt"] = request.form.get("prompt", "").strip() or build_scene_only_prompt()
        apply_automatic_duration(job, options, job.depth_path)
        depth_reference = request.form.get("depth_reference", "").strip()
        threading.Thread(
            target=run_generation,
            kwargs={
                "job": job,
                "depth_path": job.depth_path,
                "depth_reference": depth_reference,
                "person_source": person_source,
                "clothing_source": str(job.clothing_path),
                "scene_source": scene_source,
                "options": options,
            },
            daemon=True,
            name=f"scene-generate-{job.id}",
        ).start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/scene-only/retry-subject")
def retry_scene_only_subject_job():
    try:
        source_job_id = request.form.get("source_job_id", "").strip()
        if not source_job_id:
            raise WorkflowError("缺少需要恢复的项目 4 任务 ID。")
        job = get_job(source_job_id)
        if job.project != "scene_only_replacement":
            raise WorkflowError("该任务不属于项目 4。")
        if not job.depth_path or not job.depth_path.is_file():
            raise WorkflowError("中断任务没有可复用的深度视频。")
        if job.status in {"queued", "running"}:
            raise WorkflowError("人物服装提取已经在运行，请勿重复提交。")
        source = _run_artifact(job.run_dir, "reference", {".mp4", ".mov"})
        if source is None:
            raise WorkflowError("找不到中断任务保存的原视频。")
        person_prompt = request.form.get("person_prompt", "").strip() or DEFAULT_PERSON_TRIVIEW_PROMPT
        clothing_prompt = request.form.get("clothing_prompt", "").strip() or DEFAULT_CLOTHING_TRIVIEW_PROMPT
        image_model = request.form.get("image_model", "").strip() or os.getenv(
            "ARK_IMAGE_MODEL", DEFAULT_SEEDREAM_MODEL
        )
        job.update(
            kind="scene_prepare_retry",
            status="queued",
            stage="已确认安全重试人物服装提取",
            progress=max(55, min(job.progress, 82)),
            error="",
            recovery_action="",
        )
        job.log("将复用深度视频与已成功的三视图，只补充缺失的 Seedream 结果。")

        def worker() -> None:
            try:
                start = max(55, min(job.progress, 82))
                run_original_subject_extraction(
                    job,
                    source,
                    person_prompt=person_prompt,
                    clothing_prompt=clothing_prompt,
                    model=image_model,
                    start=start,
                    span=100 - start,
                    reuse_existing=True,
                )
                job.update(status="succeeded", stage="原人物与原服装三视图已恢复", progress=100)
            except Exception as exc:
                job_error(job, exc)

        threading.Thread(target=worker, daemon=True, name=f"scene-subject-retry-{job.id}").start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/scene-only/full")
def create_scene_only_full_job():
    try:
        job = new_job("scene_full")
        source = save_upload(request.files.get("reference_video"), job.run_dir, "reference")
        scene_source = save_scene_only_target(job)
        person_asset = requested_character_asset()
        options = generation_options()
        options["prompt"] = request.form.get("prompt", "").strip() or build_scene_only_prompt()
        blur_range = request.form.get("blur_range", "").strip()
        depth_reference = request.form.get("depth_reference", "").strip()
        person_prompt = request.form.get("person_prompt", "").strip() or DEFAULT_PERSON_TRIVIEW_PROMPT
        clothing_prompt = request.form.get("clothing_prompt", "").strip() or DEFAULT_CLOTHING_TRIVIEW_PROMPT
        image_model = request.form.get("image_model", "").strip() or os.getenv(
            "ARK_IMAGE_MODEL", DEFAULT_SEEDREAM_MODEL
        )

        def worker() -> None:
            try:
                depth_path = run_depth(
                    job, source, job.run_dir / "depth.mp4", blur_range, start=0, span=35
                )
                person_path, clothing_path = run_original_subject_extraction(
                    job,
                    source,
                    person_prompt=person_prompt,
                    clothing_prompt=clothing_prompt,
                    model=image_model,
                    start=35,
                    span=25,
                )
                options["duration"] = job.generation_duration or match_seedance_duration(
                    inspect_video(depth_path).duration
                )
                run_generation(
                    job,
                    depth_path=depth_path,
                    depth_reference=depth_reference,
                    person_source=person_asset or str(person_path),
                    clothing_source=str(clothing_path),
                    scene_source=scene_source,
                    options=options,
                    progress_start=60,
                )
            except Exception as exc:
                job_error(job, exc)

        threading.Thread(target=worker, daemon=True, name=f"scene-full-{job.id}").start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/person-only/prepare")
def create_person_only_prepare_job():
    try:
        job = new_job("person_prepare")
        source = save_upload(request.files.get("reference_video"), job.run_dir, "reference")
        blur_range = request.form.get("blur_range", "").strip()
        scene_prompt = request.form.get("scene_prompt", "").strip() or DEFAULT_SCENE_EXTRACTION_PROMPT
        image_model = request.form.get("image_model", "").strip() or os.getenv(
            "ARK_IMAGE_MODEL", DEFAULT_SEEDREAM_MODEL
        )

        def worker() -> None:
            try:
                run_depth(job, source, job.run_dir / "depth.mp4", blur_range, start=0, span=65)
                run_scene_extraction(
                    job,
                    source,
                    prompt=scene_prompt,
                    model=image_model,
                    start=65,
                    span=35,
                )
                job.update(status="succeeded", stage="深度视频与原片场景均已准备完成", progress=100)
            except Exception as exc:
                job_error(job, exc)

        threading.Thread(
            target=worker,
            daemon=True,
            name=f"person-prepare-{job.id}",
        ).start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/person-only/generate")
def create_person_only_generation_job():
    try:
        job = new_job("person_generate")
        source_job_id = request.form.get("source_job_id", "").strip()
        if not source_job_id:
            raise WorkflowError("请先生成深度视频和原片场景参考。")
        source_job = get_job(source_job_id)
        if not source_job.depth_path or not source_job.depth_path.is_file():
            raise WorkflowError("所选任务没有可用的深度视频。")
        if not source_job.scene_path or not source_job.scene_path.is_file():
            raise WorkflowError("所选任务没有可用的原片场景参考图。")
        job.depth_path = source_job.depth_path
        job.scene_path = source_job.scene_path
        person_source, clothing_source = save_person_only_references(job, reuse_job=source_job)
        options = generation_options()
        options["prompt"] = request.form.get("prompt", "").strip() or build_person_only_prompt()
        apply_automatic_duration(job, options, job.depth_path)
        depth_reference = request.form.get("depth_reference", "").strip()
        threading.Thread(
            target=run_generation,
            kwargs={
                "job": job,
                "depth_path": job.depth_path,
                "depth_reference": depth_reference,
                "person_source": person_source,
                "clothing_source": clothing_source,
                "scene_source": str(job.scene_path),
                "options": options,
            },
            daemon=True,
            name=f"person-generate-{job.id}",
        ).start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/person-only/retry-scene")
def retry_person_only_scene_job():
    """Explicitly retry only Seedream after an ambiguous connection reset."""
    try:
        source_job_id = request.form.get("source_job_id", "").strip()
        if not source_job_id:
            raise WorkflowError("缺少需要恢复的项目 3 任务 ID。")
        try:
            job = get_job(source_job_id)
        except WorkflowError:
            job = restore_person_retry_job(source_job_id)
            if job is None:
                raise
        if job.project != "person_only_replacement":
            raise WorkflowError("该任务不属于项目 3，不能执行场景提取重试。")
        if not job.depth_path or not job.depth_path.is_file():
            raise WorkflowError("中断任务没有可复用的深度视频。")
        if job.scene_path and job.scene_path.is_file():
            raise WorkflowError("场景参考图已经生成，无需重复调用 Seedream。")
        if job.status in {"queued", "running"}:
            raise WorkflowError("场景提取任务已经在运行，请勿重复提交。")
        source = _run_artifact(job.run_dir, "reference", {".mp4", ".mov"})
        if source is None:
            raise WorkflowError("找不到中断任务保存的原视频，无法重试场景提取。")
        scene_prompt = request.form.get("scene_prompt", "").strip() or DEFAULT_SCENE_EXTRACTION_PROMPT
        image_model = request.form.get("image_model", "").strip() or os.getenv(
            "ARK_IMAGE_MODEL", DEFAULT_SEEDREAM_MODEL
        )
        job.update(
            kind="person_prepare_retry",
            status="queued",
            stage="已确认重试 Seedream，正在复用现有深度视频",
            progress=max(47, min(job.progress, 73)),
            error="",
            recovery_action="",
        )
        job.log("用户已确认只重试 Seedream 场景提取；不会重跑深度，也不会自动提交 Seedance。")

        def worker() -> None:
            try:
                start = max(47, min(job.progress, 73))
                run_scene_extraction(
                    job,
                    source,
                    prompt=scene_prompt,
                    model=image_model,
                    start=start,
                    span=100 - start,
                )
                job.update(status="succeeded", stage="场景参考已恢复，可继续生成最终成片", progress=100)
            except Exception as exc:
                job_error(job, exc)

        threading.Thread(
            target=worker,
            daemon=True,
            name=f"person-scene-retry-{job.id}",
        ).start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/person-only/recover-seedance")
@app.post("/api/scene-only/recover-seedance")
@app.post("/api/clothing-only/recover-seedance")
def recover_person_only_seedance_job():
    """Attach an ambiguous Project 3/4/5 submission to its already-created Ark task."""
    try:
        if request.path.startswith("/api/scene-only/"):
            expected_project, project_label = "scene_only_replacement", "项目 4"
        elif request.path.startswith("/api/clothing-only/"):
            expected_project, project_label = "clothing_only_replacement", "项目 5"
        else:
            expected_project, project_label = "person_only_replacement", "项目 3"
        source_job_id = request.form.get("source_job_id", "").strip()
        if not source_job_id:
            raise WorkflowError(f"缺少需要找回的{project_label}任务 ID。")
        try:
            job = get_job(source_job_id)
        except WorkflowError:
            if expected_project == "scene_only_replacement":
                candidate = restore_latest_scene_only_job()
                job = candidate if candidate and candidate.id == source_job_id else None
            elif expected_project == "clothing_only_replacement":
                candidate = restore_latest_clothing_only_job()
                job = candidate if candidate and candidate.id == source_job_id else None
            else:
                job = restore_person_submission_job(source_job_id)
            if job is None:
                raise
        if job.project != expected_project:
            raise WorkflowError(f"该任务不属于{project_label}，不能执行 Seedance 安全找回。")
        if job.output_path and job.output_path.is_file():
            return jsonify(job.public())
        if job.task_id:
            raise WorkflowError(f"该任务已经绑定 Seedance 任务 ID：{job.task_id}")
        if not job.depth_path or not job.depth_path.is_file():
            raise WorkflowError("找不到本次提交使用的深度视频。")

        record: dict[str, Any] = {}
        record_path = job.run_dir / "job.json"
        if record_path.is_file():
            try:
                loaded = json.loads(record_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    record = loaded
            except (OSError, ValueError, TypeError):
                pass
        model = request.form.get("model", "").strip() or str(record.get("model") or "").strip() or os.getenv(
            "ARK_VIDEO_MODEL", DEFAULT_SEEDANCE_MODEL
        )
        resolution = request.form.get("resolution", "").strip() or str(record.get("resolution") or "720p")
        requested_ratio = request.form.get("ratio", "").strip() or str(
            record.get("requested_ratio") or record.get("ratio") or "adaptive"
        )
        expected_ratio = str(record.get("expected_ratio") or "").strip() or resolved_seedance_ratio(
            requested_ratio, job.depth_path
        )
        duration = form_int("duration", int(record.get("duration") or job.generation_duration or 5))
        generate_audio = form_bool("generate_audio", bool(record.get("generate_audio", False)))
        known_task_ids = {
            str(item).strip() for item in (record.get("known_task_ids") or []) if str(item).strip()
        }
        try:
            created_after = float(record.get("submitted_at") or 0)
        except (TypeError, ValueError):
            created_after = 0
        if created_after <= 0:
            created_after = job.scene_path.stat().st_mtime if job.scene_path else job.run_dir.stat().st_mtime
        try:
            created_before = float(record.get("created_before") or 0)
        except (TypeError, ValueError):
            created_before = 0
        if created_before <= 0:
            created_before = created_after + 240

        job.update(status="running", stage="正在只读查找已创建的 Seedance 任务", progress=60, error="")
        job.log(
            "正在按提交指纹查找："
            f"{model} / {resolution} / {expected_ratio or requested_ratio} / {duration} 秒 / "
            f"声音{'开' if generate_audio else '关'}；不会创建新任务。"
        )
        task_id = api_client().recover_created_task(
            known_task_ids,
            model=model,
            created_after=created_after,
            created_before=created_before,
            resolution=resolution,
            ratio=expected_ratio,
            duration=duration,
            generate_audio=generate_audio,
            attempts=1,
        )
        if not task_id:
            job.update(
                status="failed",
                stage="未找到唯一匹配的 Seedance 任务",
                recovery_action="recover_seedance_submission",
            )
            raise WorkflowError(
                "没有找到唯一匹配的已创建任务。系统没有重新提交，也不会重复计费；"
                "请使用火山方舟任务 ID 手动恢复。"
            )

        now = time.time()
        job.update(
            task_id=task_id,
            status="running",
            stage="已找回 Seedance 任务，正在恢复成片",
            progress=65,
            cloud_status="queued",
            cloud_started_at=created_after,
            cloud_updated_at=now,
            generation_duration=duration,
            recovery_action="",
        )
        job.log(f"已唯一匹配并找回 Seedance 任务：{task_id}")
        persist_cloud_job(
            job,
            status="running",
            model=model,
            resolution=resolution,
            ratio=requested_ratio,
            expected_ratio=expected_ratio,
            duration=duration,
            generate_audio=generate_audio,
        )
        threading.Thread(
            target=resume_cloud_job,
            args=(job,),
            daemon=True,
            name=f"recover-seedance-{job.id}",
        ).start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/person-only/full")
def create_person_only_full_job():
    try:
        job = new_job("person_full")
        source = save_upload(request.files.get("reference_video"), job.run_dir, "reference")
        person_source, clothing_source = save_person_only_references(job)
        options = generation_options()
        options["prompt"] = request.form.get("prompt", "").strip() or build_person_only_prompt()
        blur_range = request.form.get("blur_range", "").strip()
        depth_reference = request.form.get("depth_reference", "").strip()
        scene_prompt = request.form.get("scene_prompt", "").strip() or DEFAULT_SCENE_EXTRACTION_PROMPT
        image_model = request.form.get("image_model", "").strip() or os.getenv(
            "ARK_IMAGE_MODEL", DEFAULT_SEEDREAM_MODEL
        )

        def worker() -> None:
            try:
                depth_path = run_depth(
                    job,
                    source,
                    job.run_dir / "depth.mp4",
                    blur_range,
                    start=0,
                    span=35,
                )
                scene_path = run_scene_extraction(
                    job,
                    source,
                    prompt=scene_prompt,
                    model=image_model,
                    start=35,
                    span=15,
                )
                options["duration"] = job.generation_duration or match_seedance_duration(
                    inspect_video(depth_path).duration
                )
                run_generation(
                    job,
                    depth_path=depth_path,
                    depth_reference=depth_reference,
                    person_source=person_source,
                    clothing_source=clothing_source,
                    scene_source=str(scene_path),
                    options=options,
                    progress_start=50,
                )
            except Exception as exc:
                job_error(job, exc)

        threading.Thread(
            target=worker,
            daemon=True,
            name=f"person-full-{job.id}",
        ).start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/clothing-only/prepare")
def create_clothing_only_prepare_job():
    try:
        job = new_job("clothing_prepare")
        source = save_upload(request.files.get("reference_video"), job.run_dir, "reference")
        blur_range = request.form.get("blur_range", "").strip()
        person_prompt = request.form.get("person_prompt", "").strip() or DEFAULT_CLOTHING_PERSON_TRIVIEW_PROMPT
        scene_prompt = request.form.get("scene_prompt", "").strip() or DEFAULT_SCENE_EXTRACTION_PROMPT
        image_model = request.form.get("image_model", "").strip() or os.getenv(
            "ARK_IMAGE_MODEL", DEFAULT_SEEDREAM_MODEL
        )

        def worker() -> None:
            try:
                run_depth(job, source, job.run_dir / "depth.mp4", blur_range, start=0, span=52)
                run_clothing_only_extraction(
                    job,
                    source,
                    person_prompt=person_prompt,
                    scene_prompt=scene_prompt,
                    model=image_model,
                    start=52,
                    span=48,
                )
                job.update(status="succeeded", stage="深度、原人物与原场景参考均已准备完成", progress=100)
            except Exception as exc:
                job_error(job, exc)

        threading.Thread(target=worker, daemon=True, name=f"clothing-prepare-{job.id}").start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/clothing-only/generate")
def create_clothing_only_generation_job():
    try:
        job = new_job("clothing_generate")
        source_job_id = request.form.get("source_job_id", "").strip()
        if not source_job_id:
            raise WorkflowError("请先生成深度视频、原人物三视图和原片场景参考。")
        try:
            source_job = get_job(source_job_id)
        except WorkflowError:
            source_job = restore_latest_clothing_only_job()
            if source_job is None or source_job.id != source_job_id:
                raise
        if source_job.project != "clothing_only_replacement":
            raise WorkflowError("所选任务不属于项目 5。")
        if not source_job.depth_path or not source_job.depth_path.is_file():
            raise WorkflowError("所选任务没有可用的深度视频。")
        if not source_job.person_path or not source_job.person_path.is_file():
            raise WorkflowError("所选任务没有可用的原人物三视图。")
        if not project_five_person_reference_is_current(source_job):
            raise WorkflowError(
                "当前人物三视图由旧规则生成，仍可能包含原片服装，已禁止继续使用。"
                "请重新提交原视频并点击“生成深度 + 提取人物与场景”，生成白色底衫人物三视图后再生成成片。"
            )
        if not source_job.scene_path or not source_job.scene_path.is_file():
            raise WorkflowError("所选任务没有可用的原片场景参考图。")
        job.depth_path = source_job.depth_path
        job.person_path = source_job.person_path
        job.scene_path = source_job.scene_path
        person_source = requested_character_asset() or str(job.person_path)
        clothing_source = save_new_clothing(job, reuse_job=source_job)
        options = generation_options()
        options["prompt"] = build_clothing_only_prompt(request.form.get("prompt", ""))
        apply_automatic_duration(job, options, job.depth_path)
        depth_reference = request.form.get("depth_reference", "").strip()
        threading.Thread(
            target=run_generation,
            kwargs={
                "job": job,
                "depth_path": job.depth_path,
                "depth_reference": depth_reference,
                "person_source": person_source,
                "clothing_source": clothing_source,
                "scene_source": str(job.scene_path),
                "options": options,
            },
            daemon=True,
            name=f"clothing-generate-{job.id}",
        ).start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/clothing-only/retry-references")
def retry_clothing_only_references_job():
    try:
        source_job_id = request.form.get("source_job_id", "").strip()
        if not source_job_id:
            raise WorkflowError("缺少需要恢复的项目 5 任务 ID。")
        try:
            job = get_job(source_job_id)
        except WorkflowError:
            job = restore_latest_clothing_only_job()
            if job is None or job.id != source_job_id:
                raise
        if job.project != "clothing_only_replacement":
            raise WorkflowError("该任务不属于项目 5。")
        if job.status in {"queued", "running"}:
            raise WorkflowError("参考提取已经在运行，请勿重复提交。")
        source = _run_artifact(job.run_dir, "reference", {".mp4", ".mov"})
        if source is None:
            raise WorkflowError("找不到项目 5 保存的原视频。")
        person_prompt = request.form.get("person_prompt", "").strip() or DEFAULT_CLOTHING_PERSON_TRIVIEW_PROMPT
        scene_prompt = request.form.get("scene_prompt", "").strip() or DEFAULT_SCENE_EXTRACTION_PROMPT
        image_model = request.form.get("image_model", "").strip() or os.getenv(
            "ARK_IMAGE_MODEL", DEFAULT_SEEDREAM_MODEL
        )
        job.update(status="queued", stage="正在补充缺失的人物或场景参考", error="", recovery_action="")

        def worker() -> None:
            try:
                run_clothing_only_extraction(
                    job,
                    source,
                    person_prompt=person_prompt,
                    scene_prompt=scene_prompt,
                    model=image_model,
                    start=max(52, min(job.progress, 82)),
                    span=max(18, 100 - max(52, min(job.progress, 82))),
                    reuse_existing=True,
                )
                job.update(status="succeeded", stage="原人物与原场景参考均已恢复", progress=100)
            except Exception as exc:
                job_error(job, exc)

        threading.Thread(target=worker, daemon=True, name=f"clothing-retry-{job.id}").start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/clothing-only/full")
def create_clothing_only_full_job():
    try:
        job = new_job("clothing_full")
        source = save_upload(request.files.get("reference_video"), job.run_dir, "reference")
        clothing_source = save_new_clothing(job)
        person_asset = requested_character_asset()
        options = generation_options()
        options["prompt"] = build_clothing_only_prompt(request.form.get("prompt", ""))
        blur_range = request.form.get("blur_range", "").strip()
        depth_reference = request.form.get("depth_reference", "").strip()
        person_prompt = request.form.get("person_prompt", "").strip() or DEFAULT_CLOTHING_PERSON_TRIVIEW_PROMPT
        scene_prompt = request.form.get("scene_prompt", "").strip() or DEFAULT_SCENE_EXTRACTION_PROMPT
        image_model = request.form.get("image_model", "").strip() or os.getenv(
            "ARK_IMAGE_MODEL", DEFAULT_SEEDREAM_MODEL
        )

        def worker() -> None:
            try:
                depth_path = run_depth(job, source, job.run_dir / "depth.mp4", blur_range, start=0, span=34)
                person_path, scene_path = run_clothing_only_extraction(
                    job,
                    source,
                    person_prompt=person_prompt,
                    scene_prompt=scene_prompt,
                    model=image_model,
                    start=34,
                    span=26,
                )
                options["duration"] = job.generation_duration or match_seedance_duration(inspect_video(depth_path).duration)
                run_generation(
                    job,
                    depth_path=depth_path,
                    depth_reference=depth_reference,
                    person_source=person_asset or str(person_path),
                    clothing_source=clothing_source,
                    scene_source=str(scene_path),
                    options=options,
                    progress_start=60,
                )
            except Exception as exc:
                job_error(job, exc)

        threading.Thread(target=worker, daemon=True, name=f"clothing-full-{job.id}").start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


def update_long_shot(job: WebJob, index: int, **values: Any) -> None:
    with job.lock:
        for shot in job.shots:
            if int(shot.get("index") or 0) == int(index):
                shot.update(values)
                break


def run_long_mosaic_preparation(
    job: WebJob,
    *,
    block_size: int,
    score_threshold: float,
) -> None:
    """Create privacy-safe reference clips before any video is uploaded."""
    try:
        outputs: list[Path] = []
        total = len(job.shots)
        for ordinal, shot in enumerate(list(job.shots), start=1):
            index = int(shot.get("index") or ordinal)
            source = Path(str(shot.get("source_path") or ""))
            if not source.is_file():
                raise WorkflowError(f"找不到分镜 {index:02d} 原片。")
            shot_dir = source.parent
            output = shot_dir / "face_mosaic.mp4"
            update_long_shot(job, index, status="running", stage="正在检测并跟踪全部人脸", progress=0, error="")
            job.update(
                stage=f"正在生成打码分镜 {index:02d}/{total}",
                progress=int(4 + 88 * (ordinal - 1) / max(total, 1)),
            )

            def on_progress(current: int, frame_total: int, maximum_faces: int) -> None:
                local_progress = int(100 * current / max(frame_total, 1))
                update_long_shot(
                    job,
                    index,
                    stage=f"人脸打码中 · 当前最多 {maximum_faces} 张脸",
                    progress=min(99, local_progress),
                )

            result = render_face_mosaic_video(
                source,
                output,
                block_size=block_size,
                score_threshold=score_threshold,
                on_progress=on_progress,
                on_log=job.log,
            )
            outputs.append(output)
            update_long_shot(
                job,
                index,
                mosaic_path=str(output),
                mosaic_stats={key: value for key, value in result.items() if key != "output"},
                mosaic_reviewed=False,
                status="ready",
                stage="打码完成，等待人工复核",
                progress=100,
                error="",
            )
            job.log(
                f"分镜 {index:02d} 打码完成：{result['frames_with_faces']}/{result['frames']} 帧包含人脸遮罩，"
                f"单帧最多 {result['maximum_faces']} 张脸。"
            )
            _persist_long_job(job)

        job.update(stage="正在合并完整打码视频", progress=94)
        combined = concatenate_videos(outputs, job.run_dir / "人脸打码视频.mp4")
        job.mosaic_path = combined
        job.update(status="succeeded", stage="打码视频已完成，请逐镜复核", progress=100, error="")
        job.log("完整打码视频已生成。必须人工确认没有漏脸后才能提交白模生成。")
        _persist_long_job(job)
    except Exception as exc:
        job_error(job, exc)
        _persist_long_job(job)


def cast_continuity_covers_slots(
    continuity: dict[str, Any] | None,
    shot_slot_counts: dict[int, int],
) -> bool:
    """Return whether one normalized continuity result covers the current shot layout exactly."""
    if not isinstance(continuity, dict) or continuity.get("manual_review_required") is True:
        return False
    expected = {
        (int(shot_index), slot)
        for shot_index, count in shot_slot_counts.items()
        for slot in range(1, int(count) + 1)
    }
    actual: set[tuple[int, int]] = set()
    for item in continuity.get("assignments") or []:
        if not isinstance(item, dict):
            continue
        try:
            key = (int(item.get("shot_index") or 0), int(item.get("slot") or 0))
            character_id = int(item.get("character_id") or 0)
        except (TypeError, ValueError):
            continue
        if key[0] > 0 and key[1] > 0 and character_id > 0:
            actual.add(key)
    return actual == expected


def analyze_long_cast_continuity(
    job: WebJob,
    analyzer: ArkPerformanceAnalyzer,
    video_url: str,
    *,
    shot_slot_counts: dict[int, int],
    shot_ranges: list[dict[str, Any]],
    max_characters: int = 4,
) -> dict[str, Any]:
    """Repair/retry real-person continuity output, then safely yield to manual per-shot mapping."""
    real_person_mode = is_real_person_long_job(job)
    if real_person_mode and cast_continuity_covers_slots(job.cast_continuity, shot_slot_counts):
        job.log(
            "已存在覆盖当前全部分镜槽位的有效跨镜身份结果，直接复用；"
            "不会因重复点击分析而再次调用方舟或覆盖成功结果。"
        )
        return dict(job.cast_continuity)
    attempts = 3 if real_person_mode else 1
    expected_slots = "、".join(
        f"分镜{shot_index:02d}-P{slot}"
        for shot_index, count in sorted(shot_slot_counts.items())
        for slot in range(1, count + 1)
    )
    retry_instruction = ""
    last_error: WorkflowError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return analyzer.analyze_cast_continuity(
                video_url,
                shot_slot_counts=shot_slot_counts,
                shot_ranges=shot_ranges,
                max_characters=max_characters,
                retry_instruction=retry_instruction,
            )
        except ArkAPIError as exc:
            if not real_person_mode:
                raise
            last_error = exc
            job.log(
                "真实人物跨镜身份分析被方舟接口拒绝；已保留全部逐镜台词与表演结果，"
                "改用逐镜人工人物绑定，不会提交 Seedance 视频任务。"
            )
            break
        except (ArkConnectionError, WorkflowError) as exc:
            if not real_person_mode:
                raise
            last_error = exc
            if attempt >= attempts:
                break
            failure_kind = "连接中断" if isinstance(exc, ArkConnectionError) else "JSON格式或槽位完整性校验失败"
            job.log(
                f"真实人物跨镜身份分析{failure_kind}：{str(exc)[:300]}。正在自动纠错重试 "
                f"{attempt}/{attempts - 1}；逐镜分析结果会直接复用，不会提交 Seedance 视频任务。"
            )
            retry_instruction = (
                f"上一次结果失败：{str(exc)[:300]}。本次只输出一个合法JSON对象；"
                f"assignments必须恰好覆盖以下所有槽位且不得重复或遗漏：{expected_slots}。"
            )
            time.sleep(float(attempt))
    if real_person_mode:
        message = str(last_error or "跨镜人物连续性分析未返回结果")[:500]
        job.log(
            "真实人物跨镜身份自动分析连续失败，已自动降级为逐镜人工人物绑定；"
            "现有台词、表演、打码和分镜数据全部保留，任务可继续。"
        )
        return {
            "version": 1,
            "characters": [],
            "assignments": [],
            "manual_review_required": True,
            "analysis_error": message,
        }
    raise last_error or WorkflowError("跨镜人物连续性分析未返回结果。")


REAL_LONG_INLINE_ANALYSIS_MAX_BYTES = 32 * 1024 * 1024


def long_performance_video_reference(
    job: WebJob,
    source: Path,
    store: TempFileMediaStore,
) -> tuple[str, str]:
    """Return an Ark video reference while keeping the two projects isolated."""
    if not is_real_person_long_job(job):
        uploaded = store.upload_video(source, expires_hours=1)
        return uploaded.signed_url, uploaded.object_key

    size = source.stat().st_size
    if size > REAL_LONG_INLINE_ANALYSIS_MAX_BYTES:
        raise WorkflowError(
            "真实人物项目的表演分析视频超过 32MB，无法以内嵌数据安全提交。"
            "请先压缩原片后重试；虚拟人物项目不会受到此限制变更影响。"
        )
    mime_type = {
        ".mov": "video/quicktime",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
    }.get(source.suffix.lower(), "video/mp4")
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}", ""


def run_long_performance_analysis(job: WebJob) -> None:
    """Extract exact dialogue timing and holistic performance meaning per shot."""
    try:
        analyzer = performance_analyzer()
        store = TempFileMediaStore()
        summaries: list[dict[str, Any]] = []
        total = len(job.shots)
        for ordinal, shot in enumerate(list(job.shots), start=1):
            index = int(shot.get("index") or ordinal)
            source = Path(str(shot.get("source_path") or ""))
            if not source.is_file():
                raise WorkflowError(f"找不到分镜 {index:02d} 原片。")
            output = source.parent / "performance.json"
            if output.is_file():
                try:
                    existing = json.loads(output.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    existing = None
                if isinstance(existing, dict):
                    summaries.append({"index": index, **existing})
                    update_long_shot(
                        job,
                        index,
                        performance=existing,
                        performance_path=str(output),
                        stage="已复用台词与表演分析",
                        progress=100,
                    )
                    continue
            job.update(
                status="running",
                stage=f"正在分析分镜 {index:02d}/{total} 的台词与表演",
                progress=int(3 + 94 * (ordinal - 1) / max(total, 1)),
            )
            update_long_shot(job, index, status="running", stage="正在提取台词、说话人和表演神色", progress=25)
            uploaded_id = ""
            try:
                video_reference, uploaded_id = long_performance_video_reference(job, source, store)
                analysis = analyzer.analyze(
                    video_reference,
                    duration=float(shot.get("duration") or inspect_video(source).duration),
                    max_people=max(1, min(4, int(shot.get("suggested_actor_count") or 1))),
                )
            finally:
                if uploaded_id:
                    try:
                        store.delete(uploaded_id)
                    except Exception as exc:
                        job.log(f"分镜 {index:02d} 临时分析视频将在 1 小时后自动过期：{exc}")
            save_shot_manifest(output, analysis)
            summaries.append({"index": index, **analysis})
            update_long_shot(
                job,
                index,
                performance=analysis,
                performance_path=str(output),
                status="ready",
                stage="台词与表演分析完成",
                progress=100,
                error="",
            )
            job.log(
                f"分镜 {index:02d} 已提取 {len(analysis.get('dialogue') or [])} 句台词、"
                f"{len(analysis.get('performance') or [])} 段表演信息。"
            )
            _persist_long_job(job)

        job.update(stage="正在建立跨镜人物稳定身份", progress=97)
        slot_counts = {
            int(shot.get("index") or 0): max(
                0,
                min(
                    4,
                    max(
                        (int(item.get("actor_slot") or 0) for item in (shot.get("performance") or {}).get("performance") or [] if isinstance(item, dict)),
                        default=long_shot_stable_detected_people_count(shot),
                    ),
                ),
            )
            for shot in job.shots
        }
        reference = _run_artifact(job.run_dir, "reference", {".mp4", ".mov", ".mkv", ".webm"})
        if reference is None:
            raise WorkflowError("找不到长视频原片，无法建立跨镜人物身份。")
        uploaded_id = ""
        try:
            video_reference, uploaded_id = long_performance_video_reference(job, reference, store)
            if is_real_person_long_job(job):
                job.log(
                    "真实人物项目的跨镜分析已改用方舟内嵌视频输入，"
                    "不再依赖可能失效的免费临时公网地址；虚拟人物项目流程保持不变。"
                )
            continuity = analyze_long_cast_continuity(
                job,
                analyzer,
                video_reference,
                shot_slot_counts=slot_counts,
                shot_ranges=[
                    {
                        "index": int(shot.get("index") or 0),
                        "start": float(shot.get("start") or 0),
                        "end": float(shot.get("end") or 0),
                    }
                    for shot in job.shots
                ],
                max_characters=4,
            )
        finally:
            if uploaded_id:
                try:
                    store.delete(uploaded_id)
                except Exception as exc:
                    job.log(f"跨镜身份临时视频将在 1 小时后自动过期：{exc}")
        job.cast_continuity = continuity
        for shot in job.shots:
            apply_cast_continuity_to_shot(job, shot)
            performance_path_value = str(shot.get("performance_path") or "")
            performance_path = Path(performance_path_value) if performance_path_value else None
            if performance_path and performance_path.is_file() and isinstance(shot.get("performance"), dict):
                save_shot_manifest(performance_path, shot["performance"])
        if continuity.get("manual_review_required") is True:
            job.log(
                "跨镜身份已进入人工逐镜绑定模式：请在每个分镜的 P 槽位下拉框中选择对应新人物；"
                "每次选择只影响当前分镜，不会联动修改其他分镜。"
            )
        else:
            job.log(
                f"跨镜人物连续性分析完成：建立 {len(continuity.get('characters') or [])} 个稳定原片身份，"
                f"锁定 {len(continuity.get('assignments') or [])} 个分镜槽位。"
            )
        job.performance_path = save_shot_manifest(
            job.run_dir / "performance_summary.json",
            {"version": 3, "shots": summaries, "cast_continuity": continuity},
        )
        job.update(
            status="succeeded",
            stage=(
                "逐镜分析完成，跨镜身份需人工绑定"
                if continuity.get("manual_review_required") is True
                else "全部台词与表演分析完成"
            ),
            progress=100,
            error="",
        )
        _persist_long_job(job)
    except Exception as exc:
        job_error(job, exc)
        _persist_long_job(job)


def save_long_performance_summary(job: WebJob) -> Path | None:
    summaries = [
        {"index": int(shot.get("index") or 0), **dict(shot["performance"])}
        for shot in job.shots
        if isinstance(shot.get("performance"), dict) and shot.get("performance")
    ]
    if len(summaries) != len(job.shots):
        return None
    return save_shot_manifest(
        job.run_dir / "performance_summary.json",
        {"version": 2, "shots": summaries},
    )


def _white_model_signature(
    mosaic_path: Path,
    *,
    prompt: str,
    options: dict[str, Any],
    performance_prompt: str = "",
    quality_gate: bool = False,
) -> str:
    payload = {
        "mosaic": _reference_fingerprint(str(mosaic_path)),
        "prompt": prompt,
        "performance_prompt": performance_prompt,
        "model": options.get("model"),
        "resolution": options.get("resolution"),
        "ratio": options.get("ratio"),
        "watermark": bool(options.get("watermark")),
        "version": 5 if quality_gate else 3,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def run_long_white_model_generation(
    job: WebJob,
    *,
    options: dict[str, Any],
    force_shot_indices: set[int] | None = None,
    reuse_unforced_outputs: bool = False,
    confirmed_paid_retry_indices: set[int] | None = None,
) -> None:
    """Generate and locally gate a green-screen white-model anchor for every shot."""
    try:
        forced_indices = set(force_shot_indices or set())
        confirmed_retry_indices = set(confirmed_paid_retry_indices or set())
        prompt = DEFAULT_WHITE_MODEL_PROMPT
        outputs: list[Path] = []
        pending_quality_indices: list[int] = []
        real_person_mode = is_real_person_long_job(job)
        total = len(job.shots)
        for ordinal, shot in enumerate(list(job.shots), start=1):
            index = int(shot.get("index") or ordinal)
            source = Path(str(shot.get("source_path") or ""))
            mosaic = Path(str(shot.get("mosaic_path") or ""))
            if not source.is_file() or not mosaic.is_file():
                raise WorkflowError(f"分镜 {index:02d} 尚未生成有效打码视频。")
            if shot.get("mosaic_reviewed") is not True:
                raise WorkflowError(f"分镜 {index:02d} 的人脸打码尚未人工确认。")
            shot_dir = source.parent
            effective_options = dict(options)
            motion_reference = mosaic
            if real_person_mode:
                requested_ratio = str(options.get("ratio") or "adaptive")
                effective_ratio = resolved_seedance_ratio(requested_ratio, source)
                if not effective_ratio:
                    raise WorkflowError(f"分镜 {index:02d} 无法识别原片画面比例，请手动选择白模画面尺寸。")
                effective_options["ratio"] = effective_ratio
                if requested_ratio == "adaptive":
                    job.log(f"分镜 {index:02d} 白模画面尺寸自动跟随原片：{effective_ratio}。")
                else:
                    job.log(f"分镜 {index:02d} 使用人工选择的白模画面尺寸：{effective_ratio}。")
                motion_reference, input_text_cleanup = sanitize_video_visible_text_if_needed(
                    mosaic,
                    shot_dir / "face_mosaic_no_visible_text.mp4",
                    preserve_audio=False,
                    overlay_only=True,
                )
                if input_text_cleanup.get("changed"):
                    job.log(
                        f"分镜 {index:02d} 已在本地清除打码参考视频中的字幕、Logo和可读文字后再提交白模；"
                        "原打码视频保持不变。"
                    )
            performance = shot.get("performance")
            performance_prompt = (
                build_white_model_performance_prompt(performance)
                if isinstance(performance, dict)
                else ""
            )
            signature = _white_model_signature(
                motion_reference,
                prompt=prompt,
                options=effective_options,
                performance_prompt=performance_prompt,
                quality_gate=is_real_person_long_job(job),
            )
            existing = Path(str(shot.get("white_model_path") or "")) if shot.get("white_model_path") else None
            force_regenerate = index in forced_indices
            can_reuse = bool(
                not force_regenerate
                and existing
                and existing.is_file()
                and (
                    reuse_unforced_outputs
                    or str(shot.get("white_model_signature") or "") == signature
                )
            )
            if can_reuse and existing is not None:
                if real_person_mode and not real_long_white_model_is_approved(shot):
                    expected_count = max(
                        long_shot_stable_detected_people_count(shot),
                        int(shot.get("suggested_actor_count") or 0),
                    )
                    existing_qa = validate_white_model(
                        source,
                        existing,
                        expected_actor_count=expected_count,
                    )
                    update_long_shot(
                        job,
                        index,
                        white_model_qa=existing_qa,
                        white_model_approval_required=not existing_qa["passed"],
                    )
                if not real_person_mode or real_long_white_model_is_approved(shot):
                    outputs.append(existing)
                    if reuse_unforced_outputs:
                        job.log(f"分镜 {index:02d} 不在本次单镜白模重生范围内，已通过质量闸门并直接复用。")
                    else:
                        job.log(f"分镜 {index:02d} 白模设置未变化且质量验收通过，直接复用。")
                    continue
                if reuse_unforced_outputs:
                    pending_quality_indices.append(index)
                    job.log(
                        f"分镜 {index:02d} 的已有白模未通过质量闸门；本次单镜任务不会擅自重生其他分镜。"
                    )
                    continue
            if force_regenerate:
                job.log(f"分镜 {index:02d} 已选择强制重新生成白模；将创建新的 Seedance 任务。")
            shot_duration = float(shot.get("duration") or inspect_video(motion_reference).duration)
            generation_duration = match_seedance_cover_duration(shot_duration)
            reference = motion_reference
            if inspect_video(motion_reference).duration < generation_duration - 0.02:
                reference = extend_video_with_trailing_hold(
                    motion_reference,
                    shot_dir / "face_mosaic_seedance_timed.mp4",
                    target_duration=float(generation_duration),
                    with_audio=False,
                )
                job.log(
                    f"分镜 {index:02d} 保持原动作速度，并将结束姿势定格延长到 {generation_duration} 秒；"
                    "不会用黑帧填充或把动作摊满整段。"
                )
            white_task_dir = shot_dir / "white_model_task"
            white_task_dir.mkdir(parents=True, exist_ok=True)
            job.update(
                status="running",
                stage=f"正在生成白模分镜 {index:02d}/{total}",
                progress=int(3 + 92 * (ordinal - 1) / max(total, 1)),
            )
            expected_count = max(
                long_shot_stable_detected_people_count(shot),
                int(shot.get("suggested_actor_count") or 0),
            )
            previous_generation_count = long_white_model_generation_count(shot)
            renewed_paid_retry = real_person_mode and index in confirmed_retry_indices
            attempts = 1 if renewed_paid_retry else 1 + (MAX_REAL_AUTOMATIC_WHITE_MODEL_RETRIES if real_person_mode else 0)
            white_output: Path | None = None
            white_qa: dict[str, Any] | None = None
            final_sub_job: WebJob | None = None
            used_retry_count = 0
            for attempt in range(attempts):
                used_retry_count = attempt
                total_generation_count = previous_generation_count + attempt + 1
                sub_id = f"{job.id}-w{index:02d}q{total_generation_count}"
                attempt_dir = white_task_dir / f"attempt_{total_generation_count}"
                attempt_dir.mkdir(parents=True, exist_ok=True)
                sub_job = WebJob(
                    id=sub_id,
                    kind="real_long_white_model_shot" if real_person_mode else "long_white_model_shot",
                    project=job.project,
                    run_dir=attempt_dir,
                    depth_path=reference,
                )
                final_sub_job = sub_job
                with JOBS_LOCK:
                    JOBS[sub_id] = sub_job
                update_long_shot(
                    job,
                    index,
                    job_id=sub_id,
                    status="running",
                    stage=("正在执行白模质量纠偏" if attempt else "Seedance 正在生成白模绿幕"),
                    progress=5,
                    error="",
                    white_model_manually_approved=False,
                    white_model_approval_required=False,
                    white_model_total_generation_count=total_generation_count,
                )
                _persist_long_job(job)
                shot_options = dict(effective_options)
                shot_options["duration"] = generation_duration
                correction = build_white_model_correction_prompt(white_qa or {}) if attempt else ""
                shot_options["prompt"] = "\n".join(
                    value
                    for value in (
                        correction,
                        prompt,
                        performance_prompt,
                        seedance_hold_timing_prompt(shot_duration, generation_duration),
                    )
                    if value
                )
                shot_options["generate_audio"] = False
                run_generation(
                    sub_job,
                    depth_path=reference,
                    depth_reference="",
                    person_source="",
                    clothing_source="",
                    scene_source="",
                    options=shot_options,
                    progress_start=5,
                    video_only=True,
                )
                if sub_job.status != "succeeded" or not sub_job.output_path or not sub_job.output_path.is_file():
                    raise WorkflowError(sub_job.error or f"分镜 {index:02d} 白模生成未完成。")
                attempt_suffix = (
                    f"_manual_retry_{total_generation_count}"
                    if renewed_paid_retry
                    else "" if attempt == 0
                    else f"_quality_retry_{attempt}"
                )
                white_output = conform_video_duration(
                    sub_job.output_path,
                    shot_dir / f"white_model_{signature}{attempt_suffix}.mp4",
                    float(shot["duration"]),
                    with_audio=False,
                )
                if not real_person_mode:
                    white_qa = {"passed": True, "reasons": [], "warnings": []}
                    break
                white_output, white_text_cleanup = sanitize_video_visible_text_if_needed(
                    white_output,
                    shot_dir / f"white_model_{signature}{attempt_suffix}_no_visible_text.mp4",
                    preserve_audio=False,
                )
                if white_text_cleanup.get("changed"):
                    job.log(
                        f"分镜 {index:02d} 白模生成结果检测到可读文字，已在本地清除后再进入质量闸门；"
                        "没有新增Seedance费用。"
                    )
                white_qa = validate_white_model(
                    source,
                    white_output,
                    expected_actor_count=expected_count,
                )
                if white_qa["passed"]:
                    break
                reasons = "；".join(str(value) for value in white_qa.get("reasons") or [])
                if attempt + 1 < attempts:
                    job.log(
                        f"分镜 {index:02d} 白模质量闸门未通过：{reasons}。"
                        "将在已确认的单次自动纠偏额度内重新生成一次。"
                    )
            if white_output is None or white_qa is None or final_sub_job is None:
                raise WorkflowError(f"分镜 {index:02d} 未得到可验收的白模。")
            white_passed = bool(white_qa.get("passed"))
            if white_passed:
                outputs.append(white_output)
                final_sub_job.update(status="succeeded", stage="白模质量验收通过", progress=100)
            else:
                pending_quality_indices.append(index)
                final_sub_job.update(status="awaiting_approval", stage="白模质量未通过，等待人工处理", progress=100)
            update_long_shot(
                job,
                index,
                white_model_path=str(white_output),
                white_model_signature=signature,
                white_model_task_id=final_sub_job.task_id,
                white_model_resolution=str(options.get("resolution") or "480p"),
                white_model_model=str(options.get("model") or DEFAULT_SEEDANCE_MODEL),
                white_model_qa=white_qa,
                white_model_retry_count=used_retry_count,
                white_model_total_generation_count=previous_generation_count + used_retry_count + 1,
                white_model_approval_required=not white_passed,
                white_model_manually_approved=False,
                status="ready" if white_passed else "awaiting_approval",
                stage="白模质量验收通过" if white_passed else "白模质量未通过，禁止生成成片",
                progress=100,
                error="",
            )
            if white_passed:
                warnings = "；".join(str(value) for value in white_qa.get("warnings") or [])
                job.log(
                    f"分镜 {index:02d} 白模已通过质量闸门并校正到原时长；"
                    + (f"允许偏差提醒：{warnings}。" if warnings else "未发现致命构图、绿幕或文字问题。")
                )
            else:
                reasons = "；".join(str(value) for value in white_qa.get("reasons") or [])
                job.log(
                    f"分镜 {index:02d} 白模自动纠偏后仍未通过：{reasons}。"
                    "该白模只保留供预览，不会进入最终成片。"
                )
            _persist_long_job(job)

        if real_person_mode:
            merged = rebuild_real_long_white_model_merge(job)
            if pending_quality_indices or merged is None:
                labels = "、".join(f"{value:02d}" for value in sorted(set(pending_quality_indices)))
                job.update(
                    status="awaiting_approval",
                    stage=f"白模分镜 {labels} 未通过质量闸门",
                    progress=100,
                    error="",
                )
                job.log("未通过质量闸门的白模不会提交最终Seedance成片；请逐镜重新生成、人工确认或跳过。")
                _persist_long_job(job)
                return
        else:
            job.update(stage="正在合并完整白模视频", progress=96)
            job.white_model_path = concatenate_videos(outputs, job.run_dir / "白模绿幕视频.mp4")
        job.update(status="succeeded", stage="全部白模分镜已完成并通过质量闸门", progress=100, error="")
        _persist_long_job(job)
    except Exception as exc:
        job_error(job, exc)
        _persist_long_job(job)


def _resume_matching_long_shot_task(
    job: WebJob,
    *,
    options: dict[str, Any],
    requested_signature: str,
    actor_count: int,
    scene_path: Path,
    first_person: str,
    first_clothing: str,
) -> bool:
    """Recover an already submitted matching shot instead of creating another paid task."""
    record_path = job.run_dir / "job.json"
    if not record_path.is_file():
        return False
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(record, dict):
        return False
    task_id = str(record.get("task_id") or "").strip()
    recorded_signature = str(record.get("requested_signature") or "").strip()
    if not task_id or not recorded_signature or recorded_signature != requested_signature:
        return False
    if str(record.get("prompt") or "") != str(options.get("prompt") or ""):
        return False
    expected_values = {
        "model": str(options.get("model") or ""),
        "resolution": str(options.get("resolution") or ""),
        "ratio": str(options.get("ratio") or ""),
    }
    if any(str(record.get(key) or "") != value for key, value in expected_values.items()):
        return False
    if int(record.get("actor_count") or 0) != int(actor_count):
        return False

    def same_local_path(recorded: Any, expected: str | Path) -> bool:
        if not str(expected):
            return not str(recorded or "")
        try:
            return Path(str(recorded)).resolve() == Path(str(expected)).resolve()
        except (OSError, ValueError):
            return False

    if not same_local_path(record.get("scene"), scene_path):
        return False
    if first_person and not first_person.startswith("asset://") and not same_local_path(record.get("person"), first_person):
        return False
    if first_clothing and not same_local_path(record.get("clothing"), first_clothing):
        return False

    job.update(
        task_id=task_id,
        status="running",
        stage="正在找回已成功的 Seedance 分镜成片",
        progress=65,
        cloud_status=str(record.get("cloud_status") or "queued"),
        cloud_started_at=float(record.get("submitted_at") or time.time()),
        generation_duration=int(record.get("duration") or options.get("duration") or 0),
        error="",
    )
    job.log(f"检测到同一配置已提交的 Seedance 任务：{task_id}；只恢复查询和下载，不重新计费。")
    resume_cloud_job(job)
    if job.status != "succeeded" or not job.output_path or not job.output_path.is_file():
        raise WorkflowError(
            job.error
            or f"已存在的 Seedance 任务 {task_id} 暂未恢复成功；系统不会重新提交付费任务。"
        )
    return True


def run_real_whole_video_generation(
    job: WebJob,
    *,
    actors: list[dict[str, Any]],
    scene_reference: Path,
    options: dict[str, Any],
) -> None:
    """Generate the complete real-person film in one Seedance 2.5 edit task."""
    try:
        job.update(
            kind="long_generate",
            generation_strategy=REAL_WHOLE_GENERATION_STRATEGY,
            status="running",
            stage="正在准备整段白模连续生成",
            progress=5,
            error="",
            pause_requested=False,
        )
        merged_white_model = rebuild_real_long_white_model_merge(job)
        if merged_white_model is None or not merged_white_model.is_file():
            raise WorkflowError("整段生成模式缺少已合并的白模视频，请先完成并合并全部白模分镜。")
        white_info = validate_seedance_reference_video(merged_white_model)
        if white_info.duration < 4 or white_info.duration > 30:
            raise WorkflowError(
                "Seedance 2.5 整段视频编辑要求合并白模时长为 4–30 秒；"
                f"当前为 {white_info.duration:.2f} 秒。"
            )
        silent_reference = strip_video_audio(
            merged_white_model,
            job.run_dir / "whole_white_model_seedance_silent.mp4",
        )
        whole_options = normalize_real_final_video_options(
            dict(options),
            video_editing=True,
        )
        whole_options["model"] = DEFAULT_SEEDANCE_25_MODEL
        whole_options["ratio"] = "adaptive"
        whole_options["duration"] = -1
        whole_options["prompt"] = build_real_whole_video_prompt(
            actors,
            [shot for shot in job.shots if not real_long_shot_is_skipped(shot)],
            str(options.get("prompt") or ""),
        )
        job.depth_path = silent_reference
        job.white_reference_path = merged_white_model
        job.scene_path = scene_reference
        job.log(
            "已切换为整段白模一次生成：不会创建或使用逐镜 Seedream 场景板；"
            "@视频1为完整白模时空母版，用户选择的原始场景图只作同一空间参考。"
        )
        job.log(f"整段场景约束：{REAL_WHOLE_SCENE_REFERENCE_CONSTRAINT}")
        _persist_long_job(job)
        run_multi_generation(
            job,
            depth_path=silent_reference,
            depth_reference="",
            character_sources=real_person_character_sources(actors),
            scene_source=str(scene_reference),
            options=whole_options,
            progress_start=8,
            include_scene_reference=True,
        )
        if job.status != "succeeded" or not job.output_path or not job.output_path.is_file():
            raise WorkflowError(job.error or "Seedance 2.5 整段生成任务未完成。")
        generated_output = Path(job.output_path)
        job.update(stage="正在本地清除整段成片中的字幕与可读文字", progress=96)
        cleaned_output, text_cleanup = sanitize_video_visible_text_if_needed(
            generated_output,
            job.run_dir / "整段复刻成片_无字幕.mp4",
            preserve_audio=True,
            overlay_only=True,
        )
        job.output_path = cleaned_output
        if text_cleanup.get("changed"):
            job.log(
                "整段成片检测到字幕、Logo或可读文字，已在本地逐帧清除并保留Seedance直生对白与新音色；"
                "原始云端结果保留供审计，没有再次提交付费任务。"
            )
        final_text_profile = _sample_text_profile(cleaned_output, overlay_only=True)
        job.update(text_cleanup_profile=final_text_profile)
        if bool(final_text_profile.get("detected")):
            job.update(unclean_output_path=generated_output, output_path=None)
            raise WorkflowError(
                "整段成片本地去字后仍检测到可读字幕或Logo，已禁止交付该结果；"
                "系统没有自动重新提交付费任务。"
            )
        if bool(final_text_profile.get("suspected")):
            job.log("整段成片复检发现少量疑似字符纹理，未识别为可读文字，建议下载前人工快速预览。")
        job.update(
            kind="long_generate",
            generation_strategy=REAL_WHOLE_GENERATION_STRATEGY,
            status="succeeded",
            stage="整段白模一次生成完成",
            progress=100,
            error="",
        )
        job.log("整段成片已完成：所有分镜由同一个 Seedance 2.5 任务生成，场景空间连续性由全片共同约束。")
        _persist_long_job(job)
    except Exception as exc:
        job_error(job, exc)
        _persist_long_job(job)


def run_long_video_generation(
    job: WebJob,
    *,
    actors: list[dict[str, Any]],
    scene_prompt: str,
    image_model: str,
    options: dict[str, Any],
    blur_range: str,
    force_shot_indices: set[int] | None = None,
    composition_approved_shot_indices: set[int] | None = None,
    reuse_unforced_outputs: bool = False,
    selected_only_generation: bool = False,
    deferred_unforced_shot_indices: set[int] | None = None,
) -> None:
    try:
        forced_indices = set(force_shot_indices or set())
        approved_indices = set(composition_approved_shot_indices or set())
        deferred_indices = set(deferred_unforced_shot_indices or set())
        conformed_outputs: list[Path] = []
        pending_composition_approvals: list[int] = [
            int(shot.get("index") or 0)
            for shot in job.shots
            if bool(shot.get("composition_approval_required"))
            and int(shot.get("index") or 0) not in forced_indices
            and not (
                is_real_person_long_job(job)
                and real_long_shot_is_skipped(shot)
            )
            and (
                not selected_only_generation
                or int(shot.get("index") or 0) in forced_indices
            )
            and int(shot.get("index") or 0) not in approved_indices
        ]
        total = len(job.shots)
        new_voice_mode = bool(
            options.get("generate_audio")
            and options.get("dialogue_voice_mode") == "seedance_new_voice"
        )
        actor_by_id = {int(actor.get("id") or 0): actor for actor in actors}
        real_person_mode = is_real_person_long_job(job)
        if real_person_mode:
            job.update(status="running", stage="正在启动真实人物逐分镜生成", error="")
            if pause_real_long_generation_at_boundary(job):
                return
        if new_voice_mode:
            job.log(
                "最终成片将由 Seedance 生成新的角色音色；提交给 Seedance 的视频参考会强制移除全部音轨。"
                "台词内容、说话人、发声起止、语速、停顿、重音和情绪只来自提取及人工校订文字；"
                "原片对白、BGM、歌声与环境音都不会进入参考或成片。"
            )
        for ordinal, shot in enumerate(list(job.shots), start=1):
            index = int(shot.get("index") or ordinal)
            if pause_real_long_generation_at_boundary(job):
                return
            if real_person_mode and real_long_shot_is_skipped(shot):
                update_long_shot(
                    job,
                    index,
                    composition_approval_required=False,
                    status="skipped",
                    stage="已跳过，不参与最终合并",
                    progress=100,
                    error="",
                )
                job.log(f"分镜 {index:02d} 已按人工选择跳过；未提交 Seedream 或 Seedance 任务。")
                continue
            if selected_only_generation and index not in forced_indices and index in deferred_indices:
                job.log(
                    f"分镜 {index:02d} 当前处于暂停、失败、待纠偏、待更新或旧配置状态；"
                    "本次只生成所选镜头，不会复用该中间结果，也不会为本镜提交付费任务。"
                )
                continue
            shot_dir = Path(str(shot["source_path"])).parent
            shot_duration = float(shot["duration"])
            actor_ids = [int(value) for value in shot.get("actor_ids") or []]
            selected_actors = [actor_by_id[value] for value in actor_ids if value in actor_by_id]
            requested_signature = str(shot.get("requested_signature") or "")
            existing_output = Path(str(shot.get("output_path") or "")) if shot.get("output_path") else None
            force_regenerate = index in forced_indices
            if (
                not force_regenerate
                and (reuse_unforced_outputs or not bool(shot.get("composition_approval_required")))
                and existing_output
                and existing_output.is_file()
                and (
                    reuse_unforced_outputs
                    or (
                        requested_signature
                        and str(shot.get("output_signature") or "") == requested_signature
                    )
                )
            ):
                conformed_outputs.append(existing_output)
                if reuse_unforced_outputs:
                    job.log(f"分镜 {index:02d} 不在本次单镜重生范围内，直接复用已有成片。")
                else:
                    job.log(f"分镜 {index:02d} 的人物配置未变化，直接复用已有成片。")
                continue
            if force_regenerate:
                job.log(f"分镜 {index:02d} 已选择强制重新生成；将创建新的 Seedance 任务。")
            if len(selected_actors) != len(actor_ids):
                raise WorkflowError(f"分镜 {index:02d} 的角色素材不完整，请重新确认角色库。")
            sub_id = f"{job.id}-s{index:02d}"
            first_person = (
                str(
                    (
                        selected_actors[0].get("masked_person_source")
                        if real_person_mode
                        else selected_actors[0].get("person_source")
                    )
                    or ""
                )
                if selected_actors
                else ""
            )
            first_clothing = str(selected_actors[0].get("clothing_source") or "") if selected_actors else ""
            sub_job = WebJob(
                id=sub_id,
                kind="real_long_shot" if real_person_mode else "long_shot",
                project=job.project,
                run_dir=shot_dir,
                person_path=Path(first_person) if first_person and not first_person.startswith("asset://") else None,
                clothing_path=Path(first_clothing) if first_clothing else None,
            )
            with JOBS_LOCK:
                JOBS[sub_id] = sub_job
            update_long_shot(job, index, job_id=sub_id, status="running", stage="正在生成深度视频", progress=0, error="")
            job.update(stage=f"正在处理分镜 {index:02d}/{total}", progress=int(5 + 88 * (ordinal - 1) / max(total, 1)))
            cast_log = (
                f"已绑定 {len(selected_actors)} 位人物（{', '.join(str(value) for value in actor_ids)}）。"
                if selected_actors
                else "已确认为无人镜头，将仅重绘新场景。"
            )
            job.log(
                f"开始处理分镜 {index:02d}：{float(shot['start']):.2f}s–{float(shot['end']):.2f}s，{cast_log}"
            )
            try:
                if pause_real_long_generation_at_boundary(job, shot_index=index):
                    return
                generation_duration = match_seedance_cover_duration(shot_duration)
                depth_path = shot_dir / "depth.mp4"
                if depth_path.is_file():
                    sub_job.depth_path = depth_path
                else:
                    run_depth(sub_job, Path(str(shot["source_path"])), depth_path, blur_range, start=0, span=42)
                seedance_depth_path = depth_path
                depth_padded = inspect_video(depth_path).duration < generation_duration - 0.02
                if depth_padded:
                    seedance_depth_path = extend_video_with_trailing_hold(
                        depth_path,
                        shot_dir / "depth_seedance_timed.mp4",
                        target_duration=float(generation_duration),
                        with_audio=False,
                    )
                    sub_job.depth_path = seedance_depth_path
                    job.log(
                        f"分镜 {index:02d} 原时长 {shot_duration:.2f} 秒；保持原动作速度，"
                        f"并将结束姿势定格延长到 {generation_duration} 秒供 Seedance 参考。"
                    )
                final_reference_path = seedance_depth_path
                white_model_value = str(shot.get("white_model_path") or "")
                white_model_path = Path(white_model_value) if white_model_value else None
                if white_model_path is not None and white_model_path.is_file():
                    final_reference_path = white_model_path
                    if inspect_video(white_model_path).duration < generation_duration - 0.02:
                        final_reference_path = extend_video_with_trailing_hold(
                            white_model_path,
                            shot_dir / "white_model_seedance_timed.mp4",
                            target_duration=float(generation_duration),
                            with_audio=False,
                        )
                    job.log(f"分镜 {index:02d} 最终重绘将使用已确认的白模视频作为动作与运镜母版。")
                else:
                    job.log(f"分镜 {index:02d} 尚无白模视频，最终重绘回退使用深度参考。")
                if new_voice_mode:
                    final_reference_path = strip_video_audio(
                        final_reference_path,
                        shot_dir / "final_reference_seedance_silent.mp4",
                    )
                    update_long_shot(job, index, dialogue_timing_path="")
                    job.log(
                        f"分镜 {index:02d} 提交给 Seedance 的参考已强制静音；"
                        "不会携带原片对白、BGM、歌声、环境音或任何原声波形。"
                    )
                # The per-shot task record must describe the video actually submitted.
                # Keeping the old depth path here made successful white-model runs look
                # as if they had ignored the white model during later diagnosis.
                sub_job.depth_path = final_reference_path
                target_scene_value = str(shot.get("target_scene_path") or "")
                target_scene = Path(target_scene_value) if target_scene_value else None
                if target_scene is None or not target_scene.is_file():
                    raise WorkflowError(f"分镜 {index:02d} 尚未匹配有效的新场景图片。")
                update_long_shot(
                    job,
                    index,
                    depth_path=str(seedance_depth_path),
                    depth_padded=depth_padded,
                    depth_reference_duration=(
                        float(generation_duration)
                        if depth_padded
                        else round(inspect_video(seedance_depth_path).duration, 3)
                    ),
                    stage="正在匹配新场景机位",
                    progress=42,
                )
                scene_path = shot_dir / "scene_plate.jpg"
                requested_scene_signature = str(shot.get("requested_scene_signature") or "")
                if (
                    scene_path.is_file()
                    and requested_scene_signature
                    and str(shot.get("scene_plate_signature") or "") == requested_scene_signature
                ):
                    sub_job.scene_path = scene_path
                    job.log(f"分镜 {index:02d} 的目标场景与构图设置未变化，复用已有新场景板。")
                else:
                    run_long_scene_plate(
                        sub_job,
                        depth_path,
                        target_scene,
                        prompt=scene_prompt,
                        model=image_model,
                        source_video=Path(str(shot.get("source_path") or "")),
                        motion_video=(
                            white_model_path
                            if white_model_path is not None and white_model_path.is_file()
                            else depth_path
                        ),
                        shot_index=index,
                        strict_real_person_layout=real_person_mode,
                        start=42,
                        span=18,
                    )
                update_long_shot(
                    job,
                    index,
                    scene_path=str(scene_path),
                    scene_plate_signature=requested_scene_signature,
                    stage="Seedance 正在生成当前分镜",
                    progress=60,
                )
                if real_person_mode:
                    update_long_shot(job, index, control_reference_path="")
                    job.log(
                        f"分镜 {index:02d} 已取消场景与白模逐帧叠合："
                        "@视频1直接使用白模运动母版，分镜专属场景图作为独立外观参考，"
                        "由 Seedance 统一生成随镜头变化的真人与背景。"
                    )
                shot_options = dict(options)
                shot_options["duration"] = generation_duration
                if real_person_mode and str(shot_options.get("model") or "").strip() == DEFAULT_SEEDANCE_25_MODEL:
                    shot_options = normalize_real_final_video_options(
                        shot_options,
                        video_editing=True,
                    )
                    job.log(
                        f"分镜 {index:02d} 使用 Seedance 2.5 视频编辑参数："
                        "ratio=adaptive、duration=-1；输出比例和时长由白模视频决定。"
                    )
                localized_actors: list[dict[str, Any]] = []
                for slot_index, actor in enumerate(selected_actors):
                    localized = dict(actor)
                    position = long_shot_performance_slot_anchor(shot, slot_index + 1)
                    localized["source_slot"] = slot_index + 1
                    continuity_assignment = (
                        None
                        if shot.get("manual_identity_override") is True
                        else cast_continuity_assignment_map(job).get((index, slot_index + 1))
                    )
                    localized["source_character_id"] = (
                        int(continuity_assignment.get("character_id") or 0)
                        if continuity_assignment
                        else 0
                    )
                    localized["position_anchor"] = position
                    localized_actors.append(localized)
                shot_options["prompt"] = build_long_shot_prompt(
                    localized_actors,
                    compact_long_generation_constraints(
                        str(options.get("prompt") or ""),
                        real_person_mode=real_person_mode,
                    ),
                    replace_scene=True,
                    white_model_reference=bool(white_model_path is not None and white_model_path.is_file()),
                    real_person_mode=real_person_mode,
                )
                shot_options["prompt"] = (
                    f"{shot_options['prompt']}\n"
                    f"{seedance_hold_timing_prompt(shot_duration, generation_duration, compact=real_person_mode)}"
                )
                performance = shot.get("performance")
                if isinstance(performance, dict):
                    performance_limit = SEEDANCE_SAFE_PROMPT_LIMIT - (
                        REAL_RETRY_PROMPT_RESERVE if real_person_mode else 0
                    )
                    performance_budget = performance_limit - len(shot_options["prompt"]) - 1
                    if performance_budget < 80:
                        raise WorkflowError(
                            f"分镜 {index:02d} 的人物映射和共用约束已占满 Seedance 提示词预算；"
                            "请精简所有分镜共用提示词后重试。"
                        )
                    performance_prompt = build_performance_prompt(
                        performance,
                        max_chars=performance_budget if real_person_mode else 10000,
                        generate_new_voice=new_voice_mode,
                        speaker_roles=[str(actor.get("role") or "") for actor in selected_actors],
                        compact=True,
                        ultra_compact=real_person_mode,
                    )
                    if len(performance_prompt) > performance_budget:
                        if real_person_mode:
                            raise WorkflowError(
                                f"分镜 {index:02d} 的台词原文与说话人时间轴超过 Seedance 可用预算；"
                                "请只精简重复语气说明，不要删除台词正文。"
                            )
                        raise WorkflowError(
                            f"分镜 {index:02d} 的人工台词与表演内容在自动去重后仍超过 Seedance 提示词上限；"
                            "系统未截断台词或人物映射，请精简本镜人工表演说明后重试。"
                        )
                    shot_options["prompt"] = (
                        f"{shot_options['prompt']}\n"
                        f"{performance_prompt}"
                    )
                real_character_sources: list[tuple[str, str]] | None = None
                if real_person_mode and selected_actors:
                    real_character_sources, closeup_references = real_person_shot_character_sources(
                        localized_actors,
                        shot_dir=shot_dir,
                        motion_video=final_reference_path,
                    )
                    if closeup_references:
                        job.log(
                            f"分镜 {index:02d} 检测为近景/特写；每位人物只提交火山角色库Asset与服装图，"
                            "服装图按本镜朝向裁到上半身；不再生成或提交遮眼图、素描图。"
                        )
                    else:
                        job.log(
                            f"分镜 {index:02d} 每位人物只提交火山角色库Asset与服装图；"
                            "身份由角色库锁定，服装图按本镜朝向选取正面、侧面或背面。"
                        )
                base_prompt = str(shot_options["prompt"])
                approval_granted = index in approved_indices
                existing_retry_count = max(0, int(shot.get("composition_retry_count") or 0))
                max_auto_retries = (
                    MAX_REAL_AUTOMATIC_COMPOSITION_RETRIES
                    if real_person_mode
                    else MAX_AUTOMATIC_COMPOSITION_RETRIES
                )
                attempt_count = 1 if approval_granted else 1 + max_auto_retries
                composition_qa: dict[str, Any] | None = (
                    dict(shot.get("composition_qa") or {}) if approval_granted else None
                )
                conformed: Path | None = None
                generation_mode = ""
                final_retry_count = existing_retry_count if approval_granted else 0
                for attempt_offset in range(attempt_count):
                    if pause_real_long_generation_at_boundary(
                        job,
                        shot_index=index,
                        task_id=sub_job.task_id,
                    ):
                        return
                    correction_number = (
                        existing_retry_count + 1
                        if approval_granted
                        else attempt_offset
                    )
                    attempt_options = dict(shot_options)
                    attempt_scene_path = scene_path
                    if approval_granted or attempt_offset > 0:
                        correction = build_composition_correction_prompt(
                            composition_qa or {},
                            retry_number=correction_number,
                            absolute_white_model=real_person_mode,
                        )
                        retry_base_prompt = base_prompt.replace(
                            WHITE_MODEL_FINAL_MASTER_CONSTRAINT,
                            COMPOSITION_RETRY_MASTER_CONSTRAINT,
                            1,
                        )
                        attempt_options["prompt"] = f"{correction}\n{retry_base_prompt}"
                        if len(attempt_options["prompt"]) > SEEDANCE_SAFE_PROMPT_LIMIT:
                            raise WorkflowError(
                                f"分镜 {index:02d} 的构图纠偏提示与人工台词合并后超过 Seedance 上限；"
                                "系统没有截断台词，请精简本镜人工表演说明后重试。"
                            )
                        final_retry_count = correction_number
                        job.log(
                            f"分镜 {index:02d} 正在执行构图纠偏第 {correction_number} 次：{correction}"
                        )
                    attempt_signature = (
                        requested_signature
                        if not approval_granted and attempt_offset == 0
                        else f"{requested_signature or 'latest'}-composition-{correction_number}"
                    )
                    attempt_options["requested_signature"] = attempt_signature
                    recovered_existing = False
                    if not force_regenerate and attempt_offset == 0 and not approval_granted:
                        recovered_existing = _resume_matching_long_shot_task(
                            sub_job,
                            options=attempt_options,
                            requested_signature=requested_signature,
                            actor_count=len(selected_actors),
                            scene_path=scene_path,
                            first_person=first_person,
                            first_clothing=first_clothing,
                        )
                    if recovered_existing:
                        generation_mode = (
                            "scene_only" if not selected_actors else "single" if len(selected_actors) == 1 else "multi"
                        )
                    else:
                        sub_job.output_path = None
                        sub_job.update(
                            status="queued",
                            stage="等待提交构图纠偏任务" if (approval_granted or attempt_offset > 0) else "等待提交",
                            progress=60,
                            task_id="",
                            cloud_status="",
                            cloud_started_at=0.0,
                            cloud_updated_at=0.0,
                            error="",
                            recovery_action="",
                        )
                        if not selected_actors:
                            run_generation(
                                sub_job,
                                depth_path=final_reference_path,
                                depth_reference="",
                                person_source="",
                                clothing_source="",
                                scene_source=str(attempt_scene_path),
                                options=attempt_options,
                                progress_start=60,
                            )
                            generation_mode = "scene_only"
                        elif len(selected_actors) == 1 and not real_person_mode:
                            actor = selected_actors[0]
                            run_generation(
                                sub_job,
                                depth_path=final_reference_path,
                                depth_reference="",
                                person_source=str(actor["person_source"]),
                                clothing_source=str(actor["clothing_source"]),
                                scene_source=str(attempt_scene_path),
                                options=attempt_options,
                                progress_start=60,
                            )
                            generation_mode = "single"
                        else:
                            run_multi_generation(
                                sub_job,
                                depth_path=final_reference_path,
                                depth_reference="",
                                character_sources=(
                                    real_character_sources
                                    if real_person_mode
                                    else [
                                        (str(actor["person_source"]), str(actor["clothing_source"]))
                                        for actor in selected_actors
                                    ]
                                ),
                                scene_source=str(attempt_scene_path),
                                options=attempt_options,
                                progress_start=60,
                                include_scene_reference=True,
                            )
                            generation_mode = "real_character_asset" if real_person_mode else "multi"
                    if pause_real_long_generation_at_boundary(
                        job,
                        shot_index=index,
                        task_id=sub_job.task_id,
                    ):
                        return
                    if sub_job.status != "succeeded" or not sub_job.output_path or not sub_job.output_path.is_file():
                        raise WorkflowError(sub_job.error or f"分镜 {index:02d} 的 Seedance 任务未完成。")
                    raw_output = Path(sub_job.output_path)
                    conformed = conform_video_duration(
                        raw_output,
                        shot_dir / f"final_conformed_{attempt_signature}.mp4",
                        float(shot["duration"]),
                        with_audio=bool(options.get("generate_audio")),
                    )
                    if real_person_mode:
                        conformed, final_text_cleanup = sanitize_video_visible_text_if_needed(
                            conformed,
                            shot_dir / f"final_conformed_{attempt_signature}_no_visible_text.mp4",
                            preserve_audio=bool(options.get("generate_audio")),
                            overlay_only=True,
                        )
                        if final_text_cleanup.get("changed"):
                            job.log(
                                f"分镜 {index:02d} 成片检测到字幕、Logo或可读文字，已在本地清除并保留Seedance新音色；"
                                "不会因此自动新增付费生成。"
                            )
                    composition_qa = validate_final_composition(
                        Path(str(shot.get("source_path") or "")),
                        conformed,
                        expected_actor_count=len(selected_actors),
                        real_person_mode=real_person_mode,
                        white_model_path=(
                            Path(str(shot.get("white_model_path") or ""))
                            if real_person_mode and shot.get("white_model_path")
                            else None
                        ),
                    )
                    update_long_shot(
                        job,
                        index,
                        output_path=str(conformed),
                        raw_output_path=str(raw_output),
                        task_id=sub_job.task_id,
                        composition_qa=composition_qa,
                        composition_retry_count=final_retry_count,
                    )
                    _persist_long_job(job)
                    if pause_real_long_generation_at_boundary(
                        job,
                        shot_index=index,
                        task_id=sub_job.task_id,
                    ):
                        return
                    if composition_qa["passed"]:
                        break
                    reasons = "；".join(str(value) for value in composition_qa["reasons"])
                    if not approval_granted and attempt_offset < max_auto_retries:
                        job.log(
                            f"分镜 {index:02d} 构图验收未通过：{reasons}。"
                            f"系统将在无需人工确认的额度内自动纠偏（最多 {max_auto_retries} 次）。"
                        )

                if conformed is None or composition_qa is None:
                    raise WorkflowError(f"分镜 {index:02d} 未得到可验收的成片。")
                composition_warning = not composition_qa["passed"]
                if composition_warning:
                    reasons = "；".join(str(value) for value in composition_qa["reasons"])
                    pending_composition_approvals.append(index)
                    sub_job.update(
                        status="awaiting_approval",
                        stage="构图纠偏已达自动上限，等待人工同意",
                        progress=100,
                    )
                    update_long_shot(
                        job,
                        index,
                        output_path=str(conformed),
                        raw_output_path=str(raw_output),
                        task_id=sub_job.task_id,
                        output_signature=requested_signature,
                        final_style_id=str(options.get("final_style_id") or "match_character"),
                        performance_dirty=False,
                        position_binding_dirty=False,
                        dialogue_voice_mode=str(options.get("dialogue_voice_mode") or "silent"),
                        generation_mode=generation_mode,
                        composition_qa=composition_qa,
                        composition_retry_count=final_retry_count,
                        composition_approval_required=True,
                        status="awaiting_approval",
                        stage="自动纠偏已完成，继续生成需人工同意",
                        progress=100,
                        error="",
                    )
                    job.log(
                        f"分镜 {index:02d} 已完成 {final_retry_count} 次构图纠偏但仍有明显差异：{reasons}。"
                        "系统已停止继续付费生成；需要人工点击同意后才会再提交一次。"
                    )
                else:
                    update_long_shot(
                        job,
                        index,
                        output_path=str(conformed),
                        raw_output_path=str(raw_output),
                        task_id=sub_job.task_id,
                        output_signature=requested_signature,
                        final_style_id=str(options.get("final_style_id") or "match_character"),
                        performance_dirty=False,
                        position_binding_dirty=False,
                        dialogue_voice_mode=str(options.get("dialogue_voice_mode") or "silent"),
                        generation_mode=generation_mode,
                        composition_qa=composition_qa,
                        composition_retry_count=final_retry_count,
                        composition_approval_required=False,
                        status="succeeded",
                        stage="分镜成片完成",
                        progress=100,
                        error="",
                    )
                conformed_outputs.append(conformed)
                job.log(
                    f"分镜 {index:02d} 已保持原速度并无变速裁切到原始时长 {shot_duration:.2f} 秒。"
                )
                _persist_long_job(job)
                if composition_warning and real_person_mode:
                    job.output_path = None
                    job.update(
                        status="awaiting_approval",
                        stage=f"分镜 {index:02d} 等待人工同意继续构图纠偏；后续分镜尚未提交",
                        progress=min(94, int(5 + 88 * ordinal / max(total, 1))),
                        error="",
                    )
                    job.log(
                        f"真实人物分镜 {index:02d} 仍未通过构图验收；"
                        "系统已在提交下一分镜前停止，避免继续产生付费任务。"
                    )
                    _persist_long_job(job)
                    return
            except Exception as exc:
                update_long_shot(job, index, status="failed", stage="分镜处理失败", error=str(exc))
                raise
        if pause_real_long_generation_at_boundary(job):
            return
        if pending_composition_approvals:
            labels = "、".join(f"{value:02d}" for value in pending_composition_approvals)
            job.output_path = None
            job.update(
                status="awaiting_approval",
                stage=f"分镜 {labels} 等待人工同意继续构图纠偏",
                progress=100,
                error="",
            )
            job.log(
                f"分镜 {labels} 已达到自动构图纠偏上限。整片暂不合并；"
                "请逐镜预览当前结果，并点击“同意继续纠偏生成”。"
            )
            _persist_long_job(job)
            return
        if selected_only_generation and deferred_indices:
            selected_labels = "、".join(f"{value:02d}" for value in sorted(forced_indices))
            deferred_labels = "、".join(f"{value:02d}" for value in sorted(deferred_indices))
            job.output_path = None
            job.update(
                status="succeeded",
                stage=f"分镜 {selected_labels} 已单独生成；整片等待其他分镜完成",
                progress=100,
                error="",
            )
            job.log(
                f"分镜 {selected_labels} 已完成并独立保存。分镜 {deferred_labels} 尚未达到可安全合并状态；"
                "本次没有把暂停或旧配置的中间结果混入整片，也没有自动生成这些分镜。"
            )
            _persist_long_job(job)
            return
        skipped_indices = [
            int(shot.get("index") or 0)
            for shot in job.shots
            if real_person_mode and real_long_shot_is_skipped(shot)
        ]
        if not conformed_outputs:
            labels = "、".join(f"{value:02d}" for value in skipped_indices)
            job.output_path = None
            job.update(
                status="succeeded",
                stage="所有分镜均已跳过，未生成整合成片",
                progress=100,
                error="",
            )
            job.log(f"分镜 {labels} 均已跳过；没有可参与合并的成片片段。")
            _persist_long_job(job)
            return
        job.update(stage="正在按原始顺序合并未跳过的分镜", progress=95)
        if new_voice_mode:
            final_output = concatenate_videos(
                conformed_outputs,
                job.run_dir / "长视频多分镜复刻成片.mp4",
            )
            job.log("最终音轨完全来自Seedance直生；没有混回原片对白、BGM或环境音。")
        elif options.get("preserve_original_audio"):
            visual_output = concatenate_videos(conformed_outputs, job.run_dir / "长视频多分镜复刻_画面.mp4")
            original = _run_artifact(job.run_dir, "reference", {".mp4", ".mov"})
            if original is None:
                raise WorkflowError("找不到原片，无法恢复原始音轨。")
            job.update(stage="正在恢复并对齐原片音轨", progress=98)
            final_output = mux_original_audio(
                visual_output,
                original,
                job.run_dir / "长视频多分镜复刻成片.mp4",
            )
        else:
            final_output = concatenate_videos(conformed_outputs, job.run_dir / "长视频多分镜复刻成片.mp4")
        job.output_path = final_output
        final_stage = "全部未跳过分镜已合并完成" if skipped_indices else "全部分镜已合并完成"
        job.update(status="succeeded", stage=final_stage, progress=100)
        if skipped_indices:
            labels = "、".join(f"{value:02d}" for value in skipped_indices)
            job.log(f"最终合并已排除人工跳过的分镜：{labels}。")
        job.log(f"长视频最终成片已保存：{final_output.name}")
        _persist_long_job(job)
    except Exception as exc:
        job_error(job, exc)
        _persist_long_job(job)


@app.post("/api/real-long-video/analyze")
@app.post("/api/long-video/analyze")
def analyze_long_video_job():
    try:
        project = requested_long_project()
        job = new_job("real_long_analyze" if project == REAL_PERSON_LONG_PROJECT else "long_analyze")
        source = save_upload(request.files.get("reference_video"), job.run_dir, "reference")
        bind_long_workspace(job, str(request.form.get("workspace_id") or "").strip())
        try:
            sensitivity = float(request.form.get("sensitivity", "0.30"))
        except ValueError as exc:
            raise WorkflowError("分镜灵敏度必须是数字。") from exc
        manual_cuts_text = request.form.get("manual_cuts", "").strip()
        forced_cuts: list[float] = []
        if manual_cuts_text:
            try:
                forced_cuts = [
                    float(value)
                    for value in re.split(r"[,，;；\s]+", manual_cuts_text)
                    if value.strip()
                ]
            except ValueError as exc:
                raise WorkflowError("手动切点必须是秒数，并用逗号分隔，例如：3.73, 5.87, 7.73。") from exc

        def worker() -> None:
            try:
                job.update(status="running", stage="正在本地检测镜头切换", progress=5)
                info = inspect_video(source)
                invalid_cuts = [value for value in forced_cuts if not math.isfinite(value) or value <= 0 or value >= info.duration]
                if invalid_cuts:
                    raise WorkflowError(
                        f"手动切点必须大于 0 且小于原片时长 {info.duration:.3f} 秒：{invalid_cuts}"
                    )
                shots = detect_video_shots(source, sensitivity=sensitivity, forced_cuts=forced_cuts)
                job.update(stage=f"检测到 {len(shots)} 个分镜，正在精确拆分", progress=42, source_duration=info.duration)
                if forced_cuts:
                    job.log(f"已优先采用 {len(forced_cuts)} 个手动切点：{', '.join(f'{value:.3f}s' for value in forced_cuts)}")
                paths = split_video_shots(source, shots, job.run_dir / "shots")
                try:
                    scene_clusters = cluster_video_shot_scenes(paths)
                    job.log(
                        f"本地背景相似度分析建议归为 {len(set(scene_clusters))} 个原片场景组；"
                        "该结果只用于自动匹配建议，可在页面逐镜调整。"
                    )
                except Exception as exc:
                    scene_clusters = list(range(1, len(paths) + 1))
                    job.log(f"原片场景聚类不可用，已按分镜顺序建立匹配建议：{exc}")
                shot_records = []
                for ordinal, (boundary, path) in enumerate(zip(shots, paths, strict=True), start=1):
                    job.update(
                        stage=f"正在本地估算分镜人数 {ordinal}/{len(paths)}",
                        progress=50 + int(45 * ordinal / max(len(paths), 1)),
                    )
                    suggestion = estimate_people_count(
                        path,
                        max_people=4,
                        preview_path=path.parent / "people_map.jpg",
                    )
                    shot_records.append(
                        {
                            **boundary.public(),
                            **suggestion,
                            "scene_cluster": int(scene_clusters[ordinal - 1]),
                            "source_path": str(path),
                            "mosaic_path": "",
                            "mosaic_reviewed": False,
                            "depth_path": "",
                            "white_model_path": "",
                            "performance_path": "",
                            "performance": {},
                            "scene_path": "",
                            "output_path": "",
                            "status": "ready",
                            "stage": "等待生成",
                            "progress": 0,
                            "task_id": "",
                            "actor_ids": [],
                            "generation_mode": "",
                            "requested_signature": "",
                            "output_signature": "",
                            "error": "",
                        }
                    )
                job.update(shots=shot_records, status="succeeded", stage=f"分镜分析完成：共 {len(shot_records)} 个", progress=100)
                job.log(
                    f"本地分析完成，共拆分 {len(shot_records)} 个分镜并给出人数建议；"
                    "请在页面核对每个镜头的实际出场角色，尚未调用任何付费接口。"
                )
                _persist_long_job(job)
            except Exception as exc:
                job_error(job, exc)

        threading.Thread(target=worker, daemon=True, name=f"long-analyze-{job.id}").start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/real-long-video/workspaces")
@app.get("/api/long-video/workspaces")
def get_long_video_workspaces():
    try:
        project = requested_long_project()
        return jsonify({"project": project, "workspaces": list_long_workspaces(project)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/real-long-video/workspaces")
@app.post("/api/long-video/workspaces")
def create_long_video_workspace():
    try:
        project = requested_long_project()
        data = request.get_json(silent=True) or {}
        record = create_long_workspace(project, str(data.get("name") or ""))
        return jsonify(
            {
                "workspace": public_long_workspace(record, project),
                "workspaces": list_long_workspaces(project),
            }
        ), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.patch("/api/real-long-video/workspaces/<workspace_id>")
@app.patch("/api/long-video/workspaces/<workspace_id>")
def rename_long_video_workspace(workspace_id: str):
    try:
        project = requested_long_project()
        data = request.get_json(silent=True) or {}
        record = update_long_workspace(project, workspace_id, name=str(data.get("name") or ""))
        return jsonify(
            {
                "workspace": public_long_workspace(record, project),
                "workspaces": list_long_workspaces(project),
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.delete("/api/real-long-video/workspaces/<workspace_id>")
@app.delete("/api/long-video/workspaces/<workspace_id>")
def remove_long_video_workspace(workspace_id: str):
    try:
        project = requested_long_project()
        delete_long_workspace(project, workspace_id)
        return jsonify({"deleted": True, "workspaces": list_long_workspaces(project)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.delete("/api/real-long-video/<job_id>/shots/<int:shot_index>")
@app.delete("/api/long-video/<job_id>/shots/<int:shot_index>")
def delete_long_video_shot(job_id: str, shot_index: int):
    try:
        job = restore_expected_long_job(job_id)
        if job.status in {"queued", "running"}:
            raise WorkflowError("当前任务正在运行，完成或失败后才能删除分镜。")
        if len(job.shots) <= 1:
            raise WorkflowError("至少需要保留一个分镜。")
        deleted = next(
            (shot for shot in job.shots if int(shot.get("index") or 0) == shot_index),
            None,
        )
        if deleted is None:
            raise WorkflowError(f"找不到分镜 {shot_index:02d}。")

        with job.lock:
            job.shots = [
                shot for shot in job.shots if int(shot.get("index") or 0) != shot_index
            ]
            job.mosaic_path = None
            job.white_model_path = None
            job.output_path = None
            summaries = [
                {"index": int(shot.get("index") or 0), **dict(shot["performance"])}
                for shot in job.shots
                if isinstance(shot.get("performance"), dict) and shot.get("performance")
            ]
            job.performance_path = (
                save_shot_manifest(
                    job.run_dir / "performance_summary.json",
                    {"version": 1, "shots": summaries},
                )
                if len(summaries) == len(job.shots)
                else None
            )
            job.kind = "long_edit"
            job.status = "succeeded"
            job.stage = f"已删除分镜 {shot_index:02d}，剩余 {len(job.shots)} 个分镜"
            job.progress = 100
            job.error = ""
        job.log(
            f"已从项目中删除分镜 {shot_index:02d}；原分镜文件仍保留在本地，"
            "后续白模与最终合并将忽略该镜。"
        )
        _persist_long_job(job)
        return jsonify(job.public())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/real-long-video/<job_id>/shots/<int:shot_index>/restore")
def restore_skipped_real_long_video_shot(job_id: str, shot_index: int):
    """Restore an intentionally skipped real-person shot without spending credits."""
    try:
        job = restore_expected_long_job(job_id, REAL_PERSON_LONG_PROJECT)
        if job.status in {"queued", "running"}:
            raise WorkflowError("当前任务正在运行，完成、暂停或等待人工处理后才能恢复分镜。")
        shot = next(
            (item for item in job.shots if int(item.get("index") or 0) == shot_index),
            None,
        )
        if shot is None:
            raise WorkflowError(f"找不到分镜 {shot_index:02d}。")
        if not real_long_shot_is_skipped(shot):
            raise WorkflowError(f"分镜 {shot_index:02d} 当前没有被跳过。")
        update_long_shot(
            job,
            shot_index,
            generation_skipped=False,
            generation_skipped_at="",
            composition_approval_required=False,
            output_signature="",
            status="ready",
            stage="已恢复，等待重新生成",
            progress=0,
            error="",
        )
        job.output_path = None
        job.update(
            kind="long_edit",
            status="succeeded",
            stage=f"已恢复分镜 {shot_index:02d}，请重新生成",
            progress=100,
            error="",
        )
        job.log(
            f"已恢复分镜 {shot_index:02d}；该操作不调用付费接口。"
            "下次生成时会强制重建本镜成片，不会复用跳过前的偏差结果。"
        )
        _persist_long_job(job)
        return jsonify(job.public())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/real-long-video/<job_id>/shots/<int:shot_index>/approve-white-model")
def approve_real_long_white_model(job_id: str, shot_index: int):
    """Allow a reviewed non-fatal white-model result without creating a paid task."""
    try:
        job = restore_expected_long_job(job_id, REAL_PERSON_LONG_PROJECT)
        if job.status in {"queued", "running"}:
            raise WorkflowError("当前任务正在运行，结束后才能人工确认白模。")
        shot = next(
            (item for item in job.shots if int(item.get("index") or 0) == shot_index),
            None,
        )
        if shot is None:
            raise WorkflowError(f"找不到分镜 {shot_index:02d}。")
        white_path = Path(str(shot.get("white_model_path") or ""))
        if not white_path.is_file():
            raise WorkflowError(f"分镜 {shot_index:02d} 没有可确认的白模视频。")
        qa = shot.get("white_model_qa")
        if not isinstance(qa, dict):
            qa = validate_white_model(
                Path(str(shot.get("source_path") or "")),
                white_path,
                expected_actor_count=max(
                    long_shot_stable_detected_people_count(shot),
                    int(shot.get("suggested_actor_count") or 0),
                ),
            )
        if white_model_has_hard_text_violation(qa):
            raise WorkflowError(
                f"分镜 {shot_index:02d} 白模含有字幕、文字、Logo、时间码或水印，属于不可人工放行的问题；"
                "请重新生成此镜白模。本地去字会先自动执行，且不会额外提交付费任务。"
            )
        update_long_shot(
            job,
            shot_index,
            white_model_qa=qa,
            white_model_manually_approved=True,
            white_model_approval_required=False,
            status="ready",
            stage="白模已人工确认可用",
            progress=100,
            error="",
        )
        sub_job = JOBS.get(str(shot.get("job_id") or ""))
        if sub_job is not None:
            sub_job.update(status="succeeded", stage="白模已人工确认可用", progress=100, error="")
        remaining = [
            int(item.get("index") or 0)
            for item in job.shots
            if not real_long_shot_is_skipped(item)
            and not real_long_white_model_is_approved(item)
        ]
        merged = rebuild_real_long_white_model_merge(job)
        if remaining:
            labels = "、".join(f"{value:02d}" for value in remaining)
            job.update(
                status="awaiting_approval",
                stage=f"白模分镜 {labels} 仍待处理",
                progress=100,
                error="",
            )
        else:
            job.update(
                status="succeeded",
                stage="全部白模已通过自动或人工质量闸门" if merged else "白模确认完成",
                progress=100,
                error="",
            )
        job.log(
            f"已人工确认分镜 {shot_index:02d} 白模可用；该操作不调用付费接口。"
            "最终成片仍会执行文字、构图和运镜验收。"
        )
        _persist_long_job(job)
        return jsonify(job.public())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


def _long_job_from_form(project: str | None = None) -> WebJob:
    expected = project or requested_long_project()
    source_job_id = request.form.get("source_job_id", "").strip()
    if not source_job_id:
        raise WorkflowError("请先分析并拆分长视频。")
    job = restore_expected_long_job(source_job_id, expected)
    if not job.shots:
        raise WorkflowError("所选任务没有可用的分镜分析结果。")
    if job.status in {"queued", "running"}:
        raise WorkflowError("长视频任务正在运行，请勿重复提交。")
    return job


@app.post("/api/real-long-video/mosaic")
@app.post("/api/long-video/mosaic")
def prepare_long_video_mosaic_job():
    try:
        job = _long_job_from_form()
        block_size = max(8, min(48, form_int("block_size", 18)))
        try:
            score_threshold = float(request.form.get("face_score_threshold", "0.55"))
        except ValueError as exc:
            raise WorkflowError("人脸检测阈值必须是数字。") from exc
        if not 0.30 <= score_threshold <= 0.90:
            raise WorkflowError("人脸检测阈值必须在 0.30–0.90 之间。")
        job.update(kind="long_mosaic", status="queued", stage="等待本地人脸打码", progress=0, error="")
        threading.Thread(
            target=run_long_mosaic_preparation,
            kwargs={"job": job, "block_size": block_size, "score_threshold": score_threshold},
            daemon=True,
            name=f"long-mosaic-{job.id}",
        ).start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/real-long-video/prepare-actors")
def prepare_real_long_video_actors_job():
    try:
        job = _long_job_from_form(REAL_PERSON_LONG_PROJECT)
        if not form_bool("real_person_authorized"):
            raise WorkflowError("请确认你已获得真人参考素材及其生成用途的合法授权。")
        actor_count = form_int("actor_count", 1)
        save_real_person_actor_uploads(job, actor_count=actor_count)
        job.update(
            kind="real_long_actor_prepare",
            status="queued",
            stage="等待上传并绑定火山角色库",
            progress=0,
            error="",
        )
        _persist_long_job(job)
        threading.Thread(
            target=run_real_person_reference_preparation,
            kwargs={"job": job},
            daemon=True,
            name=f"real-long-actors-{job.id}",
        ).start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/real-long-video/actors/<int:actor_index>/character-asset")
def upload_real_long_actor_character_asset(actor_index: int):
    try:
        job = _long_job_from_form(REAL_PERSON_LONG_PROJECT)
        if not form_bool("real_person_authorized"):
            raise WorkflowError("请确认你已获得真人参考素材及其生成用途的合法授权。")
        save_single_real_actor_library_upload(job, actor_index)
        job.update(
            kind="real_long_actor_prepare",
            status="queued",
            stage=f"等待上传人物 {actor_index} 到火山角色库",
            progress=0,
            error="",
        )
        _persist_long_job(job)
        threading.Thread(
            target=run_single_real_actor_library_upload,
            kwargs={"job": job, "actor_index": actor_index},
            daemon=True,
            name=f"real-long-actor-{actor_index}-{job.id}",
        ).start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.delete("/api/real-long-video/actors/<int:actor_index>/character-asset")
def delete_real_long_actor_character_asset(actor_index: int):
    try:
        data = request.get_json(silent=True) or {}
        source_job_id = str(data.get("source_job_id") or "").strip()
        if not source_job_id:
            raise WorkflowError("缺少真实人物任务 ID。")
        job = restore_expected_long_job(source_job_id, REAL_PERSON_LONG_PROJECT)
        actor = next(
            (item for item in job.actors if int(item.get("id") or 0) == actor_index),
            None,
        )
        if actor is None:
            raise WorkflowError(f"找不到人物 {actor_index} 的角色配置。")
        asset_uri = str(
            actor.get("trusted_asset_uri") or actor.get("ark_library_asset_uri") or ""
        )
        match = re.fullmatch(r"asset://(asset-[A-Za-z0-9_-]{6,120})", asset_uri)
        if not match:
            raise WorkflowError(f"人物 {actor_index} 尚未绑定可删除的火山角色素材。")
        requested_uri = str(data.get("asset_uri") or "").strip()
        if requested_uri and requested_uri != asset_uri:
            raise WorkflowError("页面中的角色素材已变化，请刷新后再删除。")
        ark_assets_client().delete_asset(match.group(1))
        local_source = str(actor.get("local_person_source") or "")
        for item in job.actors:
            bound_uri = str(
                item.get("trusted_asset_uri") or item.get("ark_library_asset_uri") or ""
            )
            if bound_uri != asset_uri:
                continue
            item_local = str(item.get("local_person_source") or "")
            item["person_source"] = item_local
            item["original_person_source"] = item_local
            item["trusted_asset_uri"] = ""
            item["ark_library_asset_uri"] = ""
            item["ark_library_upload_status"] = "已从角色库删除"
            item["ark_library_upload_error"] = ""
            item["ark_library_source_fingerprint"] = ""
            item["reference_layout"] = ""
        job.person_path = Path(local_source) if local_source and Path(local_source).is_file() else None
        job.update(
            status="succeeded",
            stage=f"人物 {actor_index} 已从火山角色库删除",
            progress=100,
            error="",
        )
        job.log(f"人物 {actor_index} 的火山角色素材 {asset_uri} 已永久删除并解除项目绑定。")
        _persist_long_job(job)
        return jsonify(job.public())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/real-long-video/performance")
@app.post("/api/long-video/performance")
def analyze_long_video_performance_job():
    try:
        job = _long_job_from_form()
        if not form_bool("performance_consent"):
            raise WorkflowError("请确认将原片分镜临时上传至火山方舟进行台词与表演分析。")
        job.update(kind="long_performance", status="queued", stage="等待台词与表演分析", progress=0, error="")
        threading.Thread(
            target=run_long_performance_analysis,
            args=(job,),
            daemon=True,
            name=f"long-performance-{job.id}",
        ).start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.put("/api/real-long-video/<job_id>/shots/<int:shot_index>/performance")
@app.put("/api/long-video/<job_id>/shots/<int:shot_index>/performance")
def update_long_video_shot_performance(job_id: str, shot_index: int):
    try:
        job = restore_expected_long_job(job_id)
        if job.status in {"queued", "running"}:
            raise WorkflowError("当前任务正在运行，完成或失败后才能修改台词与表演。")
        shot = next(
            (item for item in job.shots if int(item.get("index") or 0) == shot_index),
            None,
        )
        if shot is None:
            raise WorkflowError(f"找不到分镜 {shot_index:02d}。")
        existing = shot.get("performance")
        if not isinstance(existing, dict) or not existing:
            raise WorkflowError(f"分镜 {shot_index:02d} 尚未完成台词与表演提取。")
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise WorkflowError("台词与表演修改内容格式无效。")
        dialogue_text = payload.get("dialogue_text")
        performance_text = payload.get("performance_text")
        dialogue_sources = payload.get("dialogue_sources", [])
        if not isinstance(dialogue_text, str) or not isinstance(performance_text, str):
            raise WorkflowError("台词与表演必须是文字。")
        if not isinstance(dialogue_sources, list):
            raise WorkflowError("逐句声音来源格式无效。")
        dialogue_text = dialogue_text.replace("\r\n", "\n").replace("\r", "\n").strip()
        performance_text = performance_text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(dialogue_text) > 1800 or len(performance_text) > 1800:
            raise WorkflowError("单个文字框最多 1800 字，请保留必要的说话人、时序与表演信息。")
        if len(dialogue_text) + len(performance_text) > 2600:
            raise WorkflowError("本分镜台词与表演合计最多 2600 字，请精简后保存。")

        revised = dict(existing)
        revised_dialogue = [dict(item) for item in existing.get("dialogue") or [] if isinstance(item, dict)]
        allowed_source_types = {"character", "offscreen", "bgm_vocal", "ignore", "uncertain"}
        for source in dialogue_sources:
            if not isinstance(source, dict):
                raise WorkflowError("逐句声音来源格式无效。")
            try:
                item_index = int(source.get("index"))
                speaker_slot = int(source.get("speaker_slot") or 0)
            except (TypeError, ValueError) as exc:
                raise WorkflowError("逐句声音来源的序号或人物槽位无效。") from exc
            source_type = str(source.get("source_type") or "uncertain").strip()
            if item_index < 0 or item_index >= len(revised_dialogue) or source_type not in allowed_source_types:
                raise WorkflowError("逐句声音来源选项无效，请刷新页面后重试。")
            if source_type == "character":
                if speaker_slot < 1 or speaker_slot > 4:
                    raise WorkflowError("画内人物说话人必须选择 P1–P4。")
                revised_dialogue[item_index]["speaker_slot"] = speaker_slot
            else:
                revised_dialogue[item_index]["speaker_slot"] = 0
            revised_dialogue[item_index]["source_type"] = source_type
            revised_dialogue[item_index]["source_reviewed"] = True
        revised["dialogue"] = revised_dialogue
        revised["manual_dialogue_text"] = dialogue_text
        revised["manual_performance_text"] = performance_text
        revised["manual_reviewed"] = True
        revised["manual_reviewed_at"] = datetime.now().isoformat(timespec="seconds")
        revised["has_dialogue"] = any(
            str(item.get("source_type") or "character") in {"character", "offscreen"}
            for item in revised_dialogue
        ) if revised_dialogue else bool(dialogue_text)
        performance_value = str(shot.get("performance_path") or "")
        performance_path = Path(performance_value) if performance_value else None
        if performance_path is None or not performance_path.parent.is_dir():
            source_path = Path(str(shot.get("source_path") or ""))
            if not source_path.is_file():
                raise WorkflowError(f"找不到分镜 {shot_index:02d} 的本地目录。")
            performance_path = source_path.parent / "performance.json"
        save_shot_manifest(performance_path, revised)
        update_long_shot(
            job,
            shot_index,
            performance=revised,
            performance_path=str(performance_path),
            performance_dirty=True,
            stage="台词与表演已人工校订",
            error="",
        )
        job.performance_path = save_long_performance_summary(job)
        job.kind = "long_edit"
        job.status = "succeeded"
        job.stage = f"已保存分镜 {shot_index:02d} 的台词与表演校订"
        job.progress = 100
        job.error = ""
        job.log(f"分镜 {shot_index:02d} 的台词与表演已人工校订；下次生成将优先使用人工文字。")
        _persist_long_job(job)
        return jsonify(job.public())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/real-long-video/white-model")
@app.post("/api/long-video/white-model")
def generate_long_video_white_model_job():
    try:
        job = _long_job_from_form()
        regeneration_mode, force_shot_indices = parse_long_white_model_regeneration_request(job)
        confirmed_paid_retry_indices: set[int] = set()
        if regeneration_mode == "selected" and is_real_person_long_job(job):
            selected_index = next(iter(force_shot_indices))
            selected_shot = next(
                shot for shot in job.shots if int(shot.get("index") or 0) == selected_index
            )
            completed_attempts = long_white_model_generation_count(selected_shot)
            if completed_attempts >= 2:
                if not form_bool("white_retry_cost_confirmed"):
                    raise WorkflowError(
                        f"分镜 {selected_index:02d} 已完成 {completed_attempts} 次付费白模生成；"
                        "请在费用确认弹窗中明确同意后再继续。"
                    )
                confirmed_paid_retry_indices.add(selected_index)
        if not form_bool("privacy_review_confirmed"):
            raise WorkflowError("请逐镜检查打码结果，并确认没有遗漏任何可识别人脸。")
        missing_mosaic = [
            int(shot.get("index") or 0)
            for shot in job.shots
            if not Path(str(shot.get("mosaic_path") or "")).is_file()
        ]
        if missing_mosaic:
            raise WorkflowError(
                "请先生成全部打码分镜；缺少："
                + ", ".join(f"{value:02d}" for value in missing_mosaic)
            )
        for shot in job.shots:
            shot["mosaic_reviewed"] = True
        options = generation_options()
        options["model"] = DEFAULT_SEEDANCE_MODEL
        options["resolution"] = "480p"
        options["generate_audio"] = False
        options["watermark"] = False
        job.update(kind="long_white_model", status="queued", stage="等待逐分镜生成白模", progress=0, error="")
        job.log("白模阶段固定使用 Seedance 2.0 / 480p 低成本规格；最终重绘分辨率不受影响。")
        if regeneration_mode == "selected":
            selected_index = next(iter(force_shot_indices))
            job.log(f"已请求只重新生成分镜 {selected_index:02d} 的白模；其他白模将直接复用后重新合并。")
            if selected_index in confirmed_paid_retry_indices:
                completed_attempts = long_white_model_generation_count(
                    next(shot for shot in job.shots if int(shot.get("index") or 0) == selected_index)
                )
                job.log(
                    f"分镜 {selected_index:02d} 已完成 {completed_attempts} 次付费生成；"
                    "本次已重新确认费用，只会新增 1 次付费任务，不会自动追加纠偏任务。"
                )
        _persist_long_job(job)
        threading.Thread(
            target=run_long_white_model_generation,
            kwargs={
                "job": job,
                "options": options,
                "force_shot_indices": force_shot_indices,
                "reuse_unforced_outputs": regeneration_mode == "selected",
                "confirmed_paid_retry_indices": confirmed_paid_retry_indices,
            },
            daemon=True,
            name=f"long-white-model-{job.id}",
        ).start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/real-long-video/generate")
@app.post("/api/long-video/generate")
def generate_long_video_job():
    try:
        source_job_id = request.form.get("source_job_id", "").strip()
        if not source_job_id:
            raise WorkflowError("请先分析并拆分长视频。")
        project = requested_long_project()
        job = restore_expected_long_job(source_job_id, project)
        generation_strategy = request.form.get(
            "generation_strategy", REAL_PER_SHOT_GENERATION_STRATEGY
        ).strip() or REAL_PER_SHOT_GENERATION_STRATEGY
        if generation_strategy not in {
            REAL_PER_SHOT_GENERATION_STRATEGY,
            REAL_WHOLE_GENERATION_STRATEGY,
        }:
            raise WorkflowError("最终成片生成模式无效，请刷新页面后重新选择。")
        if generation_strategy == REAL_WHOLE_GENERATION_STRATEGY and project != REAL_PERSON_LONG_PROJECT:
            raise WorkflowError("整段白模一次生成只适用于真实人物复刻重绘项目。")
        if not job.shots:
            raise WorkflowError("所选任务没有可用的分镜分析结果。")
        if job.status in {"queued", "running"}:
            raise WorkflowError("长视频任务正在运行，请勿重复提交。")
        composition_approval_requested = form_bool("composition_retry_approved")
        skip_requested_index = form_int("skip_shot_index", 0)
        if skip_requested_index < 0:
            raise WorkflowError("跳过的分镜编号无效。")
        if skip_requested_index and project != REAL_PERSON_LONG_PROJECT:
            raise WorkflowError("跳过分镜继续生成只适用于真实人物复刻重绘。")
        if skip_requested_index and composition_approval_requested:
            raise WorkflowError("跳过分镜与继续付费纠偏不能同时提交。")
        skip_target: dict[str, Any] | None = None
        if skip_requested_index:
            skip_target = next(
                (
                    shot
                    for shot in job.shots
                    if int(shot.get("index") or 0) == skip_requested_index
                ),
                None,
            )
            if skip_target is None:
                raise WorkflowError(f"找不到分镜 {skip_requested_index:02d}。")
            if real_long_shot_is_skipped(skip_target):
                raise WorkflowError(f"分镜 {skip_requested_index:02d} 已经处于跳过状态。")
            has_previous_result = Path(str(skip_target.get("output_path") or "")).is_file()
            if not (
                has_previous_result
                or bool(skip_target.get("composition_approval_required"))
                or bool(skip_target.get("white_model_approval_required"))
                or str(skip_target.get("status") or "") == "failed"
            ):
                raise WorkflowError(
                    f"分镜 {skip_requested_index:02d} 尚未生成或失败，当前无需跳过。"
                )
        regeneration_mode, force_shot_indices = parse_long_regeneration_request(
            job,
            allow_missing_unforced_outputs=(
                project == REAL_PERSON_LONG_PROJECT
            ),
        )
        selected_only_generation = bool(
            project == REAL_PERSON_LONG_PROJECT
            and regeneration_mode == "selected"
            and not composition_approval_requested
        )
        if skip_requested_index and (regeneration_mode != "normal" or force_shot_indices):
            raise WorkflowError("跳过当前分镜并继续时不能同时选择重新生成模式。")
        skipped_indices = {
            int(shot.get("index") or 0)
            for shot in job.shots
            if project == REAL_PERSON_LONG_PROJECT and real_long_shot_is_skipped(shot)
        }
        if skip_requested_index:
            skipped_indices.add(skip_requested_index)
        pending_composition_indices = {
            int(shot.get("index") or 0)
            for shot in job.shots
            if bool(shot.get("composition_approval_required"))
            and int(shot.get("index") or 0) not in skipped_indices
        }
        composition_approved_shot_indices: set[int] = set()
        if composition_approval_requested:
            if regeneration_mode != "selected" or len(force_shot_indices) != 1:
                raise WorkflowError("构图纠偏人工同意必须通过对应分镜的专用按钮提交。")
            selected_index = next(iter(force_shot_indices))
            if selected_index not in pending_composition_indices:
                raise WorkflowError(f"分镜 {selected_index:02d} 当前不需要构图纠偏人工同意。")
            composition_approved_shot_indices = {selected_index}
        elif (
            pending_composition_indices
            and generation_strategy == REAL_PER_SHOT_GENERATION_STRATEGY
            and not selected_only_generation
            and regeneration_mode != "all"
        ):
            labels = "、".join(f"{value:02d}" for value in sorted(pending_composition_indices))
            raise WorkflowError(
                f"分镜 {labels} 已达到两次自动构图纠偏上限。"
                "请先预览结果，并点击对应分镜的“同意继续纠偏生成”。"
            )
        if form_bool("require_white_model") or project == REAL_PERSON_LONG_PROJECT:
            missing_white = [
                int(shot.get("index") or 0)
                for shot in job.shots
                if int(shot.get("index") or 0) not in skipped_indices
                and not Path(str(shot.get("white_model_path") or "")).is_file()
            ]
            if missing_white:
                raise WorkflowError(
                    "当前模式要求先完成全部白模分镜；缺少："
                    + ", ".join(f"{value:02d}" for value in missing_white)
                )
            if project == REAL_PERSON_LONG_PROJECT:
                blocked_white: list[int] = []
                for shot in job.shots:
                    index = int(shot.get("index") or 0)
                    if index in skipped_indices:
                        continue
                    if not isinstance(shot.get("white_model_qa"), dict):
                        white_path = Path(str(shot.get("white_model_path") or ""))
                        source_path = Path(str(shot.get("source_path") or ""))
                        qa = validate_white_model(
                            source_path,
                            white_path,
                            expected_actor_count=max(
                                long_shot_stable_detected_people_count(shot),
                                int(shot.get("suggested_actor_count") or 0),
                            ),
                        )
                        shot.update(
                            white_model_qa=qa,
                            white_model_approval_required=not qa["passed"],
                        )
                    if not real_long_white_model_is_approved(shot):
                        blocked_white.append(index)
                if blocked_white:
                    _persist_long_job(job)
                    raise WorkflowError(
                        "以下白模未通过质量闸门，系统已阻止提交最终付费成片："
                        + "、".join(f"{value:02d}" for value in blocked_white)
                        + "。请逐镜重新生成、人工确认使用或跳过。"
                    )
        actor_count = form_int("actor_count", 1)
        shot_casts = parse_long_shot_casts(job, actor_count)
        continuity_warnings = validate_long_cast_continuity(
            job,
            shot_casts,
            allow_manual_override=True,
        )
        global_manual_identity_override = bool(continuity_warnings)
        real_manual_identity_override_shots = (
            parse_real_long_manual_identity_override_shots(job)
            if project == REAL_PERSON_LONG_PROJECT
            else set()
        )
        for warning in continuity_warnings:
            job.log(f"人物身份映射提醒：{warning}。")
        for shot_index in sorted(real_manual_identity_override_shots):
            job.log(
                f"真实人物分镜 {shot_index:02d} 已采用人工逐镜人物选择；"
                "自动C身份仅作参考，不会改变其他分镜的人物配置。"
            )
        shot_position_locks = parse_long_shot_position_locks(job, shot_casts)
        used_actor_ids = {
            actor_id
            for shot_index, values in shot_casts.items()
            if shot_index not in skipped_indices
            for actor_id in values
        }
        actors = save_long_actor_references(
            job,
            actor_count=actor_count,
            used_actor_ids=used_actor_ids,
            require_real_derivatives=project == REAL_PERSON_LONG_PROJECT,
        )
        target_scenes, scene_assignments = save_long_scene_library(
            job,
            require_assignments=generation_strategy == REAL_PER_SHOT_GENERATION_STRATEGY,
        )
        options = generation_options()
        if project == REAL_PERSON_LONG_PROJECT:
            if generation_strategy == REAL_WHOLE_GENERATION_STRATEGY:
                options["model"] = DEFAULT_SEEDANCE_25_MODEL
            validate_real_final_video_options(options)
            options = normalize_real_final_video_options(options)
        styled_prompt, style_preset = apply_final_style_prompt(
            request.form.get("prompt", ""),
            request.form.get("final_style_id", "match_character"),
        )
        options["prompt"] = styled_prompt
        options["final_style_id"] = style_preset["id"]
        options["dialogue_voice_mode"] = "seedance_new_voice"
        options["generate_audio"] = True
        options["watermark"] = False
        options["preserve_original_audio"] = False
        missing_performance = [
            int(shot.get("index") or 0)
            for shot in job.shots
            if int(shot.get("index") or 0) not in skipped_indices
            and not isinstance(shot.get("performance"), dict)
        ]
        if missing_performance:
            labels = "、".join(f"{value:02d}" for value in missing_performance)
            raise WorkflowError(
                f"生成新角色声音前需要先完成全部分镜的台词与表演分析；缺少分镜：{labels}。"
            )
        scene_prompt = request.form.get("scene_prompt", "").strip() or DEFAULT_LONG_SCENE_PLATE_PROMPT
        image_model = request.form.get("image_model", "").strip() or os.getenv(
            "ARK_IMAGE_MODEL", DEFAULT_SEEDREAM_MODEL
        )
        blur_range = request.form.get("blur_range", "").strip()
        if generation_strategy == REAL_WHOLE_GENERATION_STRATEGY:
            if regeneration_mode != "normal" or force_shot_indices:
                raise WorkflowError("整段白模模式一次生成完整成片，不支持逐镜重新生成。")
            if skip_requested_index or composition_approval_requested:
                raise WorkflowError("整段白模模式不能同时执行逐镜跳过或逐镜构图纠偏。")
            active_actor_ids = sorted(used_actor_ids)
            selected_whole_actors = [
                actor for actor in actors if int(actor.get("id") or 0) in active_actor_ids
            ]
            if not selected_whole_actors:
                raise WorkflowError("整段白模模式至少需要一位已绑定的火山角色人物。")
            if len(selected_whole_actors) > 4:
                raise WorkflowError("Seedance 2.5 整段生成当前最多支持 4 位火山角色人物。")
            whole_scene = selected_whole_scene_reference(
                job,
                request.form.get("whole_scene_reference", ""),
            )
            for shot in job.shots:
                index = int(shot.get("index") or 0)
                actor_ids = shot_casts[index]
                shot.update(
                    actor_ids=actor_ids,
                    actor_count=len(actor_ids),
                    actor_mappings=[
                        {
                            "slot": slot_index + 1,
                            "actor_id": actor_id,
                            "source_position": shot_position_locks[index][slot_index],
                        }
                        for slot_index, actor_id in enumerate(actor_ids)
                    ],
                    cast_confirmed=True,
                    manual_identity_override=(
                        index in real_manual_identity_override_shots
                        if project == REAL_PERSON_LONG_PROJECT
                        else global_manual_identity_override
                    ),
                    position_binding_manual=bool(actor_ids),
                )
            job.output_path = None
            job.update(
                kind="long_generate",
                generation_strategy=REAL_WHOLE_GENERATION_STRATEGY,
                status="queued",
                stage="等待整段白模一次生成",
                progress=0,
                error="",
                pause_requested=False,
            )
            job.log(
                "已选择模式二：整段合并白模一次生成。不会生成新的逐镜场景图，"
                "只使用所选原始场景图，并固定提交一个 Seedance 2.5 视频编辑任务。"
            )
            _persist_long_job(job)
            threading.Thread(
                target=run_real_whole_video_generation,
                kwargs={
                    "job": job,
                    "actors": selected_whole_actors,
                    "scene_reference": whole_scene,
                    "options": options,
                },
                daemon=True,
                name=f"real-long-whole-generate-{job.id}",
            ).start()
            return jsonify(job.public()), 202
        actor_by_id = {int(actor["id"]): actor for actor in actors}
        incompatible_unforced_shots: list[int] = []
        reusable_unforced_shots: set[int] = set()
        for shot in job.shots:
            index = int(shot.get("index") or 0)
            actor_ids = shot_casts[index]
            selected_actors = [actor_by_id[value] for value in actor_ids]
            scene_source = target_scenes.get(index, "")
            assignment = scene_assignments[index]
            signature = long_shot_generation_signature(
                selected_actors,
                options=options,
                scene_prompt=scene_prompt,
                image_model=image_model,
                scene_source=scene_source,
                motion_reference=str(shot.get("white_model_path") or ""),
                performance=shot.get("performance") if isinstance(shot.get("performance"), dict) else None,
                position_locks=shot_position_locks[index],
                cast_character_ids=[
                    (
                        0
                        if (
                            index in real_manual_identity_override_shots
                            if project == REAL_PERSON_LONG_PROJECT
                            else global_manual_identity_override
                        )
                        else int(cast_continuity_assignment_map(job).get((index, slot), {}).get("character_id") or 0)
                    )
                    for slot in range(1, len(selected_actors) + 1)
                ],
                signature_version=(
                    REAL_LONG_SHOT_SIGNATURE_VERSION
                    if project == REAL_PERSON_LONG_PROJECT
                    else 14
                ),
            )
            legacy_prompt_signature = long_shot_generation_signature(
                selected_actors,
                options=options,
                scene_prompt=scene_prompt,
                image_model=image_model,
                scene_source=scene_source,
                motion_reference=str(shot.get("white_model_path") or ""),
                performance=shot.get("performance") if isinstance(shot.get("performance"), dict) else None,
                position_locks=shot_position_locks[index],
                signature_version=12,
            )
            existing_output_value = str(shot.get("output_path") or "")
            existing_output = Path(existing_output_value) if existing_output_value else None
            # Seedance 2.5 previously accepted some white-model edits with an
            # explicit ratio before later shots were classified as video editing.
            # Treat an otherwise identical successful legacy output as compatible
            # with the now-required adaptive request so fixing one failed shot does
            # not regenerate already-paid shots 1..N.
            legacy_seedance25_signature = ""
            if (
                project == REAL_PERSON_LONG_PROJECT
                and str(options.get("model") or "") == DEFAULT_SEEDANCE_25_MODEL
                and str(options.get("ratio") or "") == "adaptive"
                and existing_output is not None
                and existing_output.is_file()
            ):
                try:
                    shot_record = json.loads(
                        (Path(str(shot.get("source_path") or "")).parent / "job.json").read_text(
                            encoding="utf-8"
                        )
                    )
                except (OSError, ValueError, TypeError):
                    shot_record = {}
                legacy_ratio = str(shot_record.get("ratio") or "").strip()
                if legacy_ratio and legacy_ratio != "adaptive":
                    legacy_options = dict(options)
                    legacy_options["ratio"] = legacy_ratio
                    legacy_seedance25_signature = long_shot_generation_signature(
                        selected_actors,
                        options=legacy_options,
                        scene_prompt=scene_prompt,
                        image_model=image_model,
                        scene_source=scene_source,
                        motion_reference=str(shot.get("white_model_path") or ""),
                        performance=(
                            shot.get("performance")
                            if isinstance(shot.get("performance"), dict)
                            else None
                        ),
                        position_locks=shot_position_locks[index],
                        cast_character_ids=[
                            (
                                0
                                if index in real_manual_identity_override_shots
                                else int(
                                    cast_continuity_assignment_map(job)
                                    .get((index, slot), {})
                                    .get("character_id")
                                    or 0
                                )
                            )
                            for slot in range(1, len(selected_actors) + 1)
                        ],
                        signature_version=REAL_LONG_SHOT_SIGNATURE_VERSION,
                    )
                    if str(shot.get("output_signature") or "") == legacy_seedance25_signature:
                        shot["output_signature"] = signature
                        job.log(
                            f"分镜 {index:02d} 已兼容迁移为 Seedance 2.5 adaptive 参数；"
                            "复用已有成功成片，不会重复提交付费任务。"
                        )
            unforced_output_compatible = bool(
                regeneration_mode == "selected"
                and index not in force_shot_indices
                and existing_output is not None
                and existing_output.is_file()
                and str(shot.get("status") or "") == "succeeded"
                and not bool(shot.get("composition_approval_required"))
                and not bool(shot.get("performance_dirty"))
                and not bool(shot.get("position_binding_dirty"))
                and str(shot.get("dialogue_voice_mode") or "")
                == str(options.get("dialogue_voice_mode") or "")
                and str(shot.get("final_style_id") or "")
                == str(options.get("final_style_id") or "")
                and str(shot.get("output_signature") or "")
                in {signature, legacy_prompt_signature, legacy_seedance25_signature}
            )
            if unforced_output_compatible:
                reusable_unforced_shots.add(index)
            if (
                regeneration_mode == "selected"
                and index not in force_shot_indices
                and existing_output is not None
                and existing_output.is_file()
                and not unforced_output_compatible
            ):
                incompatible_unforced_shots.append(index)
            scene_signature = long_scene_plate_signature(
                shot_index=index,
                scene_source=scene_source,
                source_reference=str(shot.get("source_path") or ""),
                motion_reference=str(shot.get("white_model_path") or shot.get("depth_path") or ""),
                prompt=scene_prompt,
                model=image_model,
                signature_version=(
                    REAL_SCENE_PLATE_SIGNATURE_VERSION
                    if project == REAL_PERSON_LONG_PROJECT
                    else SCENE_PLATE_SIGNATURE_VERSION
                ),
            )
            shot.update(
                actor_ids=actor_ids,
                actor_count=len(actor_ids),
                actor_mappings=[
                    {
                        "slot": slot_index + 1,
                        "actor_id": actor_id,
                        "source_position": shot_position_locks[index][slot_index],
                    }
                    for slot_index, actor_id in enumerate(actor_ids)
                ],
                target_scene_path=scene_source,
                scene_group_id=assignment["group_id"],
                scene_group_name=assignment["group_name"],
                scene_image_index=assignment["image_index"],
                requested_scene_signature=scene_signature,
                requested_signature=signature,
                cast_confirmed=True,
                manual_identity_override=(
                    index in real_manual_identity_override_shots
                    if project == REAL_PERSON_LONG_PROJECT
                    else global_manual_identity_override
                ),
                position_binding_manual=bool(actor_ids),
                position_binding_dirty=(
                    bool(shot.get("position_binding_dirty"))
                    or len(shot.get("actor_mappings") or []) != len(actor_ids)
                    or any(
                        str((shot.get("actor_mappings") or [{}])[slot_index].get("source_position") or "")
                        != shot_position_locks[index][slot_index]
                        for slot_index in range(min(len(actor_ids), len(shot.get("actor_mappings") or [])))
                    )
                ),
            )
        deferred_unforced_shot_indices: set[int] = set()
        if selected_only_generation:
            deferred_unforced_shot_indices = {
                int(shot.get("index") or 0)
                for shot in job.shots
                if int(shot.get("index") or 0) not in force_shot_indices
                and int(shot.get("index") or 0) not in skipped_indices
                and int(shot.get("index") or 0) not in reusable_unforced_shots
            }
            if deferred_unforced_shot_indices:
                labels = "、".join(
                    f"{value:02d}" for value in sorted(deferred_unforced_shot_indices)
                )
                job.log(
                    f"本次采用真实人物单镜隔离生成；分镜 {labels} 当前不可安全复用，"
                    "不会阻止所选镜头生成，也不会进入本次整片合并。"
                )
        if incompatible_unforced_shots and not selected_only_generation:
            labels = "、".join(f"{value:02d}" for value in incompatible_unforced_shots)
            raise WorkflowError(
                "其他已完成分镜与当前的新音色/提示词配置不兼容，无法只重生单镜后安全合并；"
                f"涉及分镜：{labels}。请先点击“按角色配置生成并合并”完成一次整片升级。"
            )
        if skip_target is not None:
            update_long_shot(
                job,
                skip_requested_index,
                generation_skipped=True,
                generation_skipped_at=datetime.now().isoformat(timespec="seconds"),
                composition_approval_required=False,
                status="skipped",
                stage="已跳过，不参与最终合并",
                progress=100,
                error="",
            )
            job.log(
                f"已按人工选择跳过分镜 {skip_requested_index:02d}；"
                "跳过前的结果仍保留用于预览和下载，但不会进入整片，系统将继续生成后续分镜。"
            )
        job.output_path = None
        job.update(
            kind="long_generate",
            generation_strategy=REAL_PER_SHOT_GENERATION_STRATEGY,
            status="queued",
            stage="等待逐分镜生成",
            progress=0,
            error="",
            pause_requested=False,
        )
        job.log(
            "已启用 Seedance 新角色音色：提交参考完全静音；台词、说话人和节奏只读取提取及人工校订时间轴，"
            "最终声音由新人物形象生成，绝不携带或直接合回原片声纹。"
        )
        if regeneration_mode == "all":
            job.log(f"已请求强制重新生成全部 {len(force_shot_indices)} 个分镜。")
        elif regeneration_mode == "selected":
            selected_index = next(iter(force_shot_indices))
            if selected_only_generation:
                job.log(
                    f"已请求独立重新生成分镜 {selected_index:02d}；"
                    "其他有效分镜仅在兼容时复用，暂停、失败或旧配置分镜不会拦截，也不会被混入整片。"
                )
            else:
                job.log(f"已请求只重新生成分镜 {selected_index:02d}；其他分镜将直接复用后重新合并。")
        elif skip_requested_index:
            job.log(f"已请求跳过分镜 {skip_requested_index:02d}并从下一未完成分镜继续。")
        _persist_long_job(job)
        threading.Thread(
            target=run_long_video_generation,
            kwargs={
                "job": job,
                "actors": actors,
                "scene_prompt": scene_prompt,
                "image_model": image_model,
                "options": options,
                "blur_range": blur_range,
                "force_shot_indices": force_shot_indices,
                "composition_approved_shot_indices": composition_approved_shot_indices,
                "reuse_unforced_outputs": regeneration_mode == "selected",
                "selected_only_generation": selected_only_generation,
                "deferred_unforced_shot_indices": deferred_unforced_shot_indices,
            },
            daemon=True,
            name=f"long-generate-{job.id}",
        ).start()
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/real-long-video/pause")
def pause_real_long_video_job():
    try:
        data = request.get_json(silent=True) or {}
        source_job_id = str(
            data.get("source_job_id") or request.form.get("source_job_id", "")
        ).strip()
        if not source_job_id:
            raise WorkflowError("缺少需要暂停的真实人物任务ID。")
        job = restore_expected_long_job(source_job_id, REAL_PERSON_LONG_PROJECT)
        if job.status == "paused":
            return jsonify(job.public())
        if job.kind != "long_generate" or job.status not in {"queued", "running"}:
            raise WorkflowError("当前真实人物任务没有正在运行的最终成片生成。")
        job.update(
            pause_requested=True,
            stage="已收到暂停请求，正在停止本地跟进",
            error="",
        )
        job.log(
            "已收到人工暂停请求：不会取消已提交的云端任务，但将停止自动纠偏和后续分镜提交。"
        )
        with JOBS_LOCK:
            sub_jobs = [
                candidate
                for candidate in JOBS.values()
                if candidate.project == REAL_PERSON_LONG_PROJECT
                and candidate.id.startswith(f"{job.id}-s")
                and candidate.status in {"queued", "running"}
            ]
        for sub_job in sub_jobs:
            sub_job.update(pause_requested=True)
        _persist_long_job(job)
        return jsonify(job.public()), 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/real-long-video/archives")
def get_real_long_archives():
    workspace_id = str(request.args.get("workspace_id") or "").strip()
    workspace = get_long_workspace_record(REAL_PERSON_LONG_PROJECT, workspace_id)
    return jsonify(
        {
            "archives": list_real_long_archives(
                workspace_id,
                workspace_job_id=str(workspace.get("job_id") or "") if workspace else "",
            )
        }
    )


@app.post("/api/real-long-video/archives")
def create_real_long_archive():
    try:
        data = request.get_json(silent=True) or {}
        source_job_id = str(data.get("source_job_id") or "").strip()
        name = " ".join(str(data.get("name") or "").split())[:80]
        if not source_job_id:
            raise WorkflowError("请先完成或打开一个真实人物复刻任务。")
        job = restore_expected_long_job(source_job_id, REAL_PERSON_LONG_PROJECT)
        if job.status in {"queued", "running"}:
            raise WorkflowError("任务运行中不能创建存档，请先等待完成或暂停。")
        manifest = _persist_long_job(job)
        snapshot = json.loads(manifest.read_text(encoding="utf-8"))
        archive_id = uuid.uuid4().hex[:12]
        record = {
            "id": archive_id,
            "name": name or f"真实人物复刻 · {datetime.now().strftime('%m-%d %H:%M')}",
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "workspace_id": job.workspace_id,
            "manifest_path": str(manifest),
            "snapshot": snapshot,
        }
        save_shot_manifest(real_long_archive_path(archive_id), record)
        return jsonify(
            {
                "archive": next(
                    item
                    for item in list_real_long_archives(job.workspace_id, workspace_job_id=job.id)
                    if item["id"] == archive_id
                )
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/real-long-video/archives/<archive_id>/restore")
def restore_real_long_archive(archive_id: str):
    try:
        data = request.get_json(silent=True) or {}
        requested_workspace_id = str(data.get("workspace_id") or "").strip()
        path = real_long_archive_path(archive_id)
        if not path.is_file():
            raise WorkflowError("所选存档不存在或已被删除。")
        record = json.loads(path.read_text(encoding="utf-8"))
        snapshot = record.get("snapshot")
        if not isinstance(snapshot, dict) or snapshot.get("project") != REAL_PERSON_LONG_PROJECT:
            raise WorkflowError("存档内容无效，无法恢复。")
        archive_workspace_id = str(record.get("workspace_id") or snapshot.get("workspace_id") or "")
        if requested_workspace_id and archive_workspace_id and requested_workspace_id != archive_workspace_id:
            raise WorkflowError("该存档不属于当前重绘项目。")
        target_workspace_id = requested_workspace_id or archive_workspace_id
        with JOBS_LOCK:
            active = [
                job for job in JOBS.values()
                if job.project == REAL_PERSON_LONG_PROJECT
                and job.status in {"queued", "running"}
                and (
                    (target_workspace_id and job.workspace_id == target_workspace_id)
                    or (not target_workspace_id and job.id == str(snapshot.get("local_job_id") or ""))
                )
            ]
        if active:
            raise WorkflowError("当前重绘项目仍在运行，请先完成或暂停后再恢复它的存档；其他重绘项目不受影响。")
        manifest_value = str(record.get("manifest_path") or "")
        manifest = Path(manifest_value)
        runs_root = (PROJECT_DIR / "runs").resolve()
        if not manifest.is_absolute():
            manifest = runs_root / manifest
        manifest = manifest.resolve()
        if manifest.name != "real_long_manifest.json" or not manifest.parent.is_relative_to(runs_root):
            raise WorkflowError("存档对应的任务目录无效。")
        manifest.parent.mkdir(parents=True, exist_ok=True)
        if target_workspace_id:
            workspace = get_long_workspace_record(REAL_PERSON_LONG_PROJECT, target_workspace_id)
            snapshot["workspace_id"] = target_workspace_id
            snapshot["workspace_name"] = str(workspace.get("name") or "") if workspace else ""
        save_shot_manifest(manifest, snapshot)
        snapshot_job_id = str(snapshot.get("local_job_id") or "")
        with JOBS_LOCK:
            JOBS.pop(snapshot_job_id, None)
        restored = restore_latest_long_video_job(REAL_PERSON_LONG_PROJECT, snapshot_job_id)
        if restored is None:
            raise WorkflowError("存档文件已写回，但任务状态恢复失败。")
        if target_workspace_id:
            bind_long_workspace(restored, target_workspace_id)
        restored.log(f"已恢复存档：{record.get('name') or archive_id}。")
        _persist_long_job(restored)
        return jsonify(restored.public())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.delete("/api/real-long-video/archives/<archive_id>")
def delete_real_long_archive(archive_id: str):
    try:
        path = real_long_archive_path(archive_id)
        if not path.is_file():
            raise WorkflowError("所选存档不存在或已被删除。")
        path.unlink()
        workspace_id = str(request.args.get("workspace_id") or "").strip()
        workspace = get_long_workspace_record(REAL_PERSON_LONG_PROJECT, workspace_id)
        return jsonify(
            {
                "deleted": True,
                "archives": list_real_long_archives(
                    workspace_id,
                    workspace_job_id=str(workspace.get("job_id") or "") if workspace else "",
                ),
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/query")
def query_existing_task():
    data = request.get_json(silent=True) or {}
    task_id = str(data.get("task_id") or "").strip()
    if not task_id:
        return jsonify({"error": "任务 ID 不能为空。"}), 400
    project = str(data.get("project") or "single_person_replication").strip()
    allowed_projects = {
        "single_person_replication",
        "multi_person_replication",
        "person_only_replacement",
        "scene_only_replacement",
        "clothing_only_replacement",
        "long_video_replication",
        "real_person_long_video_replication",
    }
    if project not in allowed_projects:
        project = "single_person_replication"
    job = new_job("multi_query" if project == "multi_person_replication" else "query")
    job.project = project
    source_job_id = str(data.get("source_job_id") or "").strip()
    source_job = None
    if source_job_id:
        try:
            source_job = get_job(source_job_id)
        except WorkflowError:
            source_job = restore_cloud_job(source_job_id)
    if source_job:
        job.depth_path = source_job.depth_path
        job.scene_path = source_job.scene_path
        job.person_path = source_job.person_path
        job.clothing_path = source_job.clothing_path
    job.update(task_id=task_id, cloud_status="queued", cloud_started_at=time.time())
    persist_cloud_job(job, status="running")
    threading.Thread(target=resume_cloud_job, args=(job,), daemon=True, name=f"query-{job.id}").start()
    return jsonify(job.public()), 202


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    try:
        try:
            job = get_job(job_id)
        except WorkflowError:
            job = restore_cloud_job(job_id)
            if job is None:
                job = restore_person_retry_job(job_id)
            if job is None:
                job = restore_person_submission_job(job_id)
            if job is None:
                candidate = restore_latest_clothing_only_job()
                job = candidate if candidate and candidate.id == job_id else None
            if job is None:
                job = restore_wardrobe_swap_job(job_id)
            if job is None:
                candidate = restore_latest_long_video_job(VIRTUAL_LONG_PROJECT, job_id)
                job = candidate if candidate and candidate.id == job_id else None
            if job is None:
                candidate = restore_latest_long_video_job(REAL_PERSON_LONG_PROJECT, job_id)
                job = candidate if candidate and candidate.id == job_id else None
            if job is None:
                raise
        if job.project == WARDROBE_SWAP_PROJECT and job.kind.startswith("wardrobe_prepare_"):
            restore_wardrobe_output_link(job, wardrobe_mode_from_job(job))
            reconcile_wardrobe_white_task_state(job)
            persist_wardrobe_swap_job(job, wardrobe_mode_from_job(job))
        return jsonify(job.public())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 404


@app.get("/api/jobs/<job_id>/file/<kind>")
def job_file(job_id: str, kind: str):
    try:
        try:
            job = get_job(job_id)
        except WorkflowError:
            job = (
                restore_cloud_job(job_id, resume=False)
                or restore_person_retry_job(job_id)
                or restore_person_submission_job(job_id)
            )
            if job is None:
                candidate = restore_latest_clothing_only_job()
                job = candidate if candidate and candidate.id == job_id else None
            if job is None:
                job = restore_wardrobe_swap_job(job_id)
            if job is None:
                candidate = restore_latest_long_video_job(VIRTUAL_LONG_PROJECT, job_id)
                job = candidate if candidate and candidate.id == job_id else None
            if job is None:
                candidate = restore_latest_long_video_job(REAL_PERSON_LONG_PROJECT, job_id)
                job = candidate if candidate and candidate.id == job_id else None
            if job is None:
                raise
        if job.project == WARDROBE_SWAP_PROJECT and job.kind.startswith("wardrobe_prepare_"):
            if restore_wardrobe_output_link(job, wardrobe_mode_from_job(job)):
                persist_wardrobe_swap_job(job, wardrobe_mode_from_job(job))
        path = (
            job.depth_path
            if kind == "depth"
            else job.output_path
            if kind == "output"
            else job.scene_path
            if kind == "scene"
            else job.person_path
            if kind == "person"
            else job.clothing_path
            if kind == "clothing"
            else job.mosaic_path
            if kind == "mosaic"
            else job.white_model_path
            if kind == "white_model"
            else job.performance_path
            if kind == "performance"
            else None
        )
        if path is None or not path.is_file():
            raise WorkflowError("文件尚未生成或已经不存在。")
        as_attachment = request.args.get("download") == "1"
        if kind in {"depth", "output", "mosaic", "white_model"}:
            mimetype = "video/mp4"
        elif kind == "performance":
            mimetype = "application/json"
        else:
            mimetype = {
                ".png": "image/png",
                ".webp": "image/webp",
                ".bmp": "image/bmp",
            }.get(path.suffix.lower(), "image/jpeg")
        return send_file(path, mimetype=mimetype, as_attachment=as_attachment, download_name=path.name, conditional=True)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 404


@app.get("/api/jobs/<job_id>/shots/<int:shot_index>/file/<kind>")
def shot_job_file(job_id: str, shot_index: int, kind: str):
    try:
        try:
            job = get_job(job_id)
        except WorkflowError:
            job = restore_latest_long_video_job(VIRTUAL_LONG_PROJECT, job_id)
            if job is None:
                job = restore_latest_long_video_job(REAL_PERSON_LONG_PROJECT, job_id)
                if job is None:
                    raise
        if not is_long_video_project(job.project):
            raise WorkflowError("该任务不属于项目 6。")
        shot = next(
            (item for item in job.shots if int(item.get("index") or 0) == shot_index),
            None,
        )
        if shot is None:
            raise WorkflowError("找不到该分镜。")
        if kind not in {
            "source", "depth", "scene", "output", "raw_output", "people_map", "target_scene",
            "mosaic", "white_model", "performance",
        }:
            raise WorkflowError("不支持的分镜文件类型。")
        if kind == "raw_output":
            path = long_shot_raw_output_path(shot)
            path = path.expanduser().resolve() if path else None
        else:
            raw_path = shot.get(f"{kind}_path")
            path = Path(str(raw_path)).expanduser().resolve() if raw_path else None
        if path is None or not path.is_file() or not path.is_relative_to((PROJECT_DIR / "runs").resolve()):
            raise WorkflowError("分镜文件尚未生成或已经不存在。")
        as_attachment = request.args.get("download") == "1"
        mimetype = "video/mp4" if kind in {"source", "depth", "output", "raw_output", "mosaic", "white_model"} else (
            "application/json" if kind == "performance" else {
            ".png": "image/png", ".webp": "image/webp", ".bmp": "image/bmp"
            }.get(path.suffix.lower(), "image/jpeg")
        )
        return send_file(path, mimetype=mimetype, as_attachment=as_attachment, download_name=path.name, conditional=True)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 404


@app.get("/api/jobs/<job_id>/actors/<int:actor_index>/file/<kind>")
def long_actor_reference_file(job_id: str, actor_index: int, kind: str):
    try:
        try:
            job = get_job(job_id)
        except WorkflowError:
            job = restore_latest_long_video_job(REAL_PERSON_LONG_PROJECT, job_id)
            if job is None:
                raise
        if job.project != REAL_PERSON_LONG_PROJECT or kind not in {"person", "clothing", "masked", "sketch"}:
            raise WorkflowError("找不到该真人参考图。")
        actor = next(
            (item for item in job.actors if int(item.get("id") or 0) == actor_index),
            None,
        )
        if kind in {"person", "clothing"}:
            path = real_long_actor_local_reference_path(job, actor or {}, actor_index, kind)
        else:
            source_key = "masked_person_source" if kind == "masked" else "sketch_person_source"
            raw_path = actor.get(source_key) if actor else ""
            path = Path(str(raw_path)).expanduser().resolve() if raw_path else None
        if path is None or not path.is_file() or not path.is_relative_to((PROJECT_DIR / "runs").resolve()):
            raise WorkflowError("真人参考图尚未生成或已经不存在。")
        mimetype = {".png": "image/png", ".webp": "image/webp", ".bmp": "image/bmp"}.get(
            path.suffix.lower(), "image/jpeg"
        )
        return send_file(
            path,
            mimetype=mimetype,
            as_attachment=request.args.get("download") == "1",
            download_name=path.name,
            conditional=True,
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 404


@app.get("/api/jobs/<job_id>/scene-groups/<group_id>/<int:image_index>")
def long_scene_group_file(job_id: str, group_id: str, image_index: int):
    try:
        try:
            job = get_job(job_id)
        except WorkflowError:
            job = restore_latest_long_video_job(VIRTUAL_LONG_PROJECT, job_id)
            if job is None:
                job = restore_latest_long_video_job(REAL_PERSON_LONG_PROJECT, job_id)
                if job is None:
                    raise
        if not is_long_video_project(job.project) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", group_id):
            raise WorkflowError("找不到该项目 6 场景组。")
        group = next((item for item in job.scene_groups if str(item.get("id") or "") == group_id), None)
        images = list(group.get("images") or []) if group else []
        if not 1 <= image_index <= len(images):
            raise WorkflowError("找不到该场景参考图。")
        path = Path(str(images[image_index - 1])).expanduser().resolve()
        if not path.is_file() or not path.is_relative_to((PROJECT_DIR / "runs").resolve()):
            raise WorkflowError("场景参考图已经不存在。")
        mimetype = {".png": "image/png", ".webp": "image/webp", ".bmp": "image/bmp"}.get(
            path.suffix.lower(), "image/jpeg"
        )
        return send_file(path, mimetype=mimetype, conditional=True)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 404


@app.post("/api/jobs/<job_id>/open-folder")
def open_folder(job_id: str):
    try:
        try:
            job = get_job(job_id)
        except WorkflowError:
            job = (
                restore_cloud_job(job_id, resume=False)
                or restore_person_retry_job(job_id)
                or restore_person_submission_job(job_id)
            )
            if job is None:
                candidate = restore_latest_clothing_only_job()
                job = candidate if candidate and candidate.id == job_id else None
            if job is None:
                candidate = restore_latest_long_video_job(VIRTUAL_LONG_PROJECT, job_id)
                job = candidate if candidate and candidate.id == job_id else None
            if job is None:
                candidate = restore_latest_long_video_job(REAL_PERSON_LONG_PROJECT, job_id)
                job = candidate if candidate and candidate.id == job_id else None
            if job is None:
                raise
        if not hasattr(os, "startfile"):
            raise WorkflowError("正式网站运行在服务器上，无法替你打开服务器目录；请使用页面下载入口。")
        os.startfile(str(job.run_dir))
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": "上传文件总大小超过 450 MB。"}), 413


def main() -> None:
    parser = argparse.ArgumentParser(description="本地深度视频与 Seedance 2.0 网页工作流")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    load_env_file(override=True)
    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"本地网页已启动：{url}", flush=True)
    print("关闭此窗口即可停止服务。", flush=True)
    serve(app, host=args.host, port=args.port, threads=8)


if __name__ == "__main__":
    main()
