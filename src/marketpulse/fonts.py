"""Native Chinese fonts where present, with a bundled OFL fallback."""

import os
from pathlib import Path


def font_path(bold=False):
    explicit = os.environ.get("WECHAT_PULSE_FONT_BOLD" if bold else "WECHAT_PULSE_FONT")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ValueError("指定的字体不存在")
        return str(path)
    candidates = [
        Path("/System/Library/Fonts/STHeiti Medium.ttc" if bold else "/System/Library/Fonts/STHeiti Light.ttc"),
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / ("msyhbd.ttc" if bold else "msyh.ttc"),
        Path(__file__).parent / "assets/NotoSansCJKsc-Regular.otf",
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    raise RuntimeError("安装包缺少中文字体；请重新安装完整发行版")


def emoji_font():
    for path in [
        Path("/System/Library/Fonts/Apple Color Emoji.ttc"),
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/seguiemj.ttf",
    ]:
        if path.is_file():
            return str(path)
    return None
