"""Which AI model answers. The person's choice is kept with the library (derived/llm/picked.json), so a library on a
drive remembers it; a choice that names a model only another computer holds falls back to choosing by itself there.

A choice is one of:
  'auto'      Lastbook chooses (below)
  'none'      no AI: Chat gives matching pages only
  'stock'     the model Lastbook was made with (Gemma 4 26B through lastbook-llm), where that is installed
  'server'    a model server already running on this computer (SURVIVAL_MODEL_BASE)
  <model id>  a model from engine/ai/models.py

Choosing by itself, in this order: a model that travels with the library and fits this computer (the largest); the
stock model; a model server on this computer; a model another program on this computer holds, used where it is.
"""
import json
import os
import threading
import time
from pathlib import Path

from engine import paths
from engine.setup import hardware
from . import models, runtime

FIXED = ('auto', 'none', 'stock', 'server')
RESCAN = 30.0


class Picker:
    def __init__(self, data=None):
        self.data = Path(data or paths.DATA)
        self.file = self.data / 'derived' / 'llm' / 'picked.json'
        self.runner = runtime.Runner(self.data)
        self.lock = threading.Lock()
        self._models, self._at = [], 0.0
        self._hw = None
        self._choice = None

    # ------------------------------------------------------------------------------------------------ the choice ----
    def choice(self):
        if self._choice is None:
            try:
                saved = json.loads(self.file.read_text(encoding='utf-8'))
                self._choice = saved if isinstance(saved, dict) and isinstance(saved.get('id'), str) else {'id': 'auto'}
            except (OSError, ValueError):
                self._choice = {'id': 'auto'}
        return self._choice

    def select(self, model_id, port=None):
        """Keeps the choice. Raises KeyError for a model that is not here, ValueError for a port that is not one.
        port: with 'server', the loopback port the person's own model server listens on (Ollama 11434, LM Studio 1234)."""
        if model_id not in FIXED:
            m = self.model(model_id)
            if m is None:
                raise KeyError(model_id)
            choice = {'id': model_id, 'name': m['name'], 'where': m['where']}
        elif model_id == 'server' and port not in (None, ''):
            if isinstance(port, bool) or not str(port).isdigit() or not 1024 <= int(port) <= 65535:
                raise ValueError('a port is a number from 1024 to 65535')
            choice = {'id': 'server', 'port': int(port)}
        else:
            choice = {'id': model_id}
        self._choice = choice
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix('.tmp')
            tmp.write_text(json.dumps(choice), encoding='utf-8')
            os.replace(tmp, self.file)
        except OSError as e:
            print('AI: could not keep the choice of model:', e, flush=True)      # it still holds until Lastbook closes
        if self.runner.model_id and self.runner.model_id != model_id:
            self.runner.stop('another model was chosen')
        return choice

    # ------------------------------------------------------------------------------------------------ the models ----
    def hardware(self):
        if self._hw is None:
            self._hw = hardware.detect()
        return self._hw

    def models(self, refresh=False):
        """Every model here, judged against this computer. Looked for again at most every RESCAN seconds."""
        with self.lock:
            if refresh or time.monotonic() - self._at > RESCAN or not self._at:
                try:
                    self._models = models.found(self.data, hw=self.hardware())
                except Exception as e:      # a strange file in someone's model folder must not take Chat down
                    print('AI: looking for models failed:', type(e).__name__, e, flush=True)
                    self._models = []
                self._at = time.monotonic()
            return self._models

    def model(self, model_id):
        return next((m for m in self.models() if m['id'] == model_id), None)

    def usable(self, m):
        return m is not None and m['fit'].get('fits') is not False and runtime.find(self.data) is not None

    def chosen(self):
        """The model the person named, if it is here and can run; else None."""
        c = self.choice()['id']
        if c in FIXED:
            return None
        m = self.model(c)
        return m if self.usable(m) else None

    def by_itself(self, where):
        """The largest model that fits, among those that travel with the library ('drive') or those this computer holds ('computer')."""
        if runtime.find(self.data) is None:
            return None
        fits = [m for m in self.models() if m['where'] == where and m['fit'].get('fits')]
        return max(fits, key=lambda m: m['model_bytes']) if fits else None

    # ---------------------------------------------------------------------------------------------- for the page ----
    def describe(self, active=None, stock=None, server=None):
        """Everything the model picker shows. active: the id or kind answering now."""
        rt = runtime.find(self.data)
        hw = self.hardware()
        chip = next((g.get('name') for g in hw.get('gpus') or [] if g.get('name')), None)
        return {'selected': self.choice()['id'], 'active': active, 'models': [models.public(m) for m in self.models()],
                'runtime': {'origin': rt['origin']} if rt else None, 'running': self.runner.status(),
                'stock': stock, 'server': server,
                'computer': {'ram_bytes': hw.get('ram_bytes'), 'chip': chip, 'os': hw.get('os'), 'arch': hw.get('arch')}}
