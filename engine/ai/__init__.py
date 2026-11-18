"""Any local AI model the person chooses, instead of the one model Lastbook was made with.

  gguf.py      what a model file says about itself (its first kilobytes), and how much memory it will need
  models.py    the models here: in the library's own models/ folder (they travel with it), and those other programs
               on this computer already hold (AnythingLLM, LM Studio, Ollama, the Hugging Face cache), used where they are
  runtime.py   llama.cpp's llama-server, started for the chosen model when someone asks a question and stopped when idle.
               It listens on a UNIX socket in a private folder (no port at all); on Windows, on 127.0.0.1 behind a key
  picker.py    which model is chosen, kept with the library

assistant.py asks picker for the chosen model and talks to it in the same OpenAI shape it already used for a model
server on this computer. A model with a projector file beside it (mmproj) can look at page images; one without reads text.

Nothing here opens a connection past this computer. Bringing a model from Hugging Face is engine/wizard/hf_import.py,
through engine/setup/download.py like every other download, and only when the person asks.
"""
