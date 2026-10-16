"""GridSense grid-restoration simulator and controller tests."""

import codecs
import locale
import os

__version__ = "0.4.4"


def _check_native_locale():
    # The pinned POSIX DSS backend can crash during import in this combination.
    # Python's UTF-8 mode does not change the native library's locale.
    if (
        os.name == "posix"
        and not os.getcwdb().isascii()
        and codecs.lookup(locale.getencoding()).name != "utf-8"
    ):
        raise RuntimeError(
            "GridSense needs a UTF-8 locale for a working directory containing non-ASCII "
            "characters. Start with LC_ALL=C.UTF-8 (or another installed UTF-8 locale), "
            "or use an ASCII-only working directory. PYTHONUTF8 alone is insufficient "
            "for the native OpenDSS backend."
        )


_check_native_locale()
