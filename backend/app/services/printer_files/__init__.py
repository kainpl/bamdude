"""One narrow protocol over the two ways a printer's files can be reached.

``transport_for(printer, storage)`` is the only place a storage name becomes a
transport; everything above it speaks ``external`` / ``internal`` and nothing
else.
"""
