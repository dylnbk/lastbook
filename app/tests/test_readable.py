#!/usr/bin/env python3
"""retrieval.readable(): which passages reach meaning-based search. No library, no model, no server.

  python3 -B -m unittest app.tests.test_readable
"""
import os, sys, unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]


def load():
    """chunks_of and readable, imported without leaving anything behind. engine.paths reads LASTBOOK_DATA once, when it
    is imported, so a copy kept in sys.modules would point the other tests in the same run at the wrong library."""
    before, env = dict(sys.modules), dict(os.environ)
    sys.path.insert(0, str(ROOT))
    sys.modules['onnxruntime'] = None  # importing it now raises ImportError: no model is loaded or needed here
    try:
        import retrieval
        return retrieval.chunks_of, retrieval.readable
    finally:
        sys.path.remove(str(ROOT))
        for name in set(sys.modules) - set(before):
            # numpy stays: it cannot be loaded twice in one process, and tests later in the same run import it
            # (found when every test was first run in one process, on GitHub: "cannot load module more than once per process")
            if name.split('.')[0] != 'numpy':
                del sys.modules[name]
        sys.modules.update(before)
        if 'onnxruntime' not in before:
            sys.modules.pop('onnxruntime', None)
        os.environ.clear()
        os.environ.update(env)


chunks_of, readable = load()

WARNING = ('Do not drink water from a well that has been flooded. Boil it for one minute before you use it, '
           'and store it in clean containers with a tight lid.')


class Readable(unittest.TestCase):
    def test_ordinary_prose(self):
        self.assertTrue(readable(WARNING))

    def test_capitals(self):
        """Warning boxes are often printed in capitals; they matter most and must not be dropped."""
        self.assertTrue(readable(WARNING.upper()))
        self.assertTrue(readable('WARNING: ' + WARNING.upper()))
        self.assertTrue(readable(WARNING.title()))

    def test_curly_apostrophes(self):
        text = ('Don’t drink water that’s been in a flood. If you can’t boil it, it isn’t safe, and you shouldn’t '
                'give it to a child or use it to wash food before it’s treated.')
        self.assertTrue(readable(text))
        self.assertEqual(readable(text), readable(text.replace('’', "'")))

    def test_debris_still_skipped(self):
        table = 'ID QTY UNIT SKU ' + ' '.join(f'A{i} {i * 7} EA X{i:04d}' for i in range(40))
        self.assertFalse(readable(table))
        self.assertFalse(readable('Boil water.'))  # too short

    def test_chunks_keep_capitals(self):
        self.assertTrue(chunks_of(WARNING.upper()))


if __name__ == '__main__':
    unittest.main()
