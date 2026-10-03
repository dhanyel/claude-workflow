# In-process tests must see the same clean environment as CI and as the subprocesses
# (helpers.SWITCHES): the developer's real tokens made a test pass locally and fail in CI.
import os

from .helpers import SWITCHES

for _k in SWITCHES:
    os.environ.pop(_k, None)
