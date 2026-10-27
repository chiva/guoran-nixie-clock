# Agent instructions (guoran-nixie-clock)

- Guoran nixie clock: a Web Bluetooth app (`web/`), a Home Assistant integration
  (`custom_components/guoran_nixie`) and the protocol docs (`docs/`). Everything is in English.
- The protocol lives twice, in `web/protocol.js` and `custom_components/guoran_nixie/protocol.py`.
  Change both together, and add the case to `docs/fixtures/protocol-vectors.json` first: both
  test suites read it.
- Facts not confirmed on hardware are marked "unverified" or "inferred" in `docs/PROTOCOL.md`.
  Keep them marked until a capture proves them.
- Checks: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`,
  `uv run pytest --cov` (90 % floor), `(cd web && npm test)`. Validate with hassfest before release.
- `strings.json` is copied to `translations/en.json`.
- `main` is protected: every change lands through a PR whose title is a Conventional Commit
  (it becomes the squash commit) and whose CI checks pass. Never push to `main`.
- Releases are release-please's: merging its release PR tags the version (manifest.json,
  web/package.json, the service worker cache), attaches `guoran_nixie.zip` for HACS and publishes
  the web app to GitHub Pages. `feat:` bumps the minor version, `fix:` the patch.
- release-please opens its PRs with `GITHUB_TOKEN`, so CI does not start on them and they are
  not the maintainer's: close and reopen the release PR to run the required checks, and it needs
  the code owner's approval before it can merge.
- Hardware facts go in `docs/PROTOCOL.md` marked **observed** (with model, name and date), or
  stay **inferred**/**unverified** until a test on a clock settles them.
- Only push or open PRs when the maintainer asks.
