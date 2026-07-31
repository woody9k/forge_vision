# Deploying Forge Vision

For running the platform on a machine. If you want to *use* the API, read
[AGENTS.md](AGENTS.md) and [API.md](API.md); if you want to change the code,
read [../CLAUDE.md](../CLAUDE.md).

Everything here is driven by one script:

```bash
deploy/forge-vision preflight | start | stop | restart | status | health | logs | backup
```

## What kind of thing you are deploying

Four properties decide almost every operational question:

1. **One process, and only one.** A single handle may claim the Pluto's USB
   interface, so a second instance cannot reach the radio — it will start,
   serve the UI, and quietly fail every capture. There is no `--workers`
   option and no horizontal scaling. If you need more throughput, that is a
   hardware problem, not a deployment problem.
2. **It is stateful, and the state is the point.** Raw captures are immutable
   by design. `$FORGE_VISION_DATA` (default `~/.forge-vision`) is the only
   thing that cannot be rebuilt from git. Back it up; never point a fresh
   deployment at a scratch disk.
3. **It builds its `Runtime` at import time**
   ([app.py:22](../forge_vision/server/app.py)). `FORGE_VISION_DATA` must be
   exported *before* the process starts, and an import error is a startup
   failure rather than a request failure. `preflight` imports the app for
   exactly this reason.
4. **It can key a transmitter.** See below.

## Exposure — read this before binding to a network

**There is no authentication on this API.** No tokens, no basic auth, no
CORS restriction, no allowlist. Every endpoint is reachable by anyone who can
reach the port.

Among those endpoints are `POST /api/safety/arm` and
`POST /api/devices/{id}/tx`. The `SafetyController` is an interlock against
*mistakes* — a checklist, an arm step, RX-port protection, a `try/finally`
around every TX path. It is **not an authorization boundary**. It will
happily let a stranger arm the platform and key the radio, because it cannot
tell a stranger from you.

So the default bind is `127.0.0.1`. To reach the UI from another machine,
choose deliberately:

- **Preferred — SSH tunnel.** Nothing is exposed, and it works from anywhere:
  ```bash
  ssh -N -L 8347:127.0.0.1:8347 user@bench-host
  ```
  Then open `http://127.0.0.1:8347` on your laptop.
- **Acceptable on a trusted lab network** — set `FORGE_VISION_HOST=0.0.0.0`
  in `deploy/forge-vision.env`. `preflight`, `start` and `status` will warn
  every time. Pair it with a firewall rule that limits the source:
  ```bash
  sudo ufw allow from 192.168.99.0/24 to any port 8347
  ```
- **Never** on a network you do not control, and never port-forwarded from
  the internet.

Transmitting into an unterminated port can damage the radio, so this is not
only a data-integrity question.

## First deployment

### Prerequisites

`preflight` can only run once there is a venv to run it in, so this part is
on you. **Python 3.10 is a hard floor** — `server/schemas.py` is Pydantic, and
Pydantic resolves annotations at model-build time, so `float | None` is
evaluated at runtime even under `from __future__ import annotations`. Ubuntu
22.04 (3.10) and 24.04 (3.12) are fine; 20.04 ships 3.8 and will not work
without a newer interpreter.

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
```

`python3-venv` is a separate package on Debian and Ubuntu, and without it
`python3 -m venv` fails with a message about `ensurepip` rather than anything
about the missing package. It is the single most common first-run failure.

### Install

The repository is public, so no credentials are needed:

```bash
git clone https://github.com/woody9k/forge_vision.git
cd forge_vision
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp deploy/forge-vision.env.example deploy/forge-vision.env   # then edit
deploy/forge-vision preflight
deploy/forge-vision start
deploy/forge-vision health
```

`preflight` is the whole checklist in one command — interpreter, dependencies,
a real import of the app, data directory and free space, port availability,
bind exposure, radio support, and whether the tree you are about to deploy
actually matches `origin/main`. It exits non-zero on anything fatal, so it
works in a pipeline:

```bash
deploy/forge-vision preflight && deploy/forge-vision restart
```

### Physical radio

Optional; the platform runs against simulated devices without any of this.

```bash
sudo apt install -y libiio-utils          # provides libiio itself
.venv/bin/pip install pyadi-iio pylibiio  # Python bindings
iio_info -s                               # should list the board
```

**USB permission is the step that actually bites.** The `usb:` backend opens
the raw node under `/dev/bus/usb/`, which is `0664 root:root` by default —
not writable by you. Being in `plugdev` does nothing on its own; the group
only matters if a udev rule assigns the device to it:

```bash
sudo tee /etc/udev/rules.d/53-adi-plutosdr-usb.rules >/dev/null <<'RULE'
SUBSYSTEM=="usb", ATTRS{idVendor}=="0456", ATTRS{idProduct}=="b673", MODE="0664", GROUP="plugdev"
RULE
sudo usermod -aG plugdev "$USER"
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Group changes need a fresh login. `preflight` tests this properly — it finds
the Pluto's node and tries to open it read/write, rather than checking group
membership and assuming.

A host can also have a writable node for unrelated reasons (a broad rule from
some other package), in which case it works with no Pluto rule at all. That
is luck, not provisioning, and it does not survive a rebuild.

### Vector network analyser (NanoVNA)

Optional; used to characterise antennas and cables from the Hardware page.
Needs `pyserial`, which is in `requirements.txt` — no system packages.

The instrument enumerates as an STM32 CDC-ACM port at `/dev/ttyACM0`, which
Ubuntu creates as `root:dialout 0660`. **The deployment user is typically not
in `dialout`**, so the server gets `EACCES` and the UI reports the instrument
as undetected. Unlike the Pluto rule above, granting to `plugdev` avoids a
re-login *and* a restart when the service account already carries that group:

```bash
sudo tee /etc/udev/rules.d/60-nanovna.rules >/dev/null <<'RULE'
SUBSYSTEM=="tty", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="5740", ATTRS{product}=="NanoVnaPro Virtual ComPort", GROUP="plugdev", MODE="0660", SYMLINK+="nanovna"
RULE
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=tty --action=change
```

Check with `ls -l /dev/nanovna` — the symlink is created by the rule, so its
presence proves the rule matched. `GET /api/vna/discover` then probes the port
and reports what actually answered.

`0483:5740` is STMicroelectronics' generic virtual COM port and is shared with
many unrelated STM32 boards, hence the `product` match. A different VNA model
will report a different product string; check with
`udevadm info -a -n /dev/ttyACM0 | grep product` and adjust, or drop the
`ATTRS{product}` clause if this host has no other STM32 serial devices.

### `usb:` or `ip:192.168.2.1` — choosing a backend

A Pluto on USB is a *composite* device: the radio and a USB-ethernet gadget
are one piece of hardware on one cable (`0456:b673`, one devnum). The gadget
gives the board `192.168.2.1` and this host `192.168.2.2`, so there are two
ways to reach the same radio over the same wire. Neither touches your LAN.

The difference that matters operationally is **exclusivity**:

```
# with the platform running and holding the radio:
iio_info -u ip:192.168.2.1   ->  IIO context created with network backend
iio_info -u usb:1.8.5        ->  ERROR: Unable to claim interface: Device or
                                 resource busy (16)
```

`usb:` claims the USB interface exclusively — while the platform holds it,
nothing else can talk to the radio at all. `ip:` reaches `iiod` on the board,
which serves several clients at once, so `iio_info`, `iio_attr` and other
diagnostics keep working alongside a running deployment.

| | `usb:` | `ip:192.168.2.1` |
|---|---|---|
| udev rule needed | yes | no |
| diagnostics while deployed | no — exclusive | yes |
| survives replug | yes (bare `usb:`) | yes |
| two Plutos on one host | ambiguous | ambiguous (both are `192.168.2.1`) |
| extra layers | none | RNDIS + TCP |

`usb:` is the shorter path and the default here. Prefer `ip:192.168.2.1`
when you want to keep a diagnostic shell on the radio while the platform
runs, or when you would rather not install a udev rule. They are the same
board, so register one, not both.

## Running it as a service

For a bench that should come back after a reboot:

```bash
deploy/forge-vision install-service
systemctl --user daemon-reload
systemctl --user enable --now forge-vision
```

User units stop at logout unless you enable lingering:

```bash
sudo loginctl enable-linger $USER
```

Once systemd owns the process, manage it with `systemctl`, not with this
script's `start`/`stop` — two supervisors will fight over the port. `status`,
`health`, `preflight` and `backup` stay useful either way.

## Upgrading

```bash
deploy/forge-vision backup            # state first; it is the irreplaceable part
deploy/forge-vision stop
git pull
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/     # ~265 tests, ~3.5 min
deploy/forge-vision preflight
deploy/forge-vision start
deploy/forge-vision health
```

If the API changed, regenerate the client-facing docs against the running
instance and commit the result:

```bash
.venv/bin/python tools/gen_api_docs.py
```

**Check that what you are deploying is what you think it is.** `preflight`
reports commits on `origin/main` that are missing from your tree, and warns
on uncommitted files. A merged pull request is not proof the code is on
`main`: a commit pushed to a branch *after* its PR merged stays behind, and
that has happened twice in this repo's history.

## Rolling back

Code rolls back cleanly; data does not, and does not need to.

```bash
deploy/forge-vision stop
git checkout <previous-good-sha>
.venv/bin/pip install -r requirements.txt
deploy/forge-vision start
```

Captures written by the newer version stay readable — derived products carry
their stage versions and parameters, so an older build can tell that a
product came from a pipeline it does not know, rather than misreading it.
Restore a data archive only if the directory is actually damaged:

```bash
tar -xzf backups/forge-vision-<stamp>.tar.gz -C "$(dirname "$FORGE_VISION_DATA")"
```

## Backups

```bash
deploy/forge-vision backup [dest]     # default ./backups, gitignored
```

Archives `$FORGE_VISION_DATA` minus `run/` and `logs/`. Captures accumulate
and are never rewritten, so an occasional full archive is enough — there is
no incremental-consistency problem to solve. Copy it off the machine; a
backup on the same disk is not a backup.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `start` says already running | Correct behaviour — one instance only. `status` shows the pid. |
| Starts, but every capture fails on real hardware | A second process holds the USB handle. `ss -lntp` and `ps -ef \| grep forge_[v]ision`. |
| `port 8347 already in use by pid N` | Something else, or a hand-started `uvicorn`. Stop it by pid. |
| Import error in `preflight` | Dependency drift — re-run `pip install -r requirements.txt`. The app cannot start at all in this state. |
| UI controls silently do nothing | Stale `app.js` against fresh `index.html`. Hard-reload; the server already sends no-cache headers. |
| Device unreachable after unplug/replug | `connect()` is idempotent but the handle is stale. `restart`. |

Do **not** stop the server with `pkill -f 'uvicorn forge_vision'` — the
pattern matches the killing shell's own command line. `deploy/forge-vision
stop` uses a pidfile and verifies `/proc/<pid>/cmdline` before signalling, so
it cannot kill itself or a recycled pid.

## Logs

```bash
deploy/forge-vision logs [lines]      # tails $FORGE_VISION_DATA/logs/server.log
```

Nothing rotates this file yet. It grows slowly — request logging only — but
on a long-lived bench, add it to `logrotate`.
