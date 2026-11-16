# Upside-down WASD -- for a game camera that can roll over the top (looking
# up past straight up turns the view upside down, as in some Roblox games).
# While the view is upside down, W<->S (and A<->D) are swapped; back the
# right way up, they're normal again. Keys held across a flip switch over
# at once.
#
# Macro settings: Python ON, Repeat mode "No Repeat", any key combo (it
# starts the macro; pressing it again stops it). While GAME isn't the
# focused window the macro is paused: the keys act normally and mouse
# movement isn't counted (the pauseWhen() line below).
#
# Calibration (first run, or press CALIBRATE any time; kept in this
# macro's Saved variables):
#   1. respawn so the camera is level, press CALIBRATE
#   2. look up until the view JUST turns upside down, press CALIBRATE
#   3. keep looking up as far as it goes, press CALIBRATE
#   4. respawn (level again), press CALIBRATE
#   5. look down until the view just turns upside down, press CALIBRATE
#   6. keep looking down as far as it goes, press CALIBRATE
# After every respawn (the game resets the camera to level), press LEVEL.
# Changing the game's mouse sensitivity means calibrating again.

LEVEL = KEY_F7          # camera is level right now (after a respawn)
CALIBRATE = KEY_F9      # calibrate again
SWAP_AD = True          # False: only W and S swap
POLL = 0.004            # seconds between mouse readings
GAME = "sober"          # part of the game's window class ("" = any window)
# Keys that flip the swap by hand, on top of the tracking, e.g.
# [KEY_LEFTALT, KEY_TAB, KEY_ESC, KEY_LEFTMETA]
FLIP_KEYS = []

global _running, _stop
if globals().get("_running"):
    _stop = True        # the combo again: stop the running copy
    return
_running, _stop = True, False


def say(text):
    notify(text, "Upside-down WASD")


def mark(prompt):
    say(prompt)
    waitForPress(CALIBRATE, repress=True)
    return getMouseMotion.y


def calibrate():
    while True:
        level = mark("Respawn so the camera is level, then press F9")
        up_flip = mark("Look up until the view JUST turns upside down, press F9") - level
        up_max = mark("Keep looking up as far as it goes, press F9") - level
        level = mark("Respawn again (level), press F9")
        down_flip = mark("Look down until the view just turns upside down, press F9") - level
        down_max = mark("Keep looking down as far as it goes, press F9") - level
        # signs don't matter (an inverted camera flips them); the order does
        ok = (up_flip * up_max > 0 and abs(up_max) >= abs(up_flip) > 0 and
              down_flip * down_max > 0 and abs(down_max) >= abs(down_flip) > 0 and up_flip * down_flip < 0)
        if ok:
            for name, v in (("up_flip", up_flip), ("up_max", up_max),
                            ("down_flip", down_flip), ("down_max", down_max)):
                saveVar(name, v)
            say("Calibrated. Press F7 after every respawn.")
            return
        say("Those readings don't line up -- starting over")


SWAPS = {KEY_W: KEY_S, KEY_S: KEY_W}
if SWAP_AD:
    SWAPS.update({KEY_A: KEY_D, KEY_D: KEY_A})


def set_flipped(on):
    if on:
        remapProfile("upside down", SWAPS)     # held keys switch over at once
    else:
        remapProfile("upside down")            # name only: off


# Swallowing LEVEL / CALIBRATE also keeps the keyboard going through
# Puppetry for the whole run, so a flip never grabs it with a key held down.
if GAME:
    pauseWhen(app=GAME)             # paused while the game isn't focused
ignore_keys(LEVEL, CALIBRATE)       # the game never sees them
aborted = False
try:
    set_flipped(False)
    if loadVar("up_max") is None:
        calibrate()
    up_flip, up_max = loadVar("up_flip"), loadVar("up_max")
    down_flip, down_max = loadVar("down_flip"), loadVar("down_max")
    low, high = min(up_max, down_max), max(up_max, down_max)
    say("On. Press F7 when the camera is level.")

    pitch = 0                       # mouse counts from level; the game's own limits clamp it
    last = getMouseMotion.y
    flipped = False
    by_hand = False                 # FLIP_KEYS flip this
    prev_held = getButtonsHeld()
    prev_t = getTime()
    while not _stop:
        y = getMouseMotion.y
        pitch = max(low, min(high, pitch + y - last))
        last = y
        held = getButtonsHeld()
        now = getTime()
        if now - prev_t > 0.2:      # just back from a pause: keys pressed meanwhile don't count
            prev_held = held
        prev_t = now
        if any(k in held and k not in prev_held for k in FLIP_KEYS):
            by_hand = not by_hand
        if LEVEL in held and LEVEL not in prev_held:
            pitch = 0
        if CALIBRATE in held and CALIBRATE not in prev_held:
            set_flipped(False)
            flipped = False
            calibrate()
            up_flip, up_max = loadVar("up_flip"), loadVar("up_max")
            down_flip, down_max = loadVar("down_flip"), loadVar("down_max")
            low, high = min(up_max, down_max), max(up_max, down_max)
            pitch, last = 0, getMouseMotion.y
            held = getButtonsHeld()
        prev_held = held
        upside_down = (pitch - up_flip) * up_flip >= 0 or (pitch - down_flip) * down_flip >= 0
        want = upside_down != by_hand
        if want != flipped:
            flipped = want
            set_flipped(flipped)
        wait(POLL)
    say("Off.")
except KeyboardInterrupt:
    aborted = True
    raise
finally:
    if not aborted:                 # (the abort key already cleared the profile and the ignore)
        set_flipped(False)
        ignore_keys(LEVEL, CALIBRATE)   # toggle back off
    _running = False
