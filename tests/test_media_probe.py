"""media_probe 软失败门禁：ffmpeg/ffprobe/PIL 缺失或路径无效时不抛、返回 None/False。

不依赖真实 ffmpeg/ffprobe/PIL（有则实探、无则软失败）——只锁「绝不抛异常」这条护栏，
保证上传链路即使在无媒体工具的环境也只是拿不到元数据，而非 500。
"""
from src.companion import media_probe as mp


def test_probe_video_missing_file_returns_none():
    assert mp.probe_video("/no/such/file.mp4") is None


def test_probe_video_empty_path_returns_none():
    assert mp.probe_video("") is None


def test_make_thumbnail_missing_source_returns_false(tmp_path):
    out = tmp_path / "t.jpg"
    assert mp.make_video_thumbnail("/no/such/file.mp4", str(out)) is False
    assert not out.exists()


def test_probe_image_missing_file_returns_none():
    # PIL 缺失 → None；PIL 存在但文件不存在 → 打开失败也 None。
    assert mp.probe_image("/no/such/file.jpg") is None


def test_availability_helpers_are_bool():
    assert isinstance(mp.ffmpeg_available(), bool)
    assert isinstance(mp.ffprobe_available(), bool)
