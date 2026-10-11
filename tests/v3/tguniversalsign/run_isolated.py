"""在临时目录执行隔离测试，避免 pytest 导入插件源码包。"""
import os
import shutil
import tempfile
from pathlib import Path
import pytest

os.environ['TG_SIGN_TEST_ROOT'] = str(Path(__file__).resolve().parents[3] / 'plugins.v3' / 'tguniversalsign')
with tempfile.TemporaryDirectory(prefix='tg-sign-tests-') as folder:
    test = Path(folder) / 'test_plugin.py'
    shutil.copyfile(Path(__file__).with_name('test_plugin.py'), test)
    raise SystemExit(pytest.main([str(test), '-q', '--tb=short']))
