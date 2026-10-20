import my_python_project.animation as animation


def test_glyph_draws_from_charset():
    seen = {animation._glyph("SARA") for _ in range(200)}
    assert seen <= {"S", "A", "R"}  # letters of SARA (A appears twice)
    assert "S" in seen and "A" in seen and "R" in seen

    bits = {animation._glyph("01") for _ in range(200)}
    assert bits == {"0", "1"}

    # An empty charset falls back to binary rather than crashing.
    assert animation._glyph("") in {"0", "1"}


def test_rain_initial_shape():
    rain = animation._Rain(cols=10, rows=20)
    assert len(rain.heads) == 10
    assert len(rain.speeds) == 10
    assert all(s in (1, 2) for s in rain.speeds)


def test_rain_advances_downward():
    rain = animation._Rain(cols=5, rows=20)
    before = list(rain.heads)
    rain.advance()
    # Every column moves down by its (positive) speed.
    assert all(a >= b for a, b in zip(rain.heads, before))
    assert any(a > b for a, b in zip(rain.heads, before))


def test_rain_recycles_offscreen_columns():
    rain = animation._Rain(cols=3, rows=10)
    # Push all heads well past the bottom, then watch for a respawn: a head
    # jumping from off-screen back near the top shows up as a decrease.
    rain.heads = [1000, 1000, 1000]
    respawned = False
    for _ in range(200):
        before = list(rain.heads)
        rain.advance()
        if any(a < b for a, b in zip(rain.heads, before)):
            respawned = True
            break
    assert respawned
