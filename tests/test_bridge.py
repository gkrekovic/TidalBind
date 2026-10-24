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


@pytest.fixture
def real_ghci():
    ghci = os.environ.get('TIDALBIND_TEST_GHCI') or shutil.which('ghci')
    if not ghci:
        pytest.skip('GHCi is not installed; subprocess protocol tested separately')
    return [ghci, '-ignore-dot-ghci']


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
        assert not any(line.lstrip().startswith('d1 ') for line in transcript.read_text().splitlines())


def test_real_ghci_transport(tmp_path, real_ghci):
    boot = tmp_path / 'Boot Tidal.hs'
    boot.write_text('''-- Plain Haskell transport fixture, no Tidal or audio.
:{
let twice x = x * 2
    hush = putStrLn "quiet"
:}
''')
    output = []
    with TidalBridge(boot, command=real_ghci, output=output.append) as bridge:
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


def test_boot_timeout_identifies_file_line_command_and_output(fake, tmp_path):
    command, _ = fake
    boot = tmp_path / 'Boot Tidal.hs'
    boot.write_text('-- heading\n\nstall_after_output\n')
    with pytest.raises(BridgeError) as raised:
        TidalBridge(boot, command=command, timeout=0.25)
    message = str(raised.value)
    assert str(boot) in message
    assert 'line 3' in message
    assert 'Command:\nstall_after_output' in message
    assert 'Recent GHCi output:\nWaiting for prerequisite' in message


def test_interrupt_while_waiting_terminates_child(fake, monkeypatch):
    command, _ = fake
    with TidalBridge(command=command) as bridge:
        def interrupted(**kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(bridge._lines, 'get', interrupted)
        with pytest.raises(KeyboardInterrupt):
            bridge.send_immediate('okay')
        assert bridge.process.poll() is not None


def test_real_ghci_multiline_mode_and_restart_after_interruption(tmp_path, real_ghci):
    boot = tmp_path / 'Boot Tidal.hs'
    boot.write_text('''-- Common GHCi option in editor boot files.
:set +m
let twice x = x * 2
let hush = putStrLn "quiet"
''')
    for attempt in range(3):
        output = []
        bridge = TidalBridge(boot, command=real_ghci, timeout=3, output=output.append)
        try:
            with bridge:
                bridge.send_immediate('let answer = twice 21')
                bridge.send_block('print answer')
                if attempt == 0:
                    raise KeyboardInterrupt
        except KeyboardInterrupt:
            assert attempt == 0
        assert bridge.process.poll() == 0
        assert output.count('42') == 1
        assert output.count('quiet') == 1


@pytest.mark.parametrize('taskkill_available', [True, False])
def test_windows_abort_targets_only_own_process_tree(monkeypatch, taskkill_available):
    from types import SimpleNamespace
    from unittest.mock import Mock
    import tidalbind.bridge as module

    process = Mock(pid=4321)
    process.poll.return_value = None
    bridge = object.__new__(TidalBridge)
    bridge.process = process
    bridge._closed = False
    def taskkill(*args, **kwargs):
        if not taskkill_available:
            raise FileNotFoundError('taskkill')
        process.poll.return_value = 1

    run = Mock(side_effect=taskkill)
    # Replace this module's OS binding, not the global os.name used by pathlib.
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(module.subprocess, 'run', run)
    bridge._abort()
    assert run.call_args.args[0] == ['taskkill', '/PID', '4321', '/T', '/F']
    process.wait.assert_called_once_with(timeout=3)
    assert process.terminate.called == (not taskkill_available)
    assert bridge._closed
