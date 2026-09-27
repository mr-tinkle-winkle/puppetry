#pragma once
// Pointer acceleration for OUR virtual mouse, off by default.
//
// WHY THIS EXISTS. Our virtual mouse is a relative pointing device, so
// libinput applies its usual pointer-acceleration curve to everything we
// emit -- the same curve that makes a real mouse feel good and makes
// synthetic motion land in the wrong place. A macro asking for a 400px
// move gets however many pixels the accel curve decides that velocity
// deserves. move_mouse(move_to=True) already closes the loop (it asks
// KWin where the cursor actually ended up and nudges), so it lands
// correctly either way -- but it pays kdotool round-trips to do it, and
// plain relative moves have nothing to correct against at all.
//
// There is no device-side way to opt out: the accel profile is a
// per-device COMPOSITOR setting, not a uinput capability. On KDE that
// setting lives in kcminputrc, keyed by the device's vendor id, product
// id and name -- all three of which we choose ourselves (see
// uinput_device.hpp). So we write that one group before creating the
// device, and KWin picks it up when the device appears.
//
// Scope, deliberately narrow: we touch exactly the group belonging to
// our own virtual mouse and leave every other byte of the file alone
// (the user's real mouse settings live in the same file). Nothing here
// runs unless the machine looks like a KDE install -- on anything else
// it's a no-op rather than a stray config file.
#include <string>

namespace puppetry {

// The kcminputrc group header KDE uses for a libinput device.
std::string libinput_config_group(int vendor, int product, const std::string& device_name);

// Returns what kcminputrc should contain so that `group` selects the
// flat (unaccelerated, 1:1) profile: PointerAccelerationProfile=1 with
// PointerAcceleration=0. Everything outside that group is preserved
// byte-for-byte; an existing group has just those two keys corrected.
// Pure text transform, and idempotent -- applying it to its own output
// returns the input unchanged. Exposed (and unit-tested) separately from
// the file I/O below because this is the part that can get subtly wrong.
std::string kcminputrc_with_flat_accel(const std::string& current, const std::string& group);

// True if this machine looks like a KDE install worth writing the above
// to: an existing kcminputrc or kwinrc, or a KDE-ish session in the
// environment. Deliberately conservative -- a false here just means we
// don't write anything.
bool kde_config_present();

enum class AccelResult {
    Wrote,       // the file was updated
    AlreadySet,  // nothing to do; the group was already correct
    NotKde,      // no KDE config found -- skipped on purpose
    Failed,      // couldn't read/write the file
};

// Reads ~/.config/kcminputrc, applies kcminputrc_with_flat_accel() for
// the given device, and writes it back (atomically, via a temp file plus
// rename) only if the contents actually changed. Best-effort: every
// failure is reported, never thrown.
AccelResult ensure_flat_pointer_accel(const std::string& device_name, int vendor, int product);

} // namespace puppetry
