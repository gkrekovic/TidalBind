"""Subprocess protocol fixture; intentionally not a Haskell interpreter."""
import json
from pathlib import Path
import sys
import time

transcript = Path(sys.argv[1])
with transcript.open('a', encoding='utf-8') as log:
    for line in sys.stdin:
        log.write(line)
        log.flush()
        code = line.strip()
        if code == ':quit':
            break
        if code.startswith('putStrLn "TIDALBIND_DONE_'):
            print(json.loads(code[len('putStrLn '):]), flush=True)
        elif code == 'broken_code':
            print('<interactive>:1:1: error: Not in scope: broken_code', flush=True)
        elif code == 'stall':
            time.sleep(30)
        elif code == 'explode':
            sys.exit(9)
