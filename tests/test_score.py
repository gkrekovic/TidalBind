from decimal import Decimal

import pytest

from tidalbind.score import ScoreError, canonical_source, load_score, parse_score


@pytest.mark.parametrize(("source", "expected"), [
    ("CC074", "cc74"), ("sustain", "cc64"), ("noteOn", "noteOn"),
    ("NOTE127", "note127"), ("velocity", "velocity"),
    ("pitchBand", "pitchBend"), ("pitchBend", "pitchBend"), ("noteOff", "noteOff"),
])
def test_sources(source, expected):
    assert canonical_source(source) == expected


@pytest.mark.parametrize("source", ["cc128", "note-1", "note128", "pitch", "cc", ""])
def test_invalid_sources(source):
    with pytest.raises(ValueError):
        canonical_source(source)


def test_split_blocks_comments_crlf_and_bom():
    score = parse_score('\ufeff-- heading\r\n\r\nd1 $ s "bd"\r\n  # gain 0.5\r\n \r\n\r\nhush\r\n')
    assert len(score.blocks) == 2
    assert score.blocks[0].line == 3
    assert '# gain 0.5' in score.blocks[0].text
    assert score.blocks[1].text == 'hush'


def test_multiline_comments_strings_and_explicit_ghci_blocks():
    text = '''{- outer

{- nested -}
-}
:{
let x = 1

let y = 2
:}

putStrLn "a\\

\\b"
'''
    score = parse_score(text)
    assert len(score.blocks) == 2
    assert 'let y = 2' in score.blocks[0].text


def test_tags_inside_strings_but_not_ordinary_comments():
    score = parse_score('''-- {-! not_a_function -}
{- ignore {-! not_a_function -} -}
d1 $ note "{- ! map, noteOn -}" # gain {-!map,velocity,0,127,0,1-}
''')
    block = score.blocks[0]
    assert len(block.markups) == 2
    rendered, missing = block.render({"noteOn": 60, "velocity": 127})
    assert 'note "60" # gain 1' in rendered
    assert not missing
    assert "not_a_function" in rendered  # Ordinary comments are preserved.


def test_raw_and_scaled_mapping_and_reversed_range():
    block = parse_score('x {-!map,cc1-} {-!map,cc1,0,100,1000,0-}').blocks[0]
    assert block.render({"cc1": 25}) == ("x 25 750", ())
    assert block.render({"cc1": 125}) == ("x 125 -250", ())  # Linear extrapolation.


def test_missing_value_policies_preserve_paper_concatenation():
    block = parse_score('x 0{-!map,cc1-} {-!map,cc2-}').blocks[0]
    assert block.render({}) == ("x 0", ("cc1", "cc2"))
    assert block.render({}, "zero") == ("x 00 0", ("cc1", "cc2"))
    assert block.render({"cc1": 42, "cc2": 1}) == ("x 042 1", ())


@pytest.mark.parametrize("body", [
    "missing,cc1", "map", "map,cc128", "map,cc1,0,0,1,2", "map,cc1,0,1,2",
    "map,cc1,0,127,NaN,1", "map,cc1,0,127,Infinity,1",
    "hush,cc1,!=,0", "hush,cc1,any,0", "hush,cc1,=", "hush,cc1", "hush,cc1,=,cat",
])
def test_bad_markup_has_source_location(body):
    with pytest.raises(ScoreError, match=r"piece.tidal:3:"):
        parse_score("-- header\n\nx {-!" + body + "-}", "piece.tidal")


@pytest.mark.parametrize("text", ["x {-!map,cc1", "{- unterminated", ":{\nx", ":}\nx", "-- just comments"])
def test_malformed_score(text):
    with pytest.raises(ScoreError):
        parse_score(text)


@pytest.mark.parametrize(("operator", "value", "yes", "no"), [
    ("=", 10, 10, 9), ("<", 10, 9, 10), (">", 10, 11, 10),
    ("<=", 10, 10, 11), (">=", 10, 10, 9),
])
def test_comparators(operator, value, yes, no):
    markup = parse_score(f'{{-!hush,cc1,{operator},{value}-}}').blocks[0].markups[0]
    assert markup.matches(yes)
    assert not markup.matches(no)


def test_any_and_markup_only_block():
    block = parse_score('{-!resetCycles,pitchBand,any-}').blocks[0]
    assert not block.has_code
    assert block.markups[0].matches(-8192)
    assert block.markups[0].matches(8191)


def test_load_utf8(tmp_path):
    path = tmp_path / 'poetry.tidal'
    path.write_text('-- Zmija – pjesma\nhush', encoding='utf-8-sig')
    assert load_score(path).blocks[0].has_code
