"""浏览器端纯函数（交错切分 / 偏好 / 发送键）用 node --test 跑 widget.test.mjs；没装 node 就跳过。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
HERE = Path(__file__).parent
STATIC = HERE.parent / "src" / "claude_bridge" / "static"


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_widget_pure_functions():
    r = subprocess.run([NODE, "--test", str(HERE / "widget.test.mjs")], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(not NODE, reason="node not installed")
@pytest.mark.parametrize("name", ["bridge-client.js", "bridge-widget.js"])
def test_modules_parse(name):
    r = subprocess.run([NODE, "--check", str(STATIC / name)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
