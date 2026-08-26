"""Self-scheduling: measure, recommend an interval, and install a background job.

Core tier. Nothing in this package may import ``contextlake.kb`` at module
level; ``cmds.run`` imports it inside the function, the same way ``cli.py`` does.
"""
