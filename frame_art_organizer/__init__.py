"""Frame Art Organizer — a local library + scheduler for Samsung The Frame.

The Pi (or any always-on host) owns the photo library and scheduling; the TV keeps
owning Art Mode (matte, ambient brightness, motion/night sleep). We drive it over
the LAN Art API rather than piping HDMI, so the Frame's native low-power behavior
stays intact.
"""

__version__ = "0.1.0"
