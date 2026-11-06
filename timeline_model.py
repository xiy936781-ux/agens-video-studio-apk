"""AgnesVideo 时间线编辑器 —— 阶段 0：数据模型与校验

纯 Python 模块，不依赖 server.py 或任何外部包。
字段结构：job["edit"]["clips"]
"""

from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 与 server.py SEGMENT_SECONDS 保持一致（父任务缺 segment_seconds 字段时的兜底）
_DEFAULT_SEGMENT_SECONDS = 10

_VALID_CLIP_FIELDS = {
    "seg_index", "order", "enabled", "start", "end", "duration", "local_file",
}

# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _is_finite_number(v: Any) -> bool:
    """必须是 int/float（bool 排除），且有限。"""
    if isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    import math
    return math.isfinite(v)


def _validate_finite_number(value: Any, label: str, clip_index: int) -> float:
    if isinstance(value, bool):
        raise ValueError(
            f"clip[{clip_index}].{label}: 必须为有限数值，不接受布尔值"
        )
    if not isinstance(value, (int, float)):
        raise ValueError(
            f"clip[{clip_index}].{label}: 必须为数值，实际类型 {type(value).__name__}"
        )
    import math
    if not math.isfinite(value):
        raise ValueError(
            f"clip[{clip_index}].{label}: 必须为有限数值，不接受 NaN/Infinity"
        )
    return float(value)


def _validate_bool(value: Any, label: str, clip_index: int) -> bool:
    if not isinstance(value, bool):
        raise ValueError(
            f"clip[{clip_index}].{label}: 必须为布尔值 True/False，不接受 {type(value).__name__}"
        )
    return value


def _validate_int(value: Any, label: str, clip_index: int, min_val: int = None,
                  max_val: int = None) -> int:
    if isinstance(value, bool):
        raise ValueError(
            f"clip[{clip_index}].{label}: 必须为整数，不接受布尔值"
        )
    if not isinstance(value, int):
        raise ValueError(
            f"clip[{clip_index}].{label}: 必须为整数，实际类型 {type(value).__name__}"
        )
    if min_val is not None and value < min_val:
        raise ValueError(
            f"clip[{clip_index}].{label}: 值 {value} 小于最小值 {min_val}"
        )
    if max_val is not None and value > max_val:
        raise ValueError(
            f"clip[{clip_index}].{label}: 值 {value} 大于最大值 {max_val}"
        )
    return value


def _segment_duration(parent: Dict[str, Any]) -> float:
    """返回源片段时长（秒）。

    优先级：
    1. parent["segment_seconds"]（long 任务在创建时已持久化）
    2. _DEFAULT_SEGMENT_SECONDS（=10s，与 server.py SEGMENT_SECONDS 一致）
    """
    seg_s = parent.get("segment_seconds")
    if seg_s is not None and _is_finite_number(seg_s) and seg_s > 0:
        return float(seg_s)
    return float(_DEFAULT_SEGMENT_SECONDS)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def default_timeline_model(parent: Dict[str, Any]) -> Dict[str, Any]:
    """根据 long 父任务，构建默认时间线 edit 模型。

    返回的 edit dict 包含 clips 列表，每个 clip 对应一个源分段：
      seg_index: 与 segments[i]["index"] 一致
      order:     时间线排列顺序（默认等于 seg_index，即 0,1,2,...）
      enabled:   True
      start:     0
      end:       源片段时长（由 segment_seconds 决定）
      duration:  end - start
      local_file: None（暂时不读写本地文件路径）

    若父任务非 long=True 或没有有效 segments，抛出 ValueError。
    """
    if not isinstance(parent, dict):
        raise ValueError("父任务必须为 dict")
    if not parent.get("long", False):
        raise ValueError("父任务必须是 long=True（长视频分段任务）")

    segments = parent.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("父任务必须有有效的 segments 列表")

    # 有效分段：每个 segment 须是 dict 且有 "index" 键（int）
    valid_segments = []
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            continue
        idx = seg.get("index")
        if isinstance(idx, int) and not isinstance(idx, bool):
            valid_segments.append((idx, seg))
    if not valid_segments:
        raise ValueError("segments 中没有有效分段（缺少 index 字段）")

    seg_duration = _segment_duration(parent)

    clips = []
    for order, (idx, _seg) in enumerate(valid_segments):
        clips.append({
            "seg_index": idx,
            "order": order,
            "enabled": True,
            "start": 0,
            "end": seg_duration,
            "duration": seg_duration,
            "local_file": None,
        })

    return {"clips": clips}


def validate_timeline(parent: Dict[str, Any], edit: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """对时间线 edit 模型进行纯数据校验。

    参数：
      parent: long=True 父任务 dict（必须含有效 segments）
      edit:   待校验的 edit dict；为 None 时调用 default_timeline_model(parent) 生成
              默认模型再校验（用于验证"旧任务没有 edit 字段时的兼容性"）

    校验规则：
      - 父任务必须 long=True，且 segments 非空，每个有效 segment 须有 "index"
      - 每个 clip：
          seg_index   int，不得重复，且必须对应 parent segments 中存在的 index
          order       int ≥ 0，全局唯一，且必须构成 [0, 1, ..., n-1] 的连续排列
          enabled     必须为 Python bool（True/False）
          start/end   有限数值；0 <= start < end <= 源片段时长
          duration    必须等于 end - start（容差 1e-6），否则抛错
          local_file  只读字段，不参与校验逻辑，接受任意类型（包括 None）
      - 总时长 = sum(clip["duration"] for clip where enabled==True)

    返回：
      校验通过的 edit dict（含 clips 列表，已规范化）

    校验失败时抛出 ValueError，消息包含具体错误描述。
    """
    # 父任务基本校验
    if not isinstance(parent, dict):
        raise ValueError("父任务必须为 dict")
    if not parent.get("long", False):
        raise ValueError("父任务必须是 long=True")

    segments = parent.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("父任务必须有有效的 segments 列表")

    # 建立有效 seg_index 集合（与 default_timeline_model 保持一致）
    valid_seg_indexes: set = set()
    for seg in segments:
        if isinstance(seg, dict) and isinstance(seg.get("index"), int) \
                and not isinstance(seg.get("index"), bool):
            valid_seg_indexes.add(seg["index"])
    if not valid_seg_indexes:
        raise ValueError("segments 中没有有效分段（缺少 index 字段）")

    seg_duration = _segment_duration(parent)

    # 获取 edit
    if edit is None:
        edit = default_timeline_model(parent)

    if not isinstance(edit, dict):
        raise ValueError("edit 必须为 dict")
    clips = edit.get("clips")
    if not isinstance(clips, list):
        raise ValueError("edit.clips 必须为 list")

    n = len(clips)
    seen_seg_indexes: set = set()
    seen_orders: set = set()

    total_duration = 0.0

    for i, clip in enumerate(clips):
        if not isinstance(clip, dict):
            raise ValueError(f"clips[{i}]: 每个 clip 必须为 dict，实际 {type(clip).__name__}")

        # 检测多余字段（告警式：不抛错但记录，避免静默丢失数据）
        extra = set(clip.keys()) - _VALID_CLIP_FIELDS
        if extra:
            raise ValueError(
                f"clips[{i}]: 含未识别字段 {sorted(extra)}"
            )

        # --- seg_index ---
        si = clip.get("seg_index")
        if isinstance(si, bool) or not isinstance(si, int):
            raise ValueError(f"clips[{i}].seg_index: 必须为整数，实际 {type(si).__name__}")
        if si < 0:
            raise ValueError(f"clips[{i}].seg_index: 必须 >= 0，实际 {si}")
        if si in seen_seg_indexes:
            raise ValueError(f"clips[{i}].seg_index={si}: 重复索引")
        if si not in valid_seg_indexes:
            raise ValueError(
                f"clips[{i}].seg_index={si}: 对应分段在父任务 segments 中不存在"
            )
        seen_seg_indexes.add(si)

        # --- order ---
        o = clip.get("order")
        if isinstance(o, bool) or not isinstance(o, int):
            raise ValueError(f"clips[{i}].order: 必须为整数，实际 {type(o).__name__}")
        if o < 0:
            raise ValueError(f"clips[{i}].order: 必须 >= 0，实际 {o}")
        if o in seen_orders:
            raise ValueError(f"clips[{i}].order={o}: 重复排列顺序")
        seen_orders.add(o)

        # --- enabled ---
        enabled = _validate_bool(clip.get("enabled"), "enabled", i)

        # --- start / end ---
        start = _validate_finite_number(clip.get("start"), "start", i)
        end   = _validate_finite_number(clip.get("end"),   "end",   i)

        if start < 0:
            raise ValueError(f"clips[{i}].start: 必须 >= 0，实际 {start}")
        if start >= end:
            raise ValueError(
                f"clips[{i}]: 必须满足 start < end（start={start}, end={end}）"
            )
        if end > seg_duration + 1e-6:
            raise ValueError(
                f"clips[{i}].end: 超出源片段时长 {seg_duration}s（实际 end={end}）"
            )

        # --- duration ---
        expected_dur = end - start
        dur = clip.get("duration")
        if dur is not None:
            dur_f = _validate_finite_number(dur, "duration", i)
            if abs(dur_f - expected_dur) > 1e-6:
                raise ValueError(
                    f"clips[{i}].duration: 必须等于 end-start（"
                    f"期望 {expected_dur:.6f}，实际 {dur_f:.6f}）"
                )

        # local_file：仅校验键存在（值可为 None/str 等任意），不做路径解析
        # （local_file 在 clips 中必须显式包含；若缺失则补 None）
        if "local_file" not in clip:
            clip["local_file"] = None

        if enabled:
            total_duration += expected_dur

    # --- order 连续排列校验 ---
    if n:
        expected_orders = set(range(n))
        missing = expected_orders - seen_orders
        if missing:
            raise ValueError(
                f"order 必须构成 [0, {n-1}] 的连续排列，缺少 {sorted(missing)}"
            )

    # 规范化：将 start/end/duration 统一为 float，enabled 保持 bool
    normalized_clips: List[Dict[str, Any]] = []
    for clip in clips:
        nc = dict(clip)
        nc["start"]    = float(clip["start"])
        nc["end"]      = float(clip["end"])
        nc["duration"] = float(clip["end"] - clip["start"])
        normalized_clips.append(nc)

    return {"clips": normalized_clips, "total_duration": total_duration}


def total_timeline_duration(edit: Dict[str, Any]) -> float:
    """计算时间线总时长（秒）：所有 enabled=True 片段的 duration 之和。

    若 edit 缺少 clips 或 clips 为空，返回 0.0。
    """
    if not isinstance(edit, dict):
        return 0.0
    clips = edit.get("clips", [])
    total = 0.0
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        if clip.get("enabled", False):
            total += float(clip.get("duration", 0.0))
    return total
