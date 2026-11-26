import os
import subprocess
import sys

import tokenwell


def test_old_module_name_forwards_to_tokenwell_cli(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path)}
    result = subprocess.run([sys.executable, "-m", "claude_unlimited", "--version"],
                            capture_output=True, text=True, env=env, check=True)
    assert result.stdout.strip() == tokenwell.__version__
