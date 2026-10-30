## What this changes

<!-- e.g. support for model XYZ, with the requests recorded from its web page -->

## Checklist

- [ ] Every request sent is one the adapter's web page was seen sending, with the same field grouping
- [ ] Nothing automates unpairing, factory reset, firmware upgrade or password change
- [ ] Tests assert the exact requests sent; fixtures are anonymised
- [ ] `pytest`, `ruff check` and `ruff format --check` pass
- [ ] User-facing strings are translatable, with every translation file in sync
