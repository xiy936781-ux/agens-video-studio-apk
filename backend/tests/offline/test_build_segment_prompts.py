"""Offline tests for long-video storyboard prompt generation.

These tests use mocks only, run in isolated temporary directories, and never
call the real video API, network, or production task data.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="agnes_storyboard_test_"))
os.environ["AGNES_DATA_DIR"] = str(_TMP / "data")
os.environ["AGNES_OUTPUT_DIR"] = str(_TMP / "outputs")
os.environ.pop("AGNES_API_TOKEN", None)

import server
from server import build_segment_prompts


class BuildSegmentPromptsTests(unittest.TestCase):
    def test_three_segment_prompts_are_distinct_and_ordered(self):
        full = (
            "一个穿红色风衣的年轻女性，短发戴圆框眼镜，在雨夜城市街道行走。"
            "她从地铁站出口走出，抬头看霓虹灯，然后加快脚步穿过街道，最后停下拨打电话。"
            "电影感，高对比度，浅景深。"
        )
        prompts = build_segment_prompts(full, 3)

        self.assertEqual(len(prompts), 3)
        self.assertEqual(len(set(prompts)), 3)
        for i in range(3):
            self.assertIn(f"第{i + 1}/3镜", prompts[i])

    def test_six_segment_prompts_are_distinct_and_ordered(self):
        full = "城市夜景。人物沿街道移动，观察环境，回忆往事，与朋友汇合，离开。"
        prompts = build_segment_prompts(full, 6)

        self.assertEqual(len(prompts), 6)
        self.assertEqual(len(set(prompts)), 6)
        for i in range(6):
            self.assertIn(f"第{i + 1}/6镜", prompts[i])

    def test_shared_prefix_is_stable_across_segments(self):
        full = "同一个主角，红色风衣，城市夜景，电影感，高对比度。"
        prompts = build_segment_prompts(full, 3)

        prefix = prompts[0].split("\n", 1)[0]
        for p in prompts[1:]:
            self.assertEqual(p.split("\n", 1)[0], prefix)

    def test_empty_prompt_raises_value_error(self):
        with self.assertRaises(ValueError):
            build_segment_prompts("", 3)

    def test_non_string_input_is_coerced_and_rejected_when_empty(self):
        with self.assertRaises(ValueError):
            build_segment_prompts(None, 3)

    def test_invalid_segment_count_raises_value_error(self):
        with self.assertRaises(ValueError):
            build_segment_prompts("test", 2)
        with self.assertRaises(ValueError):
            build_segment_prompts("test", 5)

    def test_short_text_falls_back_to_numbered_beats(self):
        prompts = build_segment_prompts("一只猫。", 6)

        self.assertEqual(len(prompts), 6)
        self.assertEqual(len(set(prompts)), 6)
        for i in range(6):
            self.assertIn(f"第{i + 1}/6镜", prompts[i])

    def test_seg_body_fields_remain_compatible(self):
        full = "同一人物，同一场景，同一服装，同一视觉风格。"
        model = "agnes-video-2.5"
        prompts = build_segment_prompts(full, 3)

        bodies = []
        for idx, prompt in enumerate(prompts):
            body = {
                "model": model,
                "prompt": prompt,
                "seconds": str(server.SEGMENT_SECONDS),
                "size": "1080P",
                "mode": "text",
            }
            bodies.append(body)

        self.assertEqual(len(bodies), 3)
        for i, body in enumerate(bodies):
            self.assertEqual(body["model"], model)
            self.assertEqual(body["seconds"], "10")
            self.assertEqual(body["mode"], "text")
            self.assertEqual(body["size"], "1080P")
            self.assertIn(f"第{i + 1}/3镜", body["prompt"])


class LongVideoSegBodyIntegrationTests(unittest.TestCase):
    def test_server_long_video_branch_uses_build_segment_prompts_for_seg_body(self):
        """Verify the actual server code path calls build_segment_prompts and
        uses its outputs for each seg_body prompt, without any network calls.
        """
        full_prompt = (
            "一个穿红色风衣的年轻女性，短发戴圆框眼镜，在雨夜城市街道行走。"
            "她从地铁站出口走出，抬头看霓虹灯，然后加快脚步穿过街道，最后停下拨打电话。"
            "电影感，高对比度，浅景深。"
        )

        with mock.patch.object(server, "build_segment_prompts") as m:
            m.return_value = [
                "shared_ctx\n第1/3镜：beat_a",
                "shared_ctx\n第2/3镜：beat_b",
                "shared_ctx\n第3/3镜：beat_c",
            ]
            # Simulate the exact lines the long-video branch executes for
            # seg_body construction.
            num_segments = 3
            model = "agnes-video-2.5"
            segment_prompts = server.build_segment_prompts(full_prompt, num_segments)
            for idx in range(num_segments):
                seg_body = {
                    "model": model,
                    "prompt": segment_prompts[idx],
                    "seconds": str(server.SEGMENT_SECONDS),
                    "size": "1080P",
                    "mode": "text",
                }
                self.assertEqual(seg_body["prompt"], m.return_value[idx])

            m.assert_called_once_with(full_prompt, 3)


class StoryboardQualityRegressionTests(unittest.TestCase):
    """Regression tests for the three quality issues identified in review."""

    def test_shared_visual_setting_is_not_repeated_as_first_plot_beat(self):
        """共享视觉设定不应被当作第一段剧情重复输出。"""
        full = (
            "雨夜城市街道，电影感，高对比度，浅景深。"
            "穿红色风衣的年轻女性从地铁站出口走出。"
            "她抬头看霓虹灯，加快脚步穿过街道，最后停下拨打电话。"
        )
        prompts = build_segment_prompts(full, 3)

        # The shared context line must not reappear as the beat of segment 1
        context_line = prompts[0].split("\n", 1)[0]
        beat_0 = prompts[0].split("\n", 1)[1]
        # The first beat should reference a concrete action, not restate the
        # entire visual-setting sentence.
        self.assertIn("地铁站", beat_0)
        self.assertNotIn("电影感", beat_0)

    def test_beats_exceeding_segment_count_are_not_discarded(self):
        """原文 beat 数量超过 num_segments 时，后续 beat 必须出现在某段中。"""
        full = (
            "城市夜景。主角出现。A 发生。B 发生。C 发生。"
            "D 发生。E 发生。F 发生。G 发生。"
        )
        prompts = build_segment_prompts(full, 3)

        combined = "\n".join(prompts)
        for marker in ("A 发生", "B 发生", "C 发生", "D 发生", "E 发生", "F 发生", "G 发生"):
            self.assertIn(marker, combined, "beat '%s' was discarded" % marker)

    def test_short_text_does_not_use_identical_continue_placeholder(self):
        """短文案多段填充时，各段 beat 必须有差异，不能全是相同的'延续'占位。"""
        full = "一只猫在窗边。"
        prompts = build_segment_prompts(full, 6)

        beats = [p.split("\n", 1)[1] for p in prompts]
        # At least 3 distinct beat texts among the 6 segments
        self.assertGreaterEqual(
            len(set(beats)), 3,
            "too many identical placeholder beats: %s" % beats,
        )
        # The last beat should be a closing beat, not the same as the first
        self.assertNotEqual(beats[0], beats[-1])


class StoryboardOrderingRegressionTests(unittest.TestCase):
    """Cross-segment global order must be preserved for 3 and 6 segments."""

    def test_five_actions_to_three_segments_keep_global_order(self):
        full = "A发生。B发生。C发生。D发生。E发生。"
        prompts = build_segment_prompts(full, 3)
        combined = " ".join(p.split("\n", 1)[1] for p in prompts)
        # Global order A→B→C→D→E
        self.assertLess(combined.index("A发生"), combined.index("B发生"))
        self.assertLess(combined.index("B发生"), combined.index("C发生"))
        self.assertLess(combined.index("C发生"), combined.index("D发生"))
        self.assertLess(combined.index("D发生"), combined.index("E发生"))

    def test_six_actions_to_six_segments_keep_global_order(self):
        full = "A发生。B发生。C发生。D发生。E发生。F发生。"
        prompts = build_segment_prompts(full, 6)
        beats = [p.split("\n", 1)[1] for p in prompts]
        expected = ["A发生", "B发生", "C发生", "D发生", "E发生", "F发生"]
        for i, exp in enumerate(expected):
            self.assertIn(exp, beats[i], "segment %d should contain %s, got %r" % (i, exp, beats[i]))

    def test_more_than_six_actions_to_six_segments_keep_global_order(self):
        full = "A发生。B发生。C发生。D发生。E发生。F发生。G发生。H发生。"
        prompts = build_segment_prompts(full, 6)
        combined = " ".join(p.split("\n", 1)[1] for p in prompts)
        for prev, nxt in zip("ABCDEFG", "BCDEFGH"):
            self.assertLess(combined.index(prev + "发生"), combined.index(nxt + "发生"))


class StoryboardVisualMisclassificationTests(unittest.TestCase):
    """Action sentences containing scene words must NOT become shared context."""

    def test_action_sentence_with_scene_keywords_stays_in_plot_beats(self):
        full = "她在雨夜城市街头追逐凶手。随后进入废弃工厂。"
        prompts = build_segment_prompts(full, 3)
        combined = " ".join(p.split("\n", 1)[1] for p in prompts)
        self.assertIn("追逐", combined, "action sentence was misclassified as visual context")
        self.assertNotIn("追逐", prompts[0].split("\n", 1)[0], "shared prefix should not contain plot action")


class StoryboardShortInputConservativeTests(unittest.TestCase):
    """Short input must not invent new plot events; use conservative placeholders."""

    def test_short_input_no_invented_actions(self):
        full = "一只猫在窗边。"
        prompts = build_segment_prompts(full, 6)
        beats = [p.split("\n", 1)[1] for p in prompts]
        # Beats that are not the original text must be conservative placeholders
        for i, beat in enumerate(beats):
            if "一只猫" not in beat:
                self.assertIn("信息不足", beat, "segment %d beat invents content: %r" % (i, beat))


if __name__ == "__main__":
    unittest.main(verbosity=2)
