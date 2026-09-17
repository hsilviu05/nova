# Running the model on another machine

NOVA's tools run in the API process. `git_status` reads the repositories on
the machine the API is running on, `system_health` reports that machine's
load, `disk_usage` measures its disk. That is not configuration — it is what
those tools *are*.

So the split is decided by one question: **which machine do you want NOVA to
be able to tell you about?** The API belongs there. Only the model moves.

    ┌─────────────────────┐              ┌──────────────────────┐
    │  Mac                │              │  any box with a GPU  │
    │                     │   HTTP       │                      │
    │  NOVA API ──────────┼─────────────▶│  Ollama :11434       │
    │  Postgres, Redis    │  :11434      │                      │
    │  tools run HERE     │              │  weights live here   │
    └─────────────────────┘              └──────────────────────┘
              ▲
              │ :8000
         ┌────┴────┐
         │  phone  │
         └─────────┘

The phone talks to the API. The API talks to the model. Nothing talks to the
GPU box except the API.

## Why not move everything

Putting the API on the GPU box is simpler to operate and gives you a server
that survives the laptop closing — but then `git_status` reports on the GPU
box, which has none of your repositories, and `system_health` describes a
machine you were not asking about. If that is what you want, move it; NOVA
runs the same on Linux, and the memory reader is better there (`/proc`
rather than shelling out to `vm_stat`).

There is no third option where the API is remote and the tools are local.
That would need NOVA to execute commands on another machine over the
network, which is the one capability this design spends most of its effort
not having. See SECURITY.md.

## On the GPU box

One thing is true on every platform and is the single reason this does not
work first time: **Ollama binds `127.0.0.1` by default.** Everything below is
some variation of telling it not to, and then letting the traffic through a
firewall.

### Linux

    OLLAMA_HOST=0.0.0.0 ollama serve

On a systemd host, make it stick:

    sudo systemctl edit ollama

    [Service]
    Environment="OLLAMA_HOST=0.0.0.0"

    sudo systemctl restart ollama

Pull a model the GPU can hold. NOVA needs one that supports tool calling —
without it the model will describe running a command instead of running one:

    ollama pull qwen2.5:7b
    ollama show qwen2.5:7b        # `tools` must appear under capabilities

Confirm it is actually on the GPU rather than quietly on the CPU:

    ollama run qwen2.5:7b hi
    ollama ps                     # PROCESSOR should say GPU, not CPU

Open the port on the host firewall, and give the machine a DHCP reservation
so its address does not move.

### Bazzite, and other immutable distributions

Bazzite is `rpm-ostree`-based, so there is no `dnf install ollama` that
survives. Two routes, both fine:

- `ujust` — Bazzite ships recipes for common things; `ujust --choose` and
  look for an Ollama entry. This is the path of least resistance.
- A container with GPU passthrough, which is what the OS is designed around:

      podman run -d --replace --name ollama \
          --device nvidia.com/gpu=all \
          --security-opt=label=disable \
          -p 11434:11434 \
          -v ollama:/root/.ollama \
          -e OLLAMA_HOST=0.0.0.0 \
          docker.io/ollama/ollama

  `--device nvidia.com/gpu=all` needs CDI to be generated once:

      sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
      nvidia-ctk cdi list           # nvidia.com/gpu=all should appear

  On a Bazzite NVIDIA image the driver and toolkit are already present. On a
  non-NVIDIA image they are not, and no amount of container configuration
  will conjure them — check `nvidia-smi` first.

### Windows

The easiest of the three. Ollama ships a native installer, and a current
GeForce driver is all the GPU support it needs — no container runtime, no
toolkit, nothing to pass through.

1. Install the NVIDIA driver, then check it took:

       nvidia-smi

   The table prints the GPU name and total VRAM. VRAM is what decides which
   model you can run; system RAM is almost irrelevant once a GPU is in play.
   Ollama needs compute capability 5.0 or newer, which means roughly a
   GTX 900 series or later — `nvidia-smi` naming a card at all is a good
   sign, but an old Quadro or a 700-series will not do.

2. Install Ollama from ollama.com. It runs in the tray and starts at login.

3. Make it listen on the network. In **Settings → System → About → Advanced
   system settings → Environment Variables**, add a user variable:

       OLLAMA_HOST = 0.0.0.0

   Or from an elevated PowerShell:

       setx OLLAMA_HOST "0.0.0.0" /M

   Either way **quit Ollama from the tray and start it again** — it reads
   the variable once, at launch, so a running instance keeps the old value
   and you will think the setting did nothing.

4. Let it through the firewall. This is the step that silently breaks
   everything, because a blocked port looks exactly like a server that is
   not running. From an elevated PowerShell:

       New-NetFirewallRule -DisplayName "Ollama" -Direction Inbound `
           -LocalPort 11434 -Protocol TCP -Action Allow -Profile Private

   `-Profile Private` deliberately: your home network only. If Windows has
   the network marked Public the rule will not apply, and the fix is to set
   the network to Private rather than to widen the rule.

5. If the laptop has a small system drive, put the weights elsewhere before
   pulling anything — models are gigabytes each:

       setx OLLAMA_MODELS "D:\ollama" /M

6. Stop it sleeping. A laptop lid-closes into standby and takes the model
   server with it, which is the same trap the Mac had. **Settings → System →
   Power → Lid and button actions**, set closing the lid to do nothing while
   plugged in, and screen/sleep to Never on AC.

Then pull a model and confirm the GPU is actually being used:

    ollama pull qwen2.5:7b
    ollama show qwen2.5:7b
    ollama run qwen2.5:7b hi
    ollama ps

`ollama ps` prints a PROCESSOR column. If it says CPU, the GPU is not being
used and the model will be slow in a way no amount of configuration on the
NOVA side will fix — go back to `nvidia-smi`.

## Sizing the model to the VRAM

`nvidia-smi` prints the card's total memory — the `/ 4096MiB` half of the
Memory-Usage column. That number, not system RAM, is the budget. On a laptop
with switchable graphics the display usually runs off the integrated GPU, so
if `nvidia-smi` shows no processes the whole card is yours.

The weights have to fit, and the KV cache for the context sits alongside
them. A rough budget: weights, plus a few hundred megabytes per 8k of
context, plus headroom. Overfill it and Ollama silently moves layers to the
CPU — the model still answers, just many times slower, which reads as "the
GPU is disappointing" rather than "the model does not fit".

Sizes are from the registry, and only models that advertise `tools` are
listed — NOVA needs tool calling, and a model without it describes running a
command instead of running one.

| Model | Download | Fits 4 GB | Fits 8 GB | Fits 16 GB |
| --- | --- | --- | --- | --- |
| `qwen2.5:3b` | 1.9 GB | yes | yes | yes |
| `llama3.2:3b` | 2.0 GB | yes | yes | yes |
| `qwen3:4b` | 2.5 GB | yes | yes | yes |
| `qwen2.5:7b` | 4.7 GB | no | yes | yes |
| `qwen3:8b` | 5.2 GB | no | yes | yes |
| `qwen2.5:14b` | 9.0 GB | no | no | yes |

Notably absent: **Gemma.** `gemma3` advertises vision, not tools, so NOVA's
whole tool system stops working on it regardless of how much VRAM you have.
Check before pulling anything:

    ollama show <model>        # `tools` must appear under capabilities

On 4 GB, `qwen3:4b` is the most capable that fits, and
`NOVA_AI__OLLAMA_CONTEXT_TOKENS=8192` leaves room for its KV cache. The
default of 16384 is sized for a machine with more to spare.

## On the machine running NOVA

One line, and a restart:

    # .env
    NOVA_AI__CHAT_PROVIDER=ollama
    NOVA_AI__CHAT_MODEL=qwen2.5:7b
    NOVA_AI__OLLAMA_BASE_URL=http://<gpu-box>:11434

Nothing else changes. `ollama_base_url` is used exactly as given — there is
no loopback special case — so a LAN address, a `.local` name or a hostname
all work.

Two settings worth revisiting once the model is remote:

- `NOVA_AI__OLLAMA_CONTEXT_TOKENS` (default 16384). NOVA sends this as
  `num_ctx`; without it Ollama allocates whatever the model advertises,
  which is 131072 for llama3.2 and 262144 for qwen3.8 — enough to turn a
  2 GB model into 17.7 GB resident. With a GPU you have more room, but the
  ceiling is VRAM rather than system memory now.
- `NOVA_AI__OLLAMA_KEEP_ALIVE` (default 30m). On a dedicated box, longer is
  better: the machine has nothing else to do with the memory, and a warm
  model is the difference between a reply and a five-second stare.

## Checking it

From the machine running NOVA, not from the GPU box:

    curl http://<gpu-box>:11434/api/tags          # the model is listed
    curl http://localhost:8000/api/v1/system/status \
        -H "Authorization: Bearer <token>"        # ai.online is true

If `ai.online` is false, the `detail` field says which of the two it is: a
provider that cannot be reached, or one that did not answer inside the
dashboard's six-second probe budget. A cold load on a first request often
exceeds that — ask it something once from the GPU box before believing the
card.
