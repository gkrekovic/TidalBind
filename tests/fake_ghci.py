"""Subprocess protocol fixture; intentionally not a Haskell interpreter."""
import json
from pathlib import Path
import sys
import time

transcript = Path(sys.argv[1])
noisy_ack = False


def acknowledge(signal):
    global noisy_ack
    if noisy_ack:
        # Simulate concurrent Tidal output at byte/character boundaries. With
        # the old text token this reproduces the user's broken startup log.
        message = 'Connected to SuperDirt.\n'
        for position in range(max(len(message), len(signal))):
            if position < len(message):
                sys.stdout.write(message[position])
            if position < len(signal):
                sys.stdout.write(signal[position])
            sys.stdout.flush()
        noisy_ack = False
    else:
        sys.stdout.write(signal)
        sys.stdout.flush()


with transcript.open('a', encoding='utf-8') as log:
    for line in sys.stdin:
        log.write(line)
        log.flush()
        code = line.strip()
        if code == ':quit':
            break
        if code.startswith('putStrLn "TIDALBIND_DONE_'):
            acknowledge(json.loads(code[len('putStrLn '):]) + '\n')
        elif code.startswith('TidalBindTransport.hPutChar '):
            acknowledge('\x06')
        elif code == 'interleaved_startTidal':
            print('[TidalCycles version 1.7.10]', flush=True)
            print('Listening for external controls on 127.0.0.1:6010', flush=True)
            noisy_ack = True
        elif code == 'broken_code':
            print('<interactive>:1:1: error: Not in scope: broken_code', flush=True)
        elif code == 'broken_without_newline':
            print('<interactive>:1:1: error: Not in scope: missingValue', end='', flush=True)
        elif code == 'stall':
            time.sleep(30)
        elif code == 'stall_after_output':
            print('Waiting for prerequisite', flush=True)
            time.sleep(30)
        elif code == 'explode':
            sys.exit(9)
