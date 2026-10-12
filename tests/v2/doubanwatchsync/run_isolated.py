"""从独立目录运行 pytest，避免 pytest 在隔离夹具之前导入宿主插件包。"""
import os
import shutil
import tempfile
from pathlib import Path
import pytest

root = Path(__file__).resolve().parents[3] / 'plugins.v2' / 'doubanwatchsync'
os.environ['WATCHSYNC_TEST_ROOT'] = str(root)
with tempfile.TemporaryDirectory(prefix='watchsync-tests-') as folder:
    target = Path(folder) / 'test_management.py'
    shutil.copyfile(Path(__file__).with_name('test_management.py'), target)
    raise SystemExit(pytest.main([str(target), '-q', '--tb=short']))
