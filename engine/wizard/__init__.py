"""Building a library to order: the person says what goes in it, and it is put together on their own computer.

Nothing here opens a connection itself. Every byte comes through engine/setup/download.py, from the hosts it allows, and
only once the person has started a build.

  packzip.py     a published pack read in place: its directory, and any of its members, without the rest of it
"""
