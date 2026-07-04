# jobs-assistant — TODO

Active project tracker. Not for archive content.

## Known improvements

- [ ] Add live Playwright dry-run (observe + resolve + execute against a real URL).
- [ ] Wire `import-feed` into a real TheirStack sync pipeline for tested job ingestion.
- [ ] Add `--db` path CLI flag defaults in a settings/config module for env-free setup.
- [ ] Publish observer/resolver/executor/runner guidance to `skills/` for agent workers.
- [ ] Write container-aware test run target in pyproject.toml for CI and pre-push.
- [ ] Add integration smoke: init-db + import-feed + sample-failures in one script.
- [ ] Dedupe and merge the archived `archive/old-scraper/` README and TODO into this tracker.

## Housekeeping

- Verify all archived scripts/references are clearly marked read-only.
- Remove any remaining stale `scraper/`-era environment variable baggage.
- Add a disposable local HTML/browser Playwright smoke for observe → resolve → execute → stop at final submit.