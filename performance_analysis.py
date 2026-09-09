from __future__ import annotations

import json
import re
from typing import Any, Callable

import requests

from workflow_core import DEFAULT_ARK_BASE_URL, ArkAPIError, ArkConnectionError, WorkflowError


DEFAULT_PERFORMANCE_MODEL = "doubao-seed-2-0-lite-260215"


class AnalysisOutputError(WorkflowError):
    """A received analysis reply was unusable; never an uncertain POST."""


def _without_trailing_commas(text: str) -> str:
    # Repair punctuation only, outside strings. Never invent missing content.
    return re.sub(
        r'"(?:\\.|[^"\\])*"|(,)\s*(?=[}\]])',
        lambda match: "" if match.group(1) else match.group(0),
        text,
    )


PERFORMANCE_ANALYSIS_PROMPT = """你是一名视频表演连续性分析师。请同时理解视频画面与原始声音，按当前分镜的本地时间轴输出严格 JSON，不要输出 Markdown。

目标不是把表演拆成孤立的肌肉动作，而是先判断人物在上下文中的完整表演语义，再用可见证据辅助描述。例如原片是“害羞而克制的微笑”，core_intent 必须保留这一整体语义；低头、回避视线、嘴角上扬只能写入 visible_evidence，不能把它误写成“紧张”“眯眼”或“轻笑”。无法确认时降低 confidence，不要擅自补全。

请遵守：
0. 先枚举当前分镜任意一帧出现过的全部人物，再分析台词和表演。模糊前景、只露头肩/背影/身体局部、被遮挡、静止不动、没有台词的人也必须单独计为人物并写入 performance，绝不能因为不是主角或没有动作而省略。人物槽位按首个可辨认画面中从屏幕左到屏幕右排列为 P1、P2、P3、P4，并在整镜保持不变。
1. dialogue 逐句记录听到的语言内容，并先判断声音来源 source_type：
   - character：画面内人物说话，speaker_slot 必须对应 P1/P2/P3/P4；
   - offscreen：画外人物说话，不绑定画面内人物，speaker_slot=0；
   - bgm_vocal：配乐中的歌声、念白或采样人声，speaker_slot=0；
   - ignore：电视、广播、素材水印音等不应复刻的语言声，speaker_slot=0；
   - uncertain：证据不足，必须人工核对，speaker_slot=0，绝不能猜给当前最显眼或正脸人物。
2. 嘴唇/下颌同步只是一项辅助证据。人物背对镜头、在前景只露背影、嘴被遮挡、远景看不清或台词很短时，即使看不到张嘴，也不能据此排除该人物；必须结合人物所处位置、动作意图、视线对象、问答关系、前后镜连续性及声音方向共同判断。只有嘴部清晰可见且在整句期间明确静止闭合，才可作为反证。mouth_motion 必须如实写“嘴部不可见/被遮挡/远景无法判断”，不得编造口型。
3. BGM 中存在歌词或人声时，不得把分离到的人声自动当作人物对白；没有可靠叙事关系或角色归属时标为 bgm_vocal/uncertain。画外台词标为 offscreen，不能强行让画面中的某个人张嘴。
4. performance 必须覆盖上述每一位人物。即使人物全程静止，也写“保持原位置和遮挡关系”；core_intent 写整体情绪、关系意图与表达方式；visible_evidence 必须比较首帧、中间帧和末帧，写清屏幕左/右、前/后景、人物只露出的身体范围、虚实状态、坐/站、进入或离开画面的方向、手中道具和交接对象；exclude 写容易误生成但原片并不是的表演。visible_evidence 严禁描述服装、鞋袜、发型、毛发、眼镜、首饰、耳饰、头饰或其他穿戴外观，只记录动作与空间证据。
5. 运动轨迹按观众看到的屏幕坐标描述。人物从画面左侧出画就写“向屏幕左侧移动并出画”，不得泛化为“转身离开”，也不得写成右侧；递咖啡、递文件等动作必须保留携带物、递交方向、是否真正完成交接。
6. 时间均以当前分镜 0.00 秒为起点，范围不得超过分镜时长。不分析人物身份，不评价服装与场景，不添加原片不存在的台词、情绪或动作。无法确认时降低 confidence，并使用 uncertain，不要擅自补全。

JSON 结构：
{
  "has_dialogue": true,
  "language": "zh-CN",
  "dialogue": [{"source_type": "character", "speaker_slot": 1, "speaker_confidence": 0.90, "start": 0.20, "end": 1.80, "text": "原台词", "delivery": "原语气、停顿、重音和语速", "mouth_motion": "可见口型证据，或嘴部不可见/被遮挡/无法判断"}],
  "performance": [{"actor_slot": 1, "start": 0.00, "end": 1.80, "core_intent": "完整表演语义", "visible_evidence": "可见的眼神、表情和姿态证据", "exclude": "不要误生成为什么", "confidence": 0.90}],
  "audio_summary": "说话次序、环境声和整体声音节奏的简要说明"
}
"""


CAST_CONTINUITY_PROMPT = """你是一名影视连续性场记。请观察整条原片，并根据脸部、性别呈现、发型、体型、服装、台词关系和前后镜连续性，识别跨分镜反复出现的同一个原片人物。输出严格 JSON，不要输出 Markdown。

规则：
1. shot_index 是从 1 开始的分镜编号；slot 是该分镜内按屏幕从左到右排列的 P 槽位编号；character_id 是跨整条视频稳定不变的原片身份 C1/C2/C3/C4。
2. P 编号只表示当镜位置，绝不表示身份。同一人物换边、远近、朝向、服装遮挡或只露背影后仍使用同一 character_id。
3. 每个已提供的分镜和槽位都必须输出一次。模糊前景、局部入画、背影和被遮挡人物也不能省略。
4. 同镜不同槽位不得使用相同 character_id。证据不足时降低 confidence，并在 evidence 中说明，不得为了左右顺序强行猜测。
5. 不要把新人物图片编号当成 character_id；这里只识别原片内部连续身份。

JSON 结构：
{"characters":[{"character_id":1,"description":"跨镜稳定特征"}],"assignments":[{"shot_index":1,"slot":1,"character_id":2,"confidence":0.92,"evidence":"与后续镜同一男性"}]}
"""


def _clamp_time(value: Any, duration: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return round(max(0.0, min(number, max(duration, 0.0))), 3)


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _json_from_text(text: str, *, analysis_label: str = "表演分析") -> dict[str, Any]:
    clean = text.strip().lstrip("\ufeff")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", clean, re.DOTALL | re.IGNORECASE)
    if fenced:
        clean = fenced.group(1)
    elif clean and clean[0] not in '{["':
        start = clean.find("{")
        end = clean.rfind("}")
        if start >= 0 and end > start:
            clean = clean[start : end + 1]
    try:
        value = json.loads(_without_trailing_commas(clean))
        if isinstance(value, str):
            value = json.loads(_without_trailing_commas(value))
    except json.JSONDecodeError as exc:
        raise AnalysisOutputError(
            f"{analysis_label}接口没有返回可解析的 JSON。"
        ) from exc
    if not isinstance(value, dict):
        raise AnalysisOutputError(f"{analysis_label}结果格式无效，顶层必须是 JSON 对象。")
    return value


def normalize_performance_analysis(
    raw: dict[str, Any],
    *,
    duration: float,
    max_people: int = 4,
) -> dict[str, Any]:
    max_people = max(1, min(int(max_people), 4))
    dialogue: list[dict[str, Any]] = []
    for item in raw.get("dialogue") or []:
        if not isinstance(item, dict):
            continue
        text = _clean_text(item.get("text"), 240)
        if not text:
            continue
        source_type = str(item.get("source_type") or "character").strip().lower()
        if source_type not in {"character", "offscreen", "bgm_vocal", "ignore", "uncertain"}:
            source_type = "uncertain"
        try:
            speaker = int(item.get("speaker_slot") or (1 if source_type == "character" else 0))
        except (TypeError, ValueError):
            speaker = 1 if source_type == "character" else 0
        if source_type == "character":
            speaker = max(1, min(speaker, max_people))
        else:
            speaker = 0
        try:
            speaker_confidence = max(0.0, min(float(item.get("speaker_confidence", 0.5)), 1.0))
        except (TypeError, ValueError):
            speaker_confidence = 0.5
        start = _clamp_time(item.get("start"), duration)
        end = max(start, _clamp_time(item.get("end"), duration))
        dialogue.append(
            {
                "source_type": source_type,
                "speaker_slot": speaker,
                "speaker_confidence": round(speaker_confidence, 3),
                "start": start,
                "end": end,
                "text": text,
                "delivery": _clean_text(item.get("delivery"), 180),
                "mouth_motion": _clean_text(item.get("mouth_motion"), 240),
            }
        )

    performance: list[dict[str, Any]] = []
    for item in raw.get("performance") or []:
        if not isinstance(item, dict):
            continue
        core_intent = _clean_text(item.get("core_intent"), 160)
        if not core_intent:
            continue
        try:
            actor = int(item.get("actor_slot") or 1)
        except (TypeError, ValueError):
            actor = 1
        try:
            confidence = max(0.0, min(float(item.get("confidence", 0.5)), 1.0))
        except (TypeError, ValueError):
            confidence = 0.5
        start = _clamp_time(item.get("start"), duration)
        end = max(start, _clamp_time(item.get("end"), duration))
        performance.append(
            {
                "actor_slot": max(1, min(actor, max_people)),
                "start": start,
                "end": end,
                "core_intent": core_intent,
                "visible_evidence": _clean_text(item.get("visible_evidence"), 240),
                "exclude": _clean_text(item.get("exclude"), 180),
                "confidence": round(confidence, 3),
            }
        )

    return {
        "has_dialogue": any(item["source_type"] in {"character", "offscreen"} for item in dialogue),
        "language": _clean_text(raw.get("language"), 32) or "unknown",
        "dialogue": dialogue,
        "performance": performance,
        "audio_summary": _clean_text(raw.get("audio_summary"), 260),
    }


def normalize_cast_continuity(
    raw: dict[str, Any],
    *,
    shot_slot_counts: dict[int, int],
    max_characters: int = 4,
) -> dict[str, Any]:
    if any(not isinstance(raw.get(key, []), list) for key in ("characters", "assignments")):
        raise AnalysisOutputError("跨镜人物连续性分析结果格式无效，人物与槽位必须是列表。")
    max_characters = max(1, min(int(max_characters), 4))
    characters: list[dict[str, Any]] = []
    seen_characters: set[int] = set()
    for item in raw.get("characters") or []:
        if not isinstance(item, dict):
            continue
        try:
            character_id = int(item.get("character_id") or 0)
        except (TypeError, ValueError):
            continue
        if character_id < 1 or character_id > max_characters or character_id in seen_characters:
            continue
        seen_characters.add(character_id)
        characters.append(
            {
                "character_id": character_id,
                "description": _clean_text(item.get("description"), 220),
            }
        )

    assignments: list[dict[str, Any]] = []
    seen_slots: set[tuple[int, int]] = set()
    per_shot_characters: dict[int, set[int]] = {}
    for item in raw.get("assignments") or []:
        if not isinstance(item, dict):
            continue
        try:
            shot_index = int(item.get("shot_index") or 0)
            slot = int(item.get("slot") or 0)
            character_id = int(item.get("character_id") or 0)
            confidence = max(0.0, min(float(item.get("confidence", 0.5)), 1.0))
        except (TypeError, ValueError):
            continue
        if shot_index not in shot_slot_counts or slot < 1 or slot > shot_slot_counts[shot_index]:
            continue
        key = (shot_index, slot)
        if key in seen_slots or character_id < 1 or character_id > max_characters:
            continue
        used = per_shot_characters.setdefault(shot_index, set())
        if character_id in used:
            continue
        seen_slots.add(key)
        used.add(character_id)
        assignments.append(
            {
                "shot_index": shot_index,
                "slot": slot,
                "character_id": character_id,
                "confidence": round(confidence, 3),
                "evidence": _clean_text(item.get("evidence"), 220),
            }
        )

    expected = {
        (shot_index, slot)
        for shot_index, count in shot_slot_counts.items()
        for slot in range(1, count + 1)
    }
    missing = sorted(expected - seen_slots)
    if missing:
        labels = "、".join(f"分镜{shot:02d}-P{slot}" for shot, slot in missing)
        raise AnalysisOutputError(f"跨镜人物连续性分析缺少槽位：{labels}。请在工作台核对人物对应。")
    return {
        "version": 1,
        "characters": sorted(characters, key=lambda item: item["character_id"]),
        "assignments": sorted(assignments, key=lambda item: (item["shot_index"], item["slot"])),
    }


def _fit_performance_text(value: str, limit: int) -> str:
    """Fit optional acting evidence at a semantic boundary."""
    text = " ".join(str(value or "").strip().split())
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    candidate = text[:limit]
    boundary = max(candidate.rfind(mark) for mark in "。；，！？\n")
    if boundary >= max(12, int(limit * 0.55)):
        candidate = candidate[: boundary + 1]
    return candidate.rstrip("，；、 ")


def _build_ultra_compact_performance_prompt(
    analysis: dict[str, Any],
    *,
    max_chars: int,
    generate_new_voice: bool,
) -> str:
    """Budget a real-person shot prompt without ever cutting dialogue text."""
    manual_dialogue = analysis.get("manual_dialogue_text")
    manual_performance = analysis.get("manual_performance_text")
    has_manual_dialogue = isinstance(manual_dialogue, str)
    source_items = [item for item in analysis.get("dialogue") or [] if isinstance(item, dict)]
    binding_items = (
        [item for item in source_items if item.get("source_reviewed") is True]
        if has_manual_dialogue
        else source_items
    )
    character_items = [
        item for item in binding_items
        if str(item.get("source_type") or "character") in {"character", "offscreen"}
    ]
    effective_has_dialogue = bool(
        manual_dialogue.strip()
        if has_manual_dialogue
        else character_items or analysis.get("has_dialogue")
    )
    required = ["台词与表演时间轴（最高优先级）："]
    if generate_new_voice and effective_has_dialogue:
        required.append(
            "声音：按新人物生成全新音色，不复用原声；逐字按标注时段完整说完，保持语速停顿，口型同步。"
        )
    if has_manual_dialogue:
        if manual_dialogue.strip():
            required.append(f"人工台词：{manual_dialogue.strip()}")
        else:
            required.append("本镜无台词，所有人物闭嘴且不发声。")
    else:
        for item in source_items:
            source_type = str(item.get("source_type") or "character")
            start = float(item.get("start") or 0.0)
            end = float(item.get("end") or start)
            text = str(item.get("text") or "").strip()
            if source_type == "character":
                required.append(
                    f"{start:.2f}–{end:.2f}秒 P{int(item.get('speaker_slot') or 1)}说：“{text}”。"
                )
            elif source_type == "offscreen":
                required.append(f"{start:.2f}–{end:.2f}秒 画外声：“{text}”；画内人物闭嘴。")
            elif source_type == "bgm_vocal":
                required.append(f"{start:.2f}–{end:.2f}秒 BGM人声“{text}”不生成。")
            elif source_type in {"ignore", "uncertain"}:
                required.append(f"{start:.2f}–{end:.2f}秒 “{text}”不生成、不驱动口型。")
    locks: list[str] = []
    for item in binding_items:
        source_type = str(item.get("source_type") or "character")
        text = str(item.get("text") or "").strip()
        if source_type == "character":
            locks.append(f"“{text}”=P{int(item.get('speaker_slot') or 1)}")
        elif source_type == "offscreen":
            locks.append(f"“{text}”=画外声")
    if locks:
        required.append("说话人锁：" + "；".join(locks) + "；背影、遮挡或嘴部不可见也不得转移台词。")
    if not effective_has_dialogue and not any("无台词" in line for line in required):
        required.append("本镜无台词，所有人物闭嘴且不发声。")
    required_text = "\n".join(required)
    if len(required_text) > max_chars:
        raise WorkflowError(
            "本镜台词原文、说话人和时间轴本身已超过可用提示词预算；"
            "请只精简重复的语气说明，不要删除台词正文。"
        )

    optional: list[str] = []
    if isinstance(manual_performance, str):
        if manual_performance.strip():
            optional.append("人工表演：" + _sanitize_white_model_motion_text(manual_performance))
    else:
        for item in analysis.get("performance") or []:
            if not isinstance(item, dict):
                continue
            start = float(item.get("start") or 0.0)
            end = float(item.get("end") or start)
            slot = int(item.get("actor_slot") or 1)
            intent = _sanitize_white_model_motion_text(str(item.get("core_intent") or ""))
            evidence = _sanitize_white_model_motion_text(str(item.get("visible_evidence") or ""))
            action = intent or evidence
            if intent and evidence and evidence not in intent:
                action = f"{intent}；{evidence}"
            if action:
                optional.append(f"{start:.2f}–{end:.2f}秒 P{slot}：{action}。")

    text = required_text
    for line in optional:
        remaining = max_chars - len(text) - 1
        if remaining < 18:
            break
        fitted = _fit_performance_text(line, remaining)
        if fitted:
            text += "\n" + fitted
    return text


def build_performance_prompt(
    analysis: dict[str, Any],
    *,
    max_chars: int = 1100,
    generate_new_voice: bool = False,
    speaker_roles: list[str] | None = None,
    compact: bool = False,
    ultra_compact: bool = False,
) -> str:
    if ultra_compact:
        return _build_ultra_compact_performance_prompt(
            analysis,
            max_chars=max_chars,
            generate_new_voice=generate_new_voice,
        )
    manual_dialogue = analysis.get("manual_dialogue_text")
    manual_performance = analysis.get("manual_performance_text")
    has_manual_dialogue = isinstance(manual_dialogue, str)
    has_manual_performance = isinstance(manual_performance, str)
    source_items = [item for item in analysis.get("dialogue") or [] if isinstance(item, dict)]
    binding_items = (
        [item for item in source_items if item.get("source_reviewed") is True]
        if has_manual_dialogue
        else source_items
    )
    effective_has_dialogue = (
        any(str(item.get("source_type") or "character") in {"character", "offscreen"} for item in binding_items)
        if binding_items
        else bool(manual_dialogue.strip()) if has_manual_dialogue else bool(analysis.get("has_dialogue"))
    )
    lines = [
        "按本镜时间轴复刻台词与表演；整体表演语义优先于局部动作词："
        if compact
        else "按以下当前分镜本地时间轴复刻原片台词与表演；整体表演语义优先于局部动作词："
    ]
    if generate_new_voice and effective_has_dialogue:
        if compact:
            lines.append(
                "台词文字、说话人和时序必须按人工校订内容执行；Seedance依据新人物生成全新角色音色，"
                "禁止复制或保留原片声纹。@视频1完全静音；新声音须按标注的语速、停顿、重音、语调和情绪生成，"
                "不得拖慢、截断句尾或改写节奏，口型逐音节同步。"
            )
        else:
            lines.append(
                "台词文字和时序必须复刻，但声音必须由Seedance依据新人物形象重新生成；"
                "严禁模仿、复制或保留原片人物的声纹、音色、嗓音年龄及独特发声特征。"
                "同一新人物跨分镜保持同一个新的角色音色。"
                "@视频1是完全静音的动作与运镜参考，不包含原片对白、歌声、BGM、环境音或任何原声波形。"
                "新声音必须按以下人工校订/结构化时间轴的发声起止、语速、停顿长度、重音、语调和情绪生成，"
                "不得自行放慢、拖长、截断句尾或改写节奏。"
                "只生成新人物的台词、人声呼吸和必要口腔声；不得复刻或混入原片对白、BGM、环境音及其他原片声音。"
            )

    def append_source_locks() -> None:
        for item in binding_items:
            source_type = str(item.get("source_type") or "character")
            if source_type == "character":
                slot = int(item.get("speaker_slot") or 1)
                lines.append(
                    f"人工来源锁定：‘{item.get('text', '')}’只能由P{slot}说；该锁定高于台词文字框中可能残留的旧P编号。"
                    "即使P槽位背对、嘴部不可见或被遮挡，也不能转移给其他人物。"
                )
            elif source_type == "bgm_vocal":
                lines.append(f"来源锁定：‘{item.get('text', '')}’是BGM人声，不生成该声音且不分配给任何P槽位。")
            elif source_type == "ignore":
                lines.append(f"来源锁定：‘{item.get('text', '')}’必须忽略，不生成且不分配给任何P槽位。")
            elif source_type == "uncertain":
                lines.append(f"来源锁定：‘{item.get('text', '')}’尚未确认，不能猜给画面内人物。")
            elif source_type == "offscreen":
                lines.append(f"来源锁定：‘{item.get('text', '')}’是画外声，画面内人物不能代说。")

    def semantic_actor_label(slot: int) -> str:
        for performance_item in analysis.get("performance") or []:
            if int(performance_item.get("actor_slot") or 0) != slot:
                continue
            intent = str(performance_item.get("core_intent") or "").strip()
            evidence = str(performance_item.get("visible_evidence") or "").strip()
            if intent and evidence:
                return f"执行“{intent}”表演（可见位置与动作：{evidence}）的角色"
            if evidence:
                return f"画面中“{evidence}”的角色"
            if intent:
                return f"执行“{intent}”表演的角色"
        if speaker_roles and 0 < slot <= len(speaker_roles) and speaker_roles[slot - 1].strip():
            return f"{speaker_roles[slot - 1].strip()}"
        return f"表演槽位P{slot}"
    if has_manual_dialogue:
        if manual_dialogue.strip():
            lines.append("人工校订台词文字与时序（覆盖自动提取文字；下方人工来源锁定拥有更高优先级）：")
            lines.append(manual_dialogue.strip())
            lines.append("严格按人工校订内容执行说话人、文字、语气、时序与口型，台词区间外闭嘴。")
        elif effective_has_dialogue or not compact:
            lines.append("人工已确认本分镜没有可听见台词，不得生成任何人物说话声或说话口型。")
    else:
        for item in analysis.get("dialogue") or []:
            source_type = str(item.get("source_type") or "character")
            delivery = f"；说法：{item['delivery']}" if item.get("delivery") else ""
            mouth_motion = f"；可见口型证据：{item['mouth_motion']}" if item.get("mouth_motion") else ""
            if source_type == "character":
                slot = int(item["speaker_slot"])
                role = semantic_actor_label(slot)
                lines.append(
                    f"{float(item['start']):.2f}–{float(item['end']):.2f}秒，只能由P{slot}（{role}）准确说："
                    f"“{item['text']}”{delivery}{mouth_motion}；若嘴部可见则逐音节同步；若背对、遮挡或嘴部不可见，"
                    "仍保持P槽位归属，绝不能把台词转移给正脸或更显眼的人。"
                )
            elif source_type == "offscreen":
                lines.append(
                    f"{float(item['start']):.2f}–{float(item['end']):.2f}秒，画外人物说：“{item['text']}”{delivery}；"
                    "画面内所有人物保持闭嘴和原反应，不得把画外声转给画面内人物。"
                )
            elif source_type == "bgm_vocal":
                lines.append(f"{float(item['start']):.2f}–{float(item['end']):.2f}秒的“{item['text']}”属于BGM人声：不要生成、不要驱动任何人物口型。")
            elif source_type == "ignore":
                lines.append(f"{float(item['start']):.2f}–{float(item['end']):.2f}秒的“{item['text']}”为忽略声音：不要生成、不要驱动口型。")
            else:
                lines.append(f"{float(item['start']):.2f}–{float(item['end']):.2f}秒的“{item['text']}”来源未确认：不得猜给任何画面内人物，也不得驱动口型。")
    append_source_locks()
    if has_manual_performance:
        if manual_performance.strip():
            lines.append("人工校订表演（最高优先级，覆盖自动提取结果）：")
            lines.append(
                _sanitize_white_model_motion_text(manual_performance)
                if compact
                else manual_performance.strip()
            )
        else:
            lines.append("人工已确认本分镜没有需要额外补充的表演神色约束。")
    else:
        for item in analysis.get("performance") or []:
            evidence_text = str(item.get("visible_evidence") or "")
            exclude_text = str(item.get("exclude") or "")
            if compact:
                evidence_text = _sanitize_white_model_motion_text(evidence_text)
                exclude_text = _sanitize_white_model_motion_text(exclude_text)
            evidence = f"；外在证据：{evidence_text}" if evidence_text else ""
            exclude = f"；排除误读：{exclude_text}" if exclude_text else ""
            slot = int(item["actor_slot"])
            lines.append(
                f"{float(item['start']):.2f}–{float(item['end']):.2f}秒，{semantic_actor_label(slot)}的核心表演语义为"
                f"“{item['core_intent']}”{evidence}{exclude}。"
            )
    if analysis.get("audio_summary") and not has_manual_dialogue and not compact:
        if generate_new_voice:
            lines.append(f"只复刻声音事件的种类、顺序和时间：{analysis['audio_summary']}；不得复用原人物声音。")
        else:
            lines.append(f"声音连续性：{analysis['audio_summary']}。")
    speaking_slots = sorted({
        int(item.get("speaker_slot") or 0)
        for item in binding_items
        if str(item.get("source_type") or "character") == "character" and int(item.get("speaker_slot") or 0) > 0
    })
    if has_manual_dialogue and not binding_items:
        speaking_slots = sorted({int(value) for value in re.findall(r"\bP([1-4])\b", manual_dialogue, re.IGNORECASE)})
    if effective_has_dialogue:
        slot_text = "、".join(f"P{slot}" for slot in speaking_slots) or "已标注说话人"
        if compact:
            lines.append(
                f"说话人锁定为{slot_text}及标注画外声；其他人物闭嘴。嘴部不可见、背对或遮挡不改变台词归属。"
            )
        else:
            lines.append(
                f"说话人硬锁定为{slot_text}及明确标注的画外声。任一时刻只能由标注来源发声；其他人物不得串词、抢话或同步张嘴。"
                "嘴部不可见、背对镜头或被遮挡不能改变说话人归属；只保留其他人物原片中的反应表演。"
            )
    if not effective_has_dialogue:
        if generate_new_voice:
            lines.append(
                "@视频1是完全静音的动作与运镜参考；本镜无台词，所有人物闭嘴，"
                "不得生成任何人物说话声、喘息拟声或说话口型。"
                if compact
                else "本分镜没有可听见台词，不得生成任何人物说话声、喘息拟声或说话口型。"
            )
        else:
            lines.append("本分镜没有可听见台词，不得自行添加对白或口型。")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"


_WHITE_MODEL_APPEARANCE_RE = re.compile(
    r"(?:穿着|身穿|身着|衣着|衣服|服装|服饰|装束|造型|上衣|衬衫|西装|领带|领结|婚纱|礼服|"
    r"裙子|长裙|短裙|裤子|长裤|短裤|外套|夹克|毛衣|卫衣|T恤|背心|鞋履|鞋子|靴子|袜子|"
    r"帽子|头纱|面纱|手套|肩带|蕾丝|珍珠|首饰|配饰|耳饰|耳环|项链|手链|戒指|发饰|发簪|"
    r"胸针|腰带|皮带|发型|长发|短发|刘海|头发|胡须|眼镜)"
)
_WHITE_MODEL_MOTION_ANCHOR_RE = re.compile(
    r"(?:位于|处于|站在|坐在|蹲在|躺在|跪在|从画面|从屏幕|向画面|向屏幕|面向|朝向|背对|"
    r"进入|离开|移向|移动|走向|跑向|出画|入画|抬头|低头|转身|转头|递给|接过|交给|拿着|"
    r"手持|握住|放下|扶住|视线|目光|嘴部|头部|身体|手部|脚部|保持|表情|姿态|动作|轨迹|"
    r"遮挡|虚化|清晰)"
)
_WHITE_MODEL_SLOT_PREFIX_RE = re.compile(
    r"^\s*((?:\[[^\]]+\]\s*)?(?:P[1-4]\s*[：:]\s*)?)",
    re.IGNORECASE,
)


def sanitize_motion_evidence(value: Any) -> str:
    """Remove identity/wardrobe appearance while retaining usable motion evidence."""
    clean = " ".join(str(value or "").split())
    if not clean:
        return ""
    if not _WHITE_MODEL_APPEARANCE_RE.search(clean):
        return clean
    kept: list[str] = []
    for raw_segment in re.split(r"[，,；;。！？\n]+", clean):
        segment = raw_segment.strip()
        if not segment:
            continue
        prefix_match = _WHITE_MODEL_SLOT_PREFIX_RE.match(segment)
        prefix = prefix_match.group(1) if prefix_match else ""
        body = segment[len(prefix) :].strip()
        appearance = _WHITE_MODEL_APPEARANCE_RE.search(body)
        if not appearance:
            kept.append(segment)
            continue
        motion_matches = list(_WHITE_MODEL_MOTION_ANCHOR_RE.finditer(body))
        if not motion_matches:
            continue
        first_motion = motion_matches[0]
        if appearance.start() <= first_motion.start():
            body = body[first_motion.start() :].strip()
        else:
            body = body[: appearance.start()].rstrip(" ：:")
        if body:
            kept.append(f"{prefix}{body}".strip())
    return "；".join(kept)


_sanitize_white_model_motion_text = sanitize_motion_evidence


def build_white_model_performance_prompt(
    analysis: dict[str, Any],
    *,
    max_chars: int = 1050,
) -> str:
    """Build silent mouth/acting constraints for the white-model motion pass."""
    lines = [
        "白模阶段完全静音，只复刻台词对应的嘴部时序与人物表演，不生成任何声音。",
        "说话人归属是硬约束：背对镜头、嘴部被遮挡或看不到张嘴，均不能把台词转移给正脸或更显眼的人。",
    ]
    dialogue_items = [item for item in analysis.get("dialogue") or [] if isinstance(item, dict)]
    manual_dialogue = analysis.get("manual_dialogue_text")
    if isinstance(manual_dialogue, str) and manual_dialogue.strip():
        lines.extend(["人工校订台词时序（最高优先级，只用于口型与说话人归属）：", manual_dialogue.strip()])
    binding_items = (
        [item for item in dialogue_items if item.get("source_reviewed") is True]
        if isinstance(manual_dialogue, str)
        else dialogue_items
    )
    for item in binding_items:
        source_type = str(item.get("source_type") or "character")
        start = float(item.get("start") or 0)
        end = float(item.get("end") or start)
        text = str(item.get("text") or "")
        if source_type == "character":
            slot = int(item.get("speaker_slot") or 1)
            lines.append(
                f"{start:.2f}–{end:.2f}秒仅P{slot}是说话人（‘{text}’）：嘴部可见时复刻开合与下颌节奏；"
                "嘴部不可见时保持原姿态，不得让其他P槽位代替张嘴。"
            )
        elif source_type == "offscreen":
            lines.append(f"{start:.2f}–{end:.2f}秒为画外声（‘{text}’）：画面内所有人物不得出现说话口型。")
        else:
            lines.append(f"{start:.2f}–{end:.2f}秒的‘{text}’不是已确认的画内人物对白：任何人物都不得被其驱动口型。")
    manual_performance = analysis.get("manual_performance_text")
    if isinstance(manual_performance, str) and manual_performance.strip():
        sanitized_manual = _sanitize_white_model_motion_text(manual_performance)
        if sanitized_manual:
            lines.extend(["人工校订表演与轨迹（最高优先级）：", sanitized_manual])
    else:
        for item in analysis.get("performance") or []:
            if not isinstance(item, dict):
                continue
            slot = int(item.get("actor_slot") or 1)
            intent = _sanitize_white_model_motion_text(item.get("core_intent"))
            evidence = _sanitize_white_model_motion_text(item.get("visible_evidence"))
            exclude = _sanitize_white_model_motion_text(item.get("exclude"))
            constraints = [value for value in (intent, evidence) if value]
            if not constraints:
                constraints = ["保持原片中的位置、姿态与动作时序"]
            exclude_text = f"；避免误读：{exclude}" if exclude else ""
            lines.append(
                f"{float(item.get('start') or 0):.2f}–{float(item.get('end') or 0):.2f}秒，P{slot}："
                f"只执行位置、姿态、表情、动作与轨迹约束：{'；'.join(constraints)}{exclude_text}。"
            )
    lines.append("白模表演只控制动作与口型；人偶始终保持纯白长袖长裤工作服、手套与平底鞋，不继承原人物的穿戴外观。")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"


def _response_text(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        raise AnalysisOutputError("分析接口返回的结果结构无效。")
    if payload.get("status") in {"incomplete", "failed", "cancelled"}:
        raise AnalysisOutputError("分析服务未返回完整结果，已保留完成的分析。")
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    texts: list[str] = []
    for output in payload.get("output") or []:
        if not isinstance(output, dict):
            continue
        if output.get("type") not in {None, "message"} or output.get("role") not in {None, "assistant"}:
            continue
        for content in output.get("content") or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") not in {None, "text", "output_text"}:
                continue
            value = content.get("text") or content.get("output_text")
            if isinstance(value, str) and value.strip():
                texts.append(value)
    if texts:
        return "".join(texts)
    raise AnalysisOutputError("表演分析接口返回成功，但结果中没有文本内容。")


class ArkPerformanceAnalyzer:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_ARK_BASE_URL,
        model: str = DEFAULT_PERFORMANCE_MODEL,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        if not self.api_key:
            raise WorkflowError(".env 中缺少 ARK_API_KEY。")
        self.base_url = base_url.rstrip("/")
        self.model = model.strip() or DEFAULT_PERFORMANCE_MODEL
        self.session = session or requests.Session()

    def analyze(self, video_url: str, *, duration: float, max_people: int = 4) -> dict[str, Any]:
        body = {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_video", "video_url": video_url, "fps": 5},
                        {
                            "type": "input_text",
                            "text": f"当前分镜时长为 {duration:.3f} 秒，最多 {max_people} 位人物。\n\n{PERFORMANCE_ANALYSIS_PROMPT}",
                        },
                    ],
                }
            ],
            "thinking": {"type": "disabled"},
        }
        try:
            response = self.session.post(
                f"{self.base_url}/responses",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=body,
                timeout=(30, 300),
            )
        except requests.RequestException as exc:
            raise ArkConnectionError("POST", f"表演分析网络连接失败：{exc}", 1) from exc
        if response.status_code >= 400:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            error = detail.get("error") if isinstance(detail, dict) else None
            message = str(error.get("message") or detail) if isinstance(error, dict) else str(detail)
            code = str(error.get("code") or "") if isinstance(error, dict) else ""
            raise ArkAPIError(response.status_code, message, code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise WorkflowError("表演分析接口返回了非 JSON 响应。") from exc
        raw = _json_from_text(_response_text(payload))
        return normalize_performance_analysis(raw, duration=duration, max_people=max_people)

    def analyze_cast_continuity(
        self,
        video_url: str,
        *,
        shot_slot_counts: dict[int, int],
        shot_ranges: list[dict[str, Any]],
        max_characters: int = 4,
        retry_instruction: str = "",
        on_response: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        timeline = "\n".join(
            f"分镜{int(item['index']):02d}：全片 {float(item['start']):.3f}–{float(item['end']):.3f} 秒，"
            f"共有 {int(shot_slot_counts.get(int(item['index']), 0))} 个槽位。"
            for item in shot_ranges
            if int(item.get("index") or 0) in shot_slot_counts
        )
        body = {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_video", "video_url": video_url, "fps": 3},
                        {
                            "type": "input_text",
                            "text": (
                                f"最多 {max_characters} 个跨镜人物。分镜时间表：\n{timeline}\n\n"
                                f"{CAST_CONTINUITY_PROMPT}"
                                + (f"\n\n纠错重试要求：{retry_instruction}" if retry_instruction else "")
                            ),
                        },
                    ],
                }
            ],
            "thinking": {"type": "disabled"},
        }
        try:
            response = self.session.post(
                f"{self.base_url}/responses",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=body,
                timeout=(30, 300),
            )
        except requests.RequestException as exc:
            raise ArkConnectionError("POST", f"跨镜人物连续性分析网络连接失败：{exc}", 1) from exc
        if response.status_code >= 400:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            error = detail.get("error") if isinstance(detail, dict) else None
            message = str(error.get("message") or detail) if isinstance(error, dict) else str(detail)
            code = str(error.get("code") or "") if isinstance(error, dict) else ""
            raise ArkAPIError(response.status_code, message, code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise AnalysisOutputError("跨镜人物连续性分析接口返回了非 JSON 响应。") from exc
        if on_response:
            # Persist only received output, never headers, input URLs or credentials.
            snapshot: dict[str, Any] = {}
            if isinstance(payload, dict):
                snapshot["status"] = str(payload.get("status") or "")
                try:
                    snapshot["output_text"] = _response_text({**payload, "status": "completed"})
                except AnalysisOutputError:
                    snapshot["output_text"] = ""
            on_response(snapshot)
        raw = _json_from_text(
            _response_text(payload),
            analysis_label="跨镜人物连续性分析",
        )
        return normalize_cast_continuity(
            raw,
            shot_slot_counts=shot_slot_counts,
            max_characters=max_characters,
        )
