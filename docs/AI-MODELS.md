# Choosing the AI model

Lastbook was made with one model: Gemma 4 26B, a 17 GB file that needs about 21 GB of memory, run by `lastbook-llm`.
On a computer with 16 GB it does not fit, and there was no way to use another. This page describes what the fork
adds: **any GGUF model the person chooses** answers in Chat, chosen in the app, kept with the library.

Nothing about the assistant itself changes. It still searches the library first, still must cite a source for every
statement, and its citations are still checked against what the tools returned. A smaller model writes worse answers;
it does not get to make up sources.

## What can answer

In Chat, **Model** opens the picker (only on the computer the library is open on; a phone the library is shared with
asks questions and does not choose what answers them).

| Choice | What it is |
|---|---|
| Choose for me | The largest model in the library that fits this computer; else the stock model; else a model server on this computer |
| A model in the library | A `.gguf` under `<library>/models/`. It travels with the library, so a drive answers on any computer it is plugged into |
| A model already on this computer | One that AnythingLLM, LM Studio, Ollama or the Hugging Face cache holds. Used where it is: nothing is copied, moved or changed. Used only when chosen |
| The stock model | Gemma 4 26B through `lastbook-llm`, where that is installed |
| A model server on this computer | An OpenAI-shaped server on a loopback address: `SURVIVAL_MODEL_BASE` as before, or a port typed in the picker (Ollama listens on 11434, LM Studio on 1234). Only a port is taken, never an address: it is always this computer |
| No AI | Chat shows the matching pages and writes nothing |

A model another program holds can be **copied into the library** from the picker, and so can the program that runs
models. With both on a drive, the drive answers questions on another computer of the same kind with nothing installed.

The choice is kept in `<library>/derived/llm/picked.json`. A choice that names a model only another computer holds
falls back to "Choose for me" there.

## Bringing a model from Hugging Face

The picker offers three by name and takes any repository pasted as `owner/name` (or its web address, or
`owner/name:Q4_K_M` to ask for a version):

| Offered | Repository | Size | For |
|---|---|---|---|
| Compact | `Qwen/Qwen3-VL-4B-Instruct-GGUF` | about 3.3 GB | 8 GB of memory or more |
| Balanced | `HauhauCS/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced` | about 7.8 GB with its draft file | 16 GB or more |
| Workstation | `unsloth/gemma-4-26B-A4B-it-GGUF` | about 18 GB | 24 GB or more |

All three read page images. The Balanced one is a community retraining of Gemma 4 12B that declines fewer
questions; Google's own conversion is `unsloth/gemma-4-12B-it-GGUF`, which can be pasted instead. Sizes and fit are
read live from the repository when one is looked at, so they are never out of date here.

A version too large for this computer can still be brought, with a plain warning beside it: a library on a drive
may be meant for a computer with more memory. It is not chosen to answer on this one.

**Look** asks Hugging Face what the repository holds: two small lists and the first 64 KB of one file, three requests
in all. It shows every version with its size and whether it fits this computer, the projector file (which lets the
model look at page images) and the draft file (which lets a large model write faster, where this computer's llama.cpp
supports it), and the licence. Nothing large is downloaded to find this out.

**Bring** downloads the chosen version into `<library>/models/<owner>--<name>/`, checks each file against the SHA-256
Hugging Face publishes for it, and writes `model.json` beside them (repository, revision, licence, checksums). The
revision brought is the one that was looked at.

All of it goes through `engine/setup/download.py`, to `huggingface.co` and Hugging Face's own download hosts, under
the same pinned roots as every other download. Nothing is sent but the repository's name. A repository that asks for
an account (gated or private) cannot be brought: Lastbook holds no Hugging Face login and asks for none.

## Will it run on this computer?

A GGUF file opens with its metadata: the model's name, then its shape (layers, width, attention heads, the context
it was trained for), then megabytes of vocabulary. The shape is in the first kilobytes, so 64 KB of a file still on a
server is enough to estimate what it needs:

    weights (the file) + projector and draft (held whole) + the context's memory + working room

The context's memory is counted layer by layer. Gemma gives most of its layers a 1,024-token window with small heads
and only a few layers the whole context, and says which is which in its header; counted naively its context would be
five times too large.

| Model | Context | Estimated | Measured |
|---|---|---|---|
| Qwen3-VL 4B Q4_K_M (2.5 GB) with its projector (0.84 GB) | 8,192 | 5.2 GB | 4.4 GB |

One measurement, and the estimate was 0.8 GB high: about the size of the projector, which the estimate counts whole
on top of everything else. It errs towards saying a model is too large rather than towards starting one that is. It
has not been checked against a Gemma model's measured memory.

The estimate picks the largest context that fits (16,384 down to 6,144 tokens; the assistant's prompt with its
sources needs about 6,000) and keeps 2.5 GB back for the system and the rest of Lastbook. It is an estimate.
llama.cpp decides when the model loads, and if it will not load, the next smaller context is tried.

Fitting the computer is not the same as there being room at this moment. A model is not started when the memory
free right now is under three quarters of what it uses: it would load by pushing other programs into the swap file,
and the whole computer crawls while it does. The person is told how much is free and asked to close something.

On Windows and Linux with a graphics card, the card's memory counts as room beyond the computer's own. That part
follows the existing hardware detection and has not been run on such a machine.

## How a chosen model is run

By llama.cpp's own `llama-server`, started when someone asks a question and stopped after 15 minutes unused.

- **Where it listens.** On a Mac and Linux: a UNIX socket in a folder only this user can open, in the system's
  temporary folder. There is no port, so nothing else on the computer or the network can reach the model. On
  Windows, where Python cannot open such a socket: 127.0.0.1 on a free port, behind a key made for that run.
- **When Lastbook ends**, however it ends, the model ends too: a small shell stands between them and checks every
  two seconds that Lastbook is still there. (A Mac has no way to tell a child process to end with its parent.)
- **Which `llama-server`**, first found: `LASTBOOK_LLAMA_SERVER`; `<library>/models/runtime/<os>-<arch>/`;
  `<app>/llm/`; AnythingLLM's own copy; Homebrew's; the `PATH`. Its options are read from its own `--help`, so an
  older build is not handed options it would refuse.
- **Tools and pictures.** The model's own chat template handles tool calls (`--jinja`). A model with a projector is
  offered `view_page` and can look at a page's image; one without is told it reads text only.
- **Thinking** is turned off where the model's template has a switch for it, and anything between `<think>` marks is
  never shown: someone in trouble should not wait through a page of reasoning.
- **If it will not load**, it is tried again without its draft file, then with smaller contexts, before saying so.

## What was measured, and what was not

Run on an Apple M4 with 16 GB, with the `llama-server` AnythingLLM ships (build 11425):

- Qwen3-VL 4B from AnythingLLM's folder, chosen in the picker: started in 6.4 s, called `search_library`, answered
  with a checked citation; described a test picture correctly.
- A 365 MB model brought from Hugging Face end to end (two files, SHA-256 checked), found in the library, chosen,
  removed.
- `llama-server` and its ten libraries copied to an exFAT drive and run from there: 32 s the first time (macOS
  checks new programs once), 3 s after.

Not run: Windows, Linux, a computer with a separate graphics card, Ollama's and LM Studio's folders (read from
their documented layouts), and a model with a draft file actually speeding anything up.

## In the code

| Where | What |
|---|---|
| `engine/ai/gguf.py` | Reading a model file's header; the memory estimate |
| `engine/ai/models.py` | Finding models in the library and on this computer; judging the fit |
| `engine/ai/runtime.py` | Finding and running `llama-server` |
| `engine/ai/picker.py` | The choice, kept with the library |
| `engine/ai/carry.py` | Copying a model, or the program that runs models, into the library |
| `engine/wizard/hf_import.py` | Looking at a Hugging Face repository and bringing a model from it |
| `assistant.py` | `backend()` decides what answers; `_http_round` talks to the chosen model |
| `server.py` | `/api/ai/models`, `/select`, `/inspect`, `/import`, `/carry`, `/remove`, `/stop`: this computer only |
| `web/models.js` | The picker |
| `app/tests/test_ai.py` | All of it, with made-up model files, a stand-in `llama-server` and a made-up Hugging Face |

`LASTBOOK_AI_PICKER=0` turns the whole of it off: Lastbook then behaves as it did.
