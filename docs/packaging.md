# Packaging and releases

The README has the short install table. This is the rest.

## What each install gives

| install | library | commands | `lte-dash` |
|---|---|---|---|
| `python3-libtmux-easy` `.deb` | system Python (ROS) | all but `lte-dash` | no |
| `libtmux-easy-dash` `.deb` | - | `lte-dash`, its own Textual | yes |
| `pipx install 'libtmux-easy[dash]'` | no (private venv) | all | yes |
| `pip install 'libtmux-easy[dash]'` in a venv | yes | all, in the venv | yes |
| `uv sync` in a checkout | editable `.venv` | all | yes |

- One `pyproject.toml` drives the wheel and the `.deb`s, so the build uses
  setuptools (packaged for Ubuntu 24.04) and apt-only dependencies.
- Textual is the `[dash]` extra, not a dependency: apt's is too old. The
  `libtmux-easy-dash` `.deb` bundles a pinned, hash-checked copy
  (`debian/dash-requirements.txt`) in a private directory, used by
  `lte-dash` only. `dash.launcher()` picks this interpreter if it has
  Textual >= 1.0, else an installed `lte-dash`.
- Mixing the `.deb` library with pipx commands works; install the same version of both.
- ROS: make a venv with `python3 -m venv --system-site-packages`, so it sees `rclpy`.

## Without root, venv or pipx

```bash
sudo apt install python3-libtmux python3-psutil python3-packaging   # once, if allowed
pip install --user --break-system-packages --no-deps libtmux-easy
```

- `--user` writes only `~/.local` (commands in `~/.local/bin`); never `/usr`.
- The risk is shadowing: PyPI copies of libtmux, psutil etc. in `~/.local`
  win over apt's for every `python3` program. `--no-deps` plus the apt
  packages avoids that; drop `--no-deps` if apt isn't an option.
- `[dash]` adds Textual and rich to `~/.local`; prefer pipx for `lte-dash`.
- Undo: `pip uninstall --break-system-packages libtmux-easy`.

## Building the `.deb`s

```bash
sudo apt install build-essential debhelper dh-python python3-all python3-pip \
                 python3-setuptools pybuild-plugin-pyproject
dpkg-buildpackage -us -uc -b        # needs network: fetches the bundled Textual
sudo apt install ../python3-libtmux-easy_*_all.deb ../libtmux-easy-dash_*_all.deb
debian/smoke-test
```

## Pinned versions (`uv.lock`)

`pyproject.toml` gives ranges (`libtmux>=0.17`). `uv.lock` pins the exact
version and hash of every package, per Python (3.8 gets older libtmux,
Textual and pytest than 3.11+).

| who | gets |
|---|---|
| `uv sync` in a checkout, CI | exactly `uv.lock` |
| `libtmux-easy-dash` `.deb` | `uv.lock`'s Textual and its deps, via `debian/dash-requirements.txt` |
| `python3-libtmux-easy` `.deb` | apt's libtmux, psutil, packaging (not the lock) |
| pip / pipx from PyPI | newest versions in the ranges; the lock isn't published |

The lock changes only when you run `uv lock`. CI fails if it is stale.

## When to run what locally

| you changed | run, then commit the results |
|---|---|
| dependencies in `pyproject.toml` | `uv lock && debian/update-dash-requirements` |
| nothing, want newer pins (fixes, before a release) | `uv lock --upgrade && debian/update-dash-requirements` |
| `requires-python` (e.g. drop 3.8) | `uv lock && debian/update-dash-requirements`; update the CI matrix |
| the version | `pyproject.toml` and `debian/changelog` (`dch -v X.Y.Z`), then `uv lock` |
| `debian/` packaging | the build above, plus `debian/smoke-test` |

## CI (`.github/workflows/`)

`ci.yml` runs on every push and pull request:

| job | does | fails when |
|---|---|---|
| `test` | `uv sync --locked`, then pytest, on Python 3.8, 3.10, 3.12, 3.13; plus libtmux 0.30.2 (Ubuntu 24.04's apt) and 0.17.0 (oldest supported) swapped in | a test fails; `uv.lock` is stale |
| `dist` | checks `dash-requirements.txt` against `uv.lock`; `uv build` (sdist, wheel); `twine check --strict` | pins out of date; metadata won't render on PyPI |
| `deb` | `dpkg-buildpackage`; installs both `.deb`s (with apt's old `python3-textual`) and runs `debian/smoke-test` | build fails; commands or the dashboard don't work as installed |

`dist` and `deb` upload their files as workflow artifacts.

`release.yml` runs on a `v*` tag:

1. `version`: tag, `pyproject.toml` and `debian/changelog` must match.
2. `ci`: all of `ci.yml`.
3. `github`: a GitHub Release with the sdist, wheel and both `.deb`s.
4. `pypi`: uploads the sdist and wheel (trusted publishing, no token).

Run by hand (Actions, Release, Run workflow), it is a dry run: steps 1-2,
then an upload to TestPyPI instead of 3-4.

## Releases

Step by step: [releasing.md](releasing.md).

## Development

```bash
uv sync                      # .venv: editable package, Textual, pytest
uv run pytest -q             # on a private tmux server: your sessions are safe
```
