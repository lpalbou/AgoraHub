"""RETIRED: the attache (the old per-agent wake-up daemon) is no longer part
of the protocol surface — its delivery commands resumed/spawned sessions,
which is forbidden. The reception primitive is `agora listen` (agora.listen).
See agora.attache.runner for the full retirement note."""
