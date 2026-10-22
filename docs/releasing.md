# Releasing a new version

A release is a `v*` tag pushed to GitHub. Merging into `main` publishes nothing.
CI details: [packaging.md](packaging.md#ci-githubworkflows).

## 1. Bump the version (on a branch)

Two files hold it, and they must match (the release checks):

| file | change |
|---|---|
| `pyproject.toml` | `version = "0.3.0"` |
| `debian/changelog` | a new entry on top (below) |

```
libtmux-easy (0.3.0) noble; urgency=medium

  * What changed, one bullet each.

 -- Spencer Diehl <docdiehl@gmail.com>  Mon, 02 Nov 2026 10:00:00 -0800
```

- Date: `date -R`. Two spaces between the email and the date.
- Or `dch -v 0.3.0 "What changed"` (`sudo apt install devscripts`).

## 2. Refresh the lock and the dashboard bundle

```bash
uv lock                              # the lock records the version too
uv lock --upgrade                    # optional: newer dependency pins
debian/update-dash-requirements      # always, after any uv lock
uv run pytest -q
```

CI fails if `uv.lock` or `debian/dash-requirements.txt` is out of date.

## 3. PR and merge

```bash
git commit -am "Release 0.3.0"
git push
```

Open a PR and merge it once CI is green.

## 4. Optional: dry run to TestPyPI

Actions -> Release -> Run workflow (branch `main`).

- Runs the version check and CI, then uploads to test.pypi.org only.
- TestPyPI refuses a version it already has: skip this, or use `0.3.0.dev1`
  for the dry run.

## 5. Tag

```bash
git checkout main && git pull
git tag v0.3.0
git push origin v0.3.0
```

The Release workflow then:

1. checks the tag, `pyproject.toml` and `debian/changelog` agree;
2. runs all of CI;
3. creates the GitHub Release (sdist, wheel, both `.deb`s);
4. waits for your approval, then uploads to PyPI.

## 6. Approve and check

- Actions -> the Release run -> Review deployments -> approve `pypi`.
- `pipx install --force 'libtmux-easy[dash]'`, then `lte-run true`.
- The GitHub Release page lists both `.deb`s.

## Version numbers

| change | bump | example |
|---|---|---|
| fixes only | patch | 0.2.0 -> 0.2.1 |
| new features, or breaking changes before 1.0 | minor | 0.2.1 -> 0.3.0 |

PyPI never accepts the same version twice, even after deleting it.

## If something fails

| symptom | fix |
|---|---|
| `version` job fails | the tag and the two files disagree: fix the files, merge, then retag |
| a CI job fails after tagging | fix on `main`; delete the tag (`git push --delete origin v0.3.0; git tag -d v0.3.0`) and tag again. Only before the PyPI upload |
| PyPI upload fails after the GitHub Release | rerun the failed `pypi` job from the run page |
| bad release already on PyPI | release a new patch version; "yank" the bad one on pypi.org |
