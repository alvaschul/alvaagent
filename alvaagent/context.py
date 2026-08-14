"""Runtime context object — placeholder during the mechanical split.

The real Runtime lands in the Runtime-threading phase; until then the
modules keep using the single file's module globals.
"""

class Runtime:  # noqa: D101 - replaced in Task 14
    def __init__(self, data_dir=None):
        self.data_dir = data_dir
