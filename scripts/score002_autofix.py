from score002_autofix_pre import *
try:
    from score002_autofix_clean import *
except Exception as exc:
    print(f"clean transformer stopped; continuing post repair: {exc!r}")
from score002_autofix_post import *
from score002_autofix_post2 import *
from score002_commit_now import *
