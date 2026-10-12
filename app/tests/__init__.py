"""The tests. The card's free space is the test machine's, and on the Pi /tmp is a small
memory disk, under the card's reserve (storage.RESERVE_BYTES): every photo would be
refused. A big card instead; the tests of the reserve set their own."""

from pycam import storage

measured_free_bytes = storage.Storage.free_bytes
storage.Storage.free_bytes = lambda self: 64 * 1024**3
