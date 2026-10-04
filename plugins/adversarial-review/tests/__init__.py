# In-process tests see the same clean environment as CI and as the subprocesses (helpers.SWITCHES).
import os
from .helpers import SWITCHES
for _k in SWITCHES:
    os.environ.pop(_k, None)
