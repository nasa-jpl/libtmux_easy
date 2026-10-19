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

After `uv lock`, regenerate the bundle's pins with
`debian/update-dash-requirements` (CI checks they match `uv.lock`).

## Releases

1. Bump `pyproject.toml` and `debian/changelog`.
2. Push a `v*` tag.

CI then checks the three versions match, runs the tests, builds the wheel
and `.deb`s, and publishes them to GitHub Releases and PyPI.

## Development

```bash
uv sync                      # .venv: editable package, Textual, pytest
uv run pytest -q             # on a private tmux server: your sessions are safe
```

CI runs the suite on each supported Python, checks the PyPI metadata, and
builds and smoke-tests the `.deb`s.
