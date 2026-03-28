"""
display.py - MAX7219 8-digit 7-segment display driver for garage.py

Each display function switches the MAX7219 decode register to the mode it needs:
  display_number    -> 0xFF (full BCD, all 8 digits available for numbers)
  display_time      -> 0xDB (BCD except positions 3 and 6 which go raw for '=')
  display_countdown -> 0xFB (BCD except position 3 which goes raw for '=')

Provides:
  init()                  - (re-)initialise the MAX7219 (sets 0xDB as baseline)
  close()                 - release the SPI device
  display_number(n)       - show integer 0..99,999,999 on all 8 digits (0xFF decode)
  display_time()          - show HH = MM = SS (0xDB decode, raw '=' at positions 3 and 6)
  display_countdown(secs) - show MMM=SS right-aligned (0..59999 s, up to 999 min 59 sec)
"""

import spidev
import time
from datetime import datetime

# ── SPI / MAX7219 ─────────────────────────────────────────────────────────────
_spi = spidev.SpiDev()
_spi.open(0, 0)
_spi.max_speed_hz = 100000
_spi.mode = 0


def _wr(reg, val):
    _spi.xfer2([reg, val])


def close():
    """Release the SPI device (call on shutdown)."""
    _spi.close()


# Decode masks
_DECODE_MIXED = 0xDB   # BCD for positions 1,2,4,5,7,8; raw for 3 and 6
_DECODE_FULL  = 0xFF   # BCD for all 8 positions
_DECODE_MMSS  = 0xFB   # BCD for all positions except 3 (raw for '=')
_SEP_EQUAL    = 0x09   # raw '=' : segments d(D3) + g(D0) = middle + bottom bars
_SEP_BLANK    = 0x00   # raw blank for position 3 and 6 init


def init():
    """(Re-)initialise the MAX7219. Baseline decode is 0xDB (mixed).
    All config and digit registers are written while in shutdown so the display
    wakes up clean (blank) instead of briefly showing stale/garbage values.
    """
    _wr(0x0F, 0x00)            # display test OFF
    _wr(0x0C, 0x00)            # shutdown (registers remain writable)
    time.sleep(0.01)
    _wr(0x0B, 0x07)            # scan all 8 digits
    _wr(0x0A, 0x08)            # brightness (0-15)
    _wr(0x09, _DECODE_MIXED)   # baseline: mixed decode
    for d in range(1, 9):      # pre-load blank values while still in shutdown
        _wr(d, _SEP_BLANK if d in (3, 6) else 0xF)
    _wr(0x0C, 0x01)            # wake up: display shows the blank values above


def _bcd(ch):
    """Convert a format character to BCD value (blank=0xF or digit 0-9)."""
    return 0xF if ch == ' ' else int(ch)


def display_number(n):
    """Display integer n (0..99,999,999) across all 8 digits. Leading blanks.
    Switches to full BCD decode (0xFF) so all 8 positions are numeric.
    """
    _wr(0x09, _DECODE_FULL)   # all 8 digits in BCD
    s = "%8d" % n
    chars = list(reversed(s))
    for pos in range(1, 9):
        _wr(pos, _bcd(chars[pos - 1]))


def display_time():
    """Show HH = MM = SS on all 8 digits.
    Switches to mixed decode (0xDB) so positions 3 and 6 are raw for '='.
    Layout (left->right): pos8=tens-H, pos7=ones-H, pos6='=',
                          pos5=tens-M, pos4=ones-M, pos3='=',
                          pos2=tens-S, pos1=ones-S
    """
    _wr(0x09, _DECODE_MIXED)   # positions 3 and 6 -> raw mode
    now = datetime.now()
    hh, mm, ss = now.hour, now.minute, now.second
    _wr(1, ss % 10)
    _wr(2, ss // 10)
    _wr(3, _SEP_EQUAL)
    _wr(4, mm % 10)
    _wr(5, mm // 10)
    _wr(6, _SEP_EQUAL)
    _wr(7, hh % 10)
    _wr(8, hh // 10)


def display_countdown(seconds):
    """Show MMM=SS right-aligned (0..59999 seconds, up to 999 min 59 sec).
    Uses _DECODE_MMSS (0xFB): only position 3 is raw (for '=').
    Layout (left->right): pos8=blank, pos7=blank,
                          pos6=hundreds-M (blank if <100), pos5=tens-M (blank if <10),
                          pos4=ones-M, pos3='=', pos2=tens-S, pos1=ones-S

    Position 6 is written immediately after the decode-mode switch to prevent a
    brief '0' flash: init() leaves 0x00 there (raw blank), which reads as '0'
    in BCD mode until overwritten.
    """
    _wr(0x09, _DECODE_MMSS)   # only position 3 is raw (for '=')
    mm = min(seconds // 60, 999)
    ss = seconds % 60
    _wr(6, mm // 100 if mm >= 100 else 0xF)   # write first to kill the '0' flash
    _wr(8, 0xF)   # blank
    _wr(7, 0xF)   # blank
    _wr(5, (mm // 10) % 10 if mm >= 10 else 0xF)
    _wr(4, mm % 10)
    _wr(3, _SEP_EQUAL)
    _wr(2, ss // 10)
    _wr(1, ss % 10)
