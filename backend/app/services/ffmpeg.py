"""Makes sure an executable named `ffmpeg` is on PATH (Whisper needs it to decode audio/video)."""

import os
import shutil
import sys


def ensure_ffmpeg(bin_dir: str = "ffmpeg_bin") -> str:
    existing = shutil.which("ffmpeg")
    if existing:
        return existing

    import imageio_ffmpeg

    source = imageio_ffmpeg.get_ffmpeg_exe()
    os.makedirs(bin_dir, exist_ok=True)
    target = os.path.join(bin_dir, "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg")
    if not os.path.exists(target):
        shutil.copy(source, target)
        os.chmod(target, 0o755)
    os.environ["PATH"] += os.pathsep + os.path.abspath(bin_dir)

    found = shutil.which("ffmpeg")
    if not found:
        raise RuntimeError("Could not make ffmpeg available on PATH.")
    return found
