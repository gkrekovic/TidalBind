from pathlib import Path
import os
import shutil
import sys

import pytest

from tidalbind.bridge import BridgeError, GhciError, TidalBridge, boot_commands


@pytest.fixture
def fake(tmp_path):
    transcript = tmp_path / 'commands.txt'
    return [sys.executable, str(Path(__file__).with_name('fake_ghci.py')), str(transcript)], transcript


def test_boot_blocks_and_pipe_shutdown(fake, tmp_path):
    command, transcript = fake
    boot = tmp_path / 'Boot Tidal.hs'
    boot.write_text('-- comment\n:set -XOverloadedStrings\n:{\nlet x = 1\n    y = 2\n:}\n')
    with TidalBridge(boot, command=command) as bridge:
        bridge.send_block('d1 $ s "bd"\n  # gain 0.5')
        bridge.send_immediate('resetCycles')
        bridge.send_block(':{\nlet z = 1\n:}')
    sent = transcript.read_text()
    assert ':set -XOverloadedStrings' in sent
    assert ':{\nlet x = 1\n    y = 2\n:}' in sent
    assert ':{\nd1 $ s "bd"\n  # gain 0.5\n:}' in sent
    assert ':{\n:{' not in sent
    assert 'hush\n' in sent
    assert ':quit\n' in sent
    assert bridge.process.poll() == 0
    bridge.stop()  # Idempotent.


def test_error_acknowledged_and_session_remains_usable(fake):
    command, _ = fake
    with TidalBridge(command=command) as bridge:
        with pytest.raises(GhciError, match='Not in scope'):
            bridge.send_block('broken_code')
        bridge.send_immediate('okay')


def test_timeout_terminates_session(fake):
    command, _ = fake
    with TidalBridge(command=command, timeout=0.15) as bridge:
        with pytest.raises(BridgeError, match='acknowledge'):
            bridge.send_immediate('stall')
        assert bridge.process.poll() is not None
        with pytest.raises(BridgeError, match='not running'):
            bridge.send_immediate('anything')


def test_unexpected_exit(fake):
    command, _ = fake
    with TidalBridge(command=command) as bridge:
        with pytest.raises(BridgeError, match='exited unexpectedly'):
            bridge.send_immediate('explode')


@pytest.mark.parametrize('text', [':{\nx', ':}', ':{\n:{\nx\n:}\n:}'])
def test_malformed_boot_rejected_before_start(text):
    with pytest.raises(BridgeError):
        boot_commands(text)


def test_missing_boot_does_not_spawn(tmp_path):
    with pytest.raises(FileNotFoundError):
        TidalBridge(tmp_path / 'missing.hs', command=['nonexistent-ghci'])


def test_no_demonstration_sound_on_boot(fake, tmp_path):
    command, transcript = fake
    boot = tmp_path / 'boot.hs'
    boot.write_text('let hush = pure ()')
    with TidalBridge(boot, command=command):
        assert 'd1' not in transcript.read_text()


def test_real_ghci_transport(tmp_path):
    ghci = os.environ.get('TIDALBIND_TEST_GHCI') or shutil.which('ghci')
    if not ghci:
        pytest.skip('GHCi is not installed; subprocess protocol tested separately')
    boot = tmp_path / 'Boot Tidal.hs'
    boot.write_text('''-- Plain Haskell transport fixture, no Tidal or audio.
:{
let twice x = x * 2
    hush = putStrLn "quiet"
:}
''')
    output = []
    with TidalBridge(boot, command=[ghci, '-ignore-dot-ghci'], output=output.append) as bridge:
        bridge.send_block('print (twice 21)')
        with pytest.raises(GhciError):
            bridge.send_block('thisDoesNotExist')
        bridge.send_block('putStrLn "still alive"')
    assert any(line.strip() == '42' for line in output)
    assert any(line.strip() == 'still alive' for line in output)
    assert any(line.strip() == 'quiet' for line in output)


def test_boot_multiline_comments_and_quoted_comment_markers():
    commands = boot_commands('''{- header
and {- nested -} comment -}
let x = "-- not a comment"
{- another -} let y = 2 -- trailing comment
''')
    assert commands == ['let x = "-- not a comment"', 'let y = 2']


def test_comment_headers_before_ghci_commands_and_blocks(fake):
    command, transcript = fake
    with TidalBridge(command=command) as bridge:
        bridge.send_block('-- heading\n:{\nlet x = 1\n:}')
        bridge.send_block('{- header -}\n:set -XOverloadedStrings')
    sent = transcript.read_text()
    assert ':{\n:{' not in sent
    assert ':set -XOverloadedStrings\n' in sent
