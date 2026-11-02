import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_no_secret_shapes.py"


def _run(target):
    return subprocess.run([sys.executable, str(SCRIPT), str(target)], capture_output=True, text=True)


def test_repo_is_free_of_secret_shapes():
    r = _run(ROOT)
    assert r.returncode == 0, r.stdout + r.stderr


def test_scanner_detects_secret_shape(tmp_path):
    f = tmp_path / "leak.txt"
    f.write_text("bot " + "123456789" + ":" + "AAHdqTcvCH1vGWJx" + "fSeofSAs0K5PALDsawq" + "\n")
    r = _run(tmp_path)
    assert r.returncode == 1
    assert "leak.txt:1" in r.stdout
