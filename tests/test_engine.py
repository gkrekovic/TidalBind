import mido
import pytest

from tidalbind.bridge import DryRunBridge
from tidalbind.engine import Performance
from tidalbind.midi import MidiState
from tidalbind.score import parse_score


def cc(control, value):
    return mido.Message('control_change', control=control, value=value)


def note(note=60, velocity=100, kind='note_on'):
    return mido.Message(kind, note=note, velocity=velocity)


def player(text, **kwargs):
    sink = DryRunBridge()
    performance = Performance(parse_score(text), sink, **kwargs)
    performance.start()
    return performance, sink


def test_default_pedal_and_eof_no_wrap():
    performance, sink = player('first\n\nsecond')
    performance.handle_midi(cc(64, 0))
    assert not sink.commands
    for _ in range(4):
        performance.handle_midi(cc(64, 127))
    assert sink.commands == ['first', 'second']
    assert performance.next_index == 2


def test_leading_standalone_bindings_are_armed_not_executed():
    performance, sink = player('{-!triggerNextBlock,cc1,any-}\n{-!hush,note36,>,0-}\n\nfirst',
                               default_pedal=False)
    assert performance.next_index == 1
    assert not sink.commands
    performance.handle_midi(cc(64, 127))
    assert not sink.commands
    performance.handle_midi(cc(1, 0))
    performance.handle_midi(note(36))
    assert sink.commands == ['first', 'hush']


def test_trigger_block_persists_without_moving_pointer():
    performance, sink = player('{-!triggerBlock,note36,>,0-}\nfirst\n\nsecond')
    performance.next()
    performance.next()
    performance.handle_midi(note(36))
    assert performance.next_index == 2
    assert sink.commands[-1].strip() == 'first'
    count = len(sink.commands)
    performance.handle_midi(note(36, 0))
    performance.handle_midi(note(36, 64, 'note_off'))
    assert len(sink.commands) == count


def test_binding_replacement_and_reactive_does_not_steal():
    performance, sink = player('''{-!triggerBlock,note36,>,0-}
first {-!map,cc1-}

{-!triggerBlock,note36,>,0-}
second {-!map,cc1-}
''', missing_values='zero')
    performance.next()
    performance.next()
    performance.handle_midi(note(36))
    assert sink.commands[-1].strip() == 'second 0'
    performance.evaluate(0)  # Explicit activation owns the bindings again.
    performance.handle_midi(cc(1, 5))
    assert sink.commands[-1].strip() == 'first 5'


def test_retrigger_does_not_steal_replaced_map_owner():
    performance, sink = player('''{-!triggerBlock,note36,>,0-}
first {-!map,cc1-}

second {-!map,cc1-}''', missing_values='zero')
    performance.next()
    performance.next()
    performance.handle_midi(note(36))
    performance.handle_midi(cc(1, 7))
    assert sink.commands[-1] == 'second 7'


def test_note_and_velocity_coalesced_and_latest_values_resolved():
    performance, sink = player('d1 $ note "{-!map,noteOn-}" # gain {-!map,velocity,0,127,0,1-}',
                               missing_values='defer')
    performance.next()
    assert not sink.commands
    performance.handle_midi(note(60, 127))
    assert sink.commands == ['d1 $ note "60" # gain 1']
    performance.handle_midi(note(62, 64))
    assert len(sink.commands) == 2
    assert 'note "62"' in sink.commands[-1]
    performance.handle_midi(note(62, 10, 'note_off'))
    assert len(sink.commands) == 2


def test_trigger_and_map_same_block_evaluated_once():
    performance, sink = player('{-!triggerBlock,note36,>,0-}\nx {-!map,noteOn-}', missing_values='zero')
    performance.next()
    performance.handle_midi(note(36))
    assert len(sink.commands) == 2
    assert sink.commands[-1].strip() == 'x 36'


def test_bindings_from_same_event_do_not_recurse():
    performance, sink = player('{-!triggerNextBlock,cc1,any-}\n\nx {-!map,cc1-}\n\ny',
                               default_pedal=False)
    performance.handle_midi(cc(1, 99))
    assert sink.commands == ['x 99']
    assert performance.next_index == 2


def test_channel_filter_is_one_based():
    performance, sink = player('first', channel=2)
    performance.handle_midi(cc(64, 127))
    assert not sink.commands
    performance.handle_midi(cc(64, 127).copy(channel=1))
    assert sink.commands == ['first']


def test_reload_resets_bindings_and_pointer_but_keeps_values():
    performance, sink = player('old {-!map,cc1-}', missing_values='defer')
    performance.next()
    performance.handle_midi(cc(1, 42))
    performance.replace_score(parse_score('new {-!map,cc2-}'))
    assert performance.next_index == 0
    assert performance.state.values['cc1'] == 42
    performance.handle_midi(cc(1, 43))
    assert sink.commands == ['old 42']
    performance.next()
    performance.handle_midi(cc(2, 7))
    assert sink.commands[-1] == 'new 7'


@pytest.mark.parametrize(('policy', 'expected'), [('empty', 'x'), ('zero', 'x 0'), ('defer', None)])
def test_uninitialized_policy(policy, expected):
    notices = []
    performance, sink = player('x {-!map,cc1-}', missing_values=policy, notify=notices.append)
    performance.next()
    assert sink.commands == ([] if expected is None else [expected])
    assert any(n.kind == 'missing' for n in notices)
    performance.handle_midi(cc(1, 9))
    assert sink.commands[-1] == 'x 9'


def test_midi_state_pitchbend_release_and_ignored_messages():
    state = MidiState()
    assert state.update(mido.Message('pitchwheel', pitch=-8192)) == {'pitchBend': -8192}
    assert state.update(note(72, 100)) == {'noteOn': 72, 'velocity': 100, 'note72': 100}
    assert state.update(note(72, 30, 'note_off')) == {'noteOff': 72, 'note72': 0}
    assert state.values['velocity'] == 100
    assert not state.update(mido.Message('clock'))


def test_explicit_block_and_command_validation():
    performance, _ = player('first')
    with pytest.raises(IndexError):
        performance.evaluate(-1)
    with pytest.raises(ValueError):
        performance.command(':quit')


def test_failed_send_does_not_consume_pointer():
    class Broken(DryRunBridge):
        def send_block(self, code):
            raise RuntimeError('pipe failed')
    performance = Performance(parse_score('first'), Broken())
    with pytest.raises(RuntimeError):
        performance.next()
    assert performance.next_index == 0


def test_hush_is_last_when_event_also_matches_maps():
    performance, sink = player('{-!hush,note36,>,0-}\n\nx {-!map,noteOn-}', missing_values='zero')
    performance.next()
    performance.handle_midi(note(36))
    assert sink.commands[-1] == 'hush'


def test_hush_still_runs_when_same_event_pattern_is_rejected():
    from tidalbind.bridge import GhciError

    class RejectPattern(DryRunBridge):
        def send_block(self, code):
            if code == 'x 36':
                raise GhciError('invalid pattern')
            super().send_block(code)

    sink = RejectPattern()
    performance = Performance(parse_score('{-!hush,note36,>,0-}\n\nx {-!map,noteOn-}'), sink,
                              missing_values='zero')
    performance.start()
    performance.next()
    with pytest.raises(GhciError, match='invalid pattern'):
        performance.handle_midi(note(36))
    assert sink.commands[-1] == 'hush'
