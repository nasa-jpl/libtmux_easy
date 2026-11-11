"""Copying the script's own output to the dashboard's home log. See
TmuxSession.log_home()."""

import logging


class _Tee:
    """A text stream that also copies everything written to it to 'log'."""

    def __init__(self, stream, log):
        self._stream = stream
        self._log = log

    def write(self, text):
        """Write to the stream and copy to the log; a closed log is skipped."""
        n = self._stream.write(text)
        try:
            self._log.write(text)
        except (OSError, ValueError):  # closed by untee, or disk trouble
            pass
        return n

    def flush(self):
        """Flush the stream and the log."""
        self._stream.flush()
        try:
            self._log.flush()
        except (OSError, ValueError):
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _SkipIfTeed(logging.Filter):
    """Drop records the stderr tee already copies: a root handler created after
    log_home() (e.g. a late logging.basicConfig()) writes to the tee itself."""

    def filter(self, record):
        """Drop the record if a root handler writes to the tee: copied already."""
        return not any(
            isinstance(getattr(h, "stream", None), _Tee) for h in logging.getLogger().handlers
        )
