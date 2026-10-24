import json
from pathlib import Path
import subprocess
import sys

import pytest

from tidalbind.cli import main

ROOT = Path(__file__).resolve().parents[1]


def test_validate_example(capsys):
    assert main([str(ROOT / 'examples/performance.tidal'), '--validate']) == 0
    assert '5 blocks' in capsys.readouterr().out


def test_replay_without_hardware_or_ghci(capsys):
    result = main([str(ROOT / 'examples/performance.tidal'), '--dry-run', '--replay',
                   str(ROOT / 'examples/replay.jsonl'), '--missing-values', 'defer'])
    assert result == 0
    output = capsys.readouterr().out
    assert 'note "67"' in output
    assert '# lpf 9000' in output
    assert '[command] resetCycles' in output
    assert 'End of score' in output


def test_bad_replay_error_location(tmp_path, capsys):
    replay = tmp_path / 'bad.jsonl'
    replay.write_text('{"type":"note_on","note":999}')
    assert main([str(ROOT / 'examples/performance.tidal'), '--dry-run', '--replay', str(replay)]) == 1
    assert 'bad.jsonl:1:' in capsys.readouterr().err


def test_missing_file(capsys):
    assert main(['missing.tidal', '--validate']) == 1
    assert 'Error:' in capsys.readouterr().err


def test_interactive_subprocess():
    result = subprocess.run([sys.executable, '-m', 'tidalbind',
                             str(ROOT / 'examples/pipe-smoke.tidal'), '--dry-run',
                             '--no-midi', '--interactive'],
                            input='next\nblock 0\nnext\nstatus\nquit\n', text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'TidalBind pipe ready' in result.stdout
    assert 'print (2 + 2)' in result.stdout
    assert 'Block must be between' in result.stderr


def test_bad_score_does_not_need_midi(tmp_path, capsys):
    path = tmp_path / 'bad.tidal'
    path.write_text('{-!map,cc128-}')
    assert main([str(path), '--validate']) == 1
    assert 'Unknown MIDI source' in capsys.readouterr().err


def test_live_midi_callback_and_port_cleanup(monkeypatch, tmp_path, capsys):
    import io
    from types import SimpleNamespace
    import mido
    import tidalbind.cli as cli

    opened = []
    closed = []

    class Port:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            closed.append(True)

    def open_input(name, callback):
        opened.append(name)
        callback(mido.Message('control_change', control=64, value=127))
        return Port()

    backend = SimpleNamespace(get_input_names=lambda: ['keyboard', 'pedal'], open_input=open_input)
    monkeypatch.setattr(cli, '_mido', lambda: backend)
    monkeypatch.setattr(sys, 'stdin', io.StringIO('quit\n'))
    path = tmp_path / 'piece.tidal'
    path.write_text('first')
    assert main([str(path), '--dry-run', '--interactive', '--midi-port', 'pedal']) == 0
    assert opened == ['pedal']
    assert closed == [True]
    assert '[execute block 1]' in capsys.readouterr().out


def test_unknown_midi_port_fails_before_ghci(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace
    import tidalbind.cli as cli

    monkeypatch.setattr(cli, '_mido', lambda: SimpleNamespace(get_input_names=lambda: ['keyboard']))
    path = tmp_path / 'piece.tidal'
    path.write_text('first')
    assert main([str(path), '--midi-port', 'missing']) == 1
    assert 'Available inputs' in capsys.readouterr().err


def test_missing_mido_identifies_interpreter_and_install_command(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, 'mido', None)
    assert main(['--list-midi']) == 1
    error = capsys.readouterr().err
    assert "MIDI dependency 'mido'" in error
    assert sys.executable in error
    assert '-m pip install -e .' in error
    assert 'same Python interpreter' in error


def test_missing_rtmidi_identifies_distribution_name(monkeypatch, capsys):
    from types import SimpleNamespace
    import tidalbind.cli as cli

    def unavailable():
        raise ModuleNotFoundError("No module named 'rtmidi'", name='rtmidi')

    monkeypatch.setattr(cli, '_mido', lambda: SimpleNamespace(get_input_names=unavailable))
    assert main(['--list-midi']) == 1
    error = capsys.readouterr().err
    assert "MIDI dependency 'python-rtmidi'" in error
    assert sys.executable in error
    assert '-m pip install -e .' in error


def test_unrelated_backend_import_error_is_not_misdiagnosed(monkeypatch, capsys):
    from types import SimpleNamespace
    import tidalbind.cli as cli

    def unavailable():
        raise ModuleNotFoundError("No module named 'another_module'", name='another_module')

    monkeypatch.setattr(cli, '_mido', lambda: SimpleNamespace(get_input_names=unavailable))
    assert main(['--list-midi']) == 1
    error = capsys.readouterr().err
    assert 'another_module' in error
    assert 'MIDI dependency' not in error
