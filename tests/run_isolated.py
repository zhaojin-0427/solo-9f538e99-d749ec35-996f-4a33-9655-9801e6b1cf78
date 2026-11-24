"""隔离模式测试入口：python3 -I tests/run_isolated.py

-I 会忽略 PYTHONPATH，故在启动后、导入被测包之前显式加入本地依赖目录，
其余隔离语义（不读用户站点、不读 PYTHON* 环境变量）保持不变。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, ".pydeps"))
sys.path.insert(0, ROOT)

import unittest  # noqa: E402

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(ROOT, "tests"), top_level_dir=ROOT)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
