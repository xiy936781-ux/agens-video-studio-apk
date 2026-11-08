"""AgnesVideo 时间线编辑器 阶段 0 离线测试

覆盖场景：
  1. 默认模型构建
  2. 正常校验
  3. 重复 seg_index
  4. 重复 / 不连续 order
  5. 越界裁剪（start < 0, start >= end, end > 源时长）
  6. 非法数值（NaN / Infinity / 布尔值冒充数字）
  7. 非法布尔值 enabled（0/1 等 int 冒充 bool）
  8. 总时长计算
  9. 旧任务兼容（无 edit 字段）
  10. JSON 序列化
"""

import json
import math
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import timeline_model as tm


# ---------------------------------------------------------------------------
# 辅助：构造合法父任务
# ---------------------------------------------------------------------------

def make_parent(num_segments=3, segment_seconds=10, **extra):
    segs = []
    for i in range(num_segments):
        segs.append({
            "index": i, "video_id": None, "status": "pending_creation",
            "progress": 0, "url": None, "error": None,
        })
    p = {
        "id": "lv_test", "long": True, "segment_seconds": segment_seconds,
        "num_segments": num_segments, "total_seconds": num_segments * segment_seconds,
        "segments": segs, "status": "queued", "progress": 0,
        "merge_status": "pending", "merged_url": None,
    }
    p.update(extra)
    return p


def make_clip(seg_index=0, order=0, enabled=True, start=0.0, end=10.0,
              duration=10.0, local_file=None):
    return {
        "seg_index": seg_index, "order": order, "enabled": enabled,
        "start": start, "end": end, "duration": duration, "local_file": local_file,
    }


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

class TestDefaultModel(unittest.TestCase):

    def test_default_model_3_segments(self):
        parent = make_parent(num_segments=3)
        m = tm.default_timeline_model(parent)
        self.assertEqual(len(m["clips"]), 3)
        for i, c in enumerate(m["clips"]):
            self.assertEqual(c["seg_index"], i)
            self.assertEqual(c["order"], i)
            self.assertIs(c["enabled"], True)
            self.assertEqual(c["start"], 0)
            self.assertEqual(c["end"], 10)
            self.assertEqual(c["duration"], 10)
            self.assertIsNone(c["local_file"])

    def test_default_model_custom_segment_seconds(self):
        parent = make_parent(num_segments=4, segment_seconds=8)
        m = tm.default_timeline_model(parent)
        self.assertEqual(len(m["clips"]), 4)
        for c in m["clips"]:
            self.assertEqual(c["end"], 8)
            self.assertEqual(c["duration"], 8)

    def test_default_model_rejects_short_task(self):
        parent = make_parent()
        parent["long"] = False
        with self.assertRaises(ValueError):
            tm.default_timeline_model(parent)

    def test_default_model_rejects_non_dict(self):
        with self.assertRaises(ValueError):
            tm.default_timeline_model([1, 2, 3])

    def test_default_model_rejects_empty_segments(self):
        parent = make_parent()
        parent["segments"] = []
        with self.assertRaises(ValueError):
            tm.default_timeline_model(parent)


class TestValidateNormal(unittest.TestCase):

    def test_valid_model_passes(self):
        parent = make_parent(num_segments=3)
        edit = {
            "clips": [
                make_clip(0, 0, True, 0.0, 10.0, 10.0),
                make_clip(1, 1, True, 2.5, 9.0, 6.5),
                make_clip(2, 2, False, 0.0, 5.0, 5.0),
            ]
        }
        result = tm.validate_timeline(parent, edit)
        self.assertEqual(result["total_duration"], 10.0 + 6.5)

    def test_duration_consistency(self):
        parent = make_parent(num_segments=1)
        # duration 与 end-start 不一致
        clip = make_clip(0, 0, True, 1.0, 5.0, 4.0)  # end-start=4.0，duration=4.0 实际一致
        clip["duration"] = 3.0  # 制造不一致
        edit = {"clips": [clip]}
        with self.assertRaises(ValueError):
            tm.validate_timeline(parent, edit)

    def test_float_start_end(self):
        parent = make_parent(num_segments=2)
        edit = {"clips": [
            make_clip(0, 0, True, 0.5, 10.0, 9.5),
            make_clip(1, 1, True, 0.0, 3.0, 3.0),
        ]}
        result = tm.validate_timeline(parent, edit)
        self.assertAlmostEqual(result["total_duration"], 12.5)


class TestDuplicateSegIndex(unittest.TestCase):

    def test_duplicate_seg_index_rejected(self):
        parent = make_parent(num_segments=3)
        edit = {"clips": [
            make_clip(0, 0, True, 0.0, 10.0, 10.0),
            make_clip(0, 1, True, 0.0, 10.0, 10.0),  # 重复 seg_index=0
            make_clip(2, 2, True, 0.0, 10.0, 10.0),
        ]}
        with self.assertRaises(ValueError) as ctx:
            tm.validate_timeline(parent, edit)
        self.assertIn("重复", str(ctx.exception))


class TestOrderUniqueness(unittest.TestCase):

    def test_duplicate_order_rejected(self):
        parent = make_parent(num_segments=3)
        edit = {"clips": [
            make_clip(0, 0, True, 0.0, 10.0, 10.0),
            make_clip(1, 0, True, 0.0, 10.0, 10.0),  # 重复 order=0
            make_clip(2, 2, True, 0.0, 10.0, 10.0),
        ]}
        with self.assertRaises(ValueError) as ctx:
            tm.validate_timeline(parent, edit)
        self.assertIn("重复", str(ctx.exception))

    def test_non_consecutive_order_rejected(self):
        parent = make_parent(num_segments=3)
        # order 缺失 1：[0, 1, 3]
        edit = {"clips": [
            make_clip(0, 0, True, 0.0, 10.0, 10.0),
            make_clip(1, 1, True, 0.0, 10.0, 10.0),
            make_clip(2, 3, True, 0.0, 10.0, 10.0),  # order=3，缺 2
        ]}
        with self.assertRaises(ValueError):
            tm.validate_timeline(parent, edit)

    def test_valid_permutation_order_passes(self):
        parent = make_parent(num_segments=3)
        # 顺序重排但每个都有效且连续：[1, 0, 2]
        edit = {"clips": [
            make_clip(0, 1, True, 0.0, 10.0, 10.0),
            make_clip(1, 0, True, 0.0, 10.0, 10.0),
            make_clip(2, 2, True, 0.0, 10.0, 10.0),
        ]}
        result = tm.validate_timeline(parent, edit)
        self.assertEqual(result["total_duration"], 30.0)


class TestBounds(unittest.TestCase):

    def test_negative_start_rejected(self):
        parent = make_parent(num_segments=1)
        edit = {"clips": [make_clip(0, 0, True, -1.0, 5.0, 6.0)]}
        with self.assertRaises(ValueError):
            tm.validate_timeline(parent, edit)

    def test_start_equal_end_rejected(self):
        parent = make_parent(num_segments=1)
        edit = {"clips": [make_clip(0, 0, True, 5.0, 5.0, 0.0)]}
        with self.assertRaises(ValueError):
            tm.validate_timeline(parent, edit)

    def test_end_exceeds_source_duration_rejected(self):
        parent = make_parent(num_segments=1, segment_seconds=10)
        edit = {"clips": [make_clip(0, 0, True, 0.0, 11.0, 11.0)]}
        with self.assertRaises(ValueError):
            tm.validate_timeline(parent, edit)


class TestInvalidNumbers(unittest.TestCase):

    def test_nan_start_rejected(self):
        parent = make_parent(num_segments=1)
        edit = {"clips": [make_clip(0, 0, True, float("nan"), 5.0, 5.0)]}
        with self.assertRaises(ValueError):
            tm.validate_timeline(parent, edit)

    def test_infinity_end_rejected(self):
        parent = make_parent(num_segments=1)
        edit = {"clips": [make_clip(0, 0, True, 0.0, float("inf"), 0.0)]}
        with self.assertRaises(ValueError):
            tm.validate_timeline(parent, edit)

    def test_bool_as_number_rejected(self):
        parent = make_parent(num_segments=1)
        edit = {"clips": [make_clip(0, 0, True, True, 5.0, 5.0)]}  # start=True 冒充数字
        with self.assertRaises(ValueError):
            tm.validate_timeline(parent, edit)

    def test_string_number_rejected(self):
        parent = make_parent(num_segments=1)
        edit = {"clips": [make_clip(0, 0, True, "0.0", 5.0, 5.0)]}
        with self.assertRaises(ValueError):
            tm.validate_timeline(parent, edit)


class TestInvalidBool(unittest.TestCase):

    def test_int_one_as_enabled_rejected(self):
        parent = make_parent(num_segments=1)
        edit = {"clips": [make_clip(0, 0, 1, 0.0, 10.0, 10.0)]}  # enabled=1 (int)
        with self.assertRaises(ValueError):
            tm.validate_timeline(parent, edit)

    def test_int_zero_as_enabled_rejected(self):
        parent = make_parent(num_segments=1)
        edit = {"clips": [make_clip(0, 0, 0, 0.0, 10.0, 10.0)]}  # enabled=0 (int)
        with self.assertRaises(ValueError):
            tm.validate_timeline(parent, edit)

    def test_string_bool_rejected(self):
        parent = make_parent(num_segments=1)
        edit = {"clips": [make_clip(0, 0, "true", 0.0, 10.0, 10.0)]}
        with self.assertRaises(ValueError):
            tm.validate_timeline(parent, edit)


class TestTotalDuration(unittest.TestCase):

    def test_mixed_enabled_total(self):
        parent = make_parent(num_segments=4)
        edit = {"clips": [
            make_clip(0, 0, True, 0.0, 10.0, 10.0),
            make_clip(1, 1, False, 0.0, 10.0, 10.0),  # 不计入
            make_clip(2, 2, True, 5.0, 10.0, 5.0),
            make_clip(3, 3, True, 0.0, 10.0, 10.0),
        ]}
        result = tm.validate_timeline(parent, edit)
        self.assertAlmostEqual(result["total_duration"], 25.0)

    def test_total_timeline_duration_function(self):
        edit = {"clips": [
            make_clip(0, 0, True, 0.0, 10.0, 10.0),
            make_clip(1, 1, False, 0.0, 10.0, 10.0),
        ]}
        self.assertAlmostEqual(tm.total_timeline_duration(edit), 10.0)

    def test_total_timeline_duration_empty(self):
        self.assertEqual(tm.total_timeline_duration({"clips": []}), 0.0)
        self.assertEqual(tm.total_timeline_duration({}), 0.0)


class TestLegacyCompat(unittest.TestCase):

    def test_old_task_no_edit_field_compat(self):
        """旧任务没有 edit 字段时，validate_timeline(parent, None) 应正常生成默认模型。"""
        parent = make_parent(num_segments=3)
        # 模拟旧任务：无 edit 键
        result = tm.validate_timeline(parent, None)
        self.assertIn("clips", result)
        self.assertEqual(len(result["clips"]), 3)
        self.assertAlmostEqual(result["total_duration"], 30.0)

    def test_old_task_json_serializable(self):
        """校验结果可 JSON 序列化。"""
        parent = make_parent(num_segments=2)
        result = tm.validate_timeline(parent, None)
        s = json.dumps(result, ensure_ascii=False)
        parsed = json.loads(s)
        self.assertIn("clips", parsed)
        self.assertIn("total_duration", parsed)


class TestJsonSerialization(unittest.TestCase):

    def test_full_json_roundtrip(self):
        parent = make_parent(num_segments=3)
        edit = {"clips": [
            make_clip(0, 0, True, 0.0, 10.0, 10.0, local_file="/data/clip0.mp4"),
            make_clip(1, 1, True, 1.5, 8.5, 7.0),
            make_clip(2, 2, False, 0.0, 10.0, 10.0),
        ]}
        result = tm.validate_timeline(parent, edit)
        s = json.dumps(result, ensure_ascii=False)
        parsed = json.loads(s)
        self.assertEqual(len(parsed["clips"]), 3)
        self.assertAlmostEqual(parsed["total_duration"], 17.0)
        # local_file 保留
        self.assertEqual(parsed["clips"][0]["local_file"], "/data/clip0.mp4")
        self.assertIsNone(parsed["clips"][1]["local_file"])


if __name__ == "__main__":
    unittest.main()
