<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/readme/banner-dark.svg">
    <img alt="Enacted — Virginia. What changed. What’s current. Who to ask." src="docs/readme/banner-light.svg" width="100%">
  </picture>
</p>

<p align="center">
  <img alt="Records: Markdown + YAML" src="docs/readme/badge-records.svg">
  <img alt="Provenance: SHA-256 per source" src="docs/readme/badge-provenance.svg">
  <img alt="Models in pipeline: none" src="docs/readme/badge-models.svg">
  <img alt="Checks: Python 3.12+" src="docs/readme/badge-python.svg">
  <a href="LICENSE-DATA"><img alt="Records licence: CC0 1.0" src="docs/readme/badge-license-data.svg"></a>
  <a href="LICENSE"><img alt="Code: MIT" src="docs/readme/badge-license-code.svg"></a>
</p>

# Enacted: Virginia records

This repository records the enacted law. It does not advise, approve, determine, or certify anything.

Nothing here is legal advice, and using it does not create an attorney-client relationship with anyone. The government source cited in each record controls: a record may have been amended, superseded or corrected since it was fetched. If a decision depends on a rule, read the cited source and, where it matters, ask the office named for that jurisdiction or a licensed professional there.

Browse these records on **[enacted.app](https://enacted.app/coverage/)**, or follow a county or city by feed at **[enacted.app/feeds](https://enacted.app/feeds/)**. Both are free.

## Contents

- [What's in it](#whats-in-it)
- [Coverage](#coverage)
  - [Water](#water)
  - [Zoning and land use](#zoning-and-land-use)
- [How a record is made](#how-a-record-is-made)
- [Checking the records yourself](#checking-the-records-yourself)
- [Reuse and licences](#reuse-and-licences)
- [Corrections](#corrections)
- [About](#about)


## What's in it

| Path | What it holds |
| :-- | :-- |
| `<jurisdiction>/` | Zoning and land-use records for that county or independent city, one Markdown file per instrument (ordinance, zoning ordinance amendment) |
| `<jurisdiction>/authority.yaml` | Who to ask: the office's public line, quoted from the government's own contact page, with that page's SHA-256 |
| `<jurisdiction>/SOURCING_BLOCKED.md` | Why a jurisdiction has no records yet |
| `water/measurements/<system>/` | Published drinking-water results for each water system (lead, copper), one file per parameter and year |
| `water/regulator.yaml` | The Virginia Department of Health's Office of Drinking Water line, from a saved, hash-checked page |
| `.cache/` | Saved copies of the contact and regulator pages, so their hashes can be recomputed |
| `coverage.json` | Every jurisdiction this repository tracks |
| `verify_corpus_integrity.py`, `tests/`, `scripts/` | The checks, and the deterministic tools that fetch records |

## Coverage

Each topic below has a short summary and the full list in a dropdown. The [coverage page](https://enacted.app/coverage/) shows the same thing on a map.

### Water

Arlington County Department of Environmental Services' published lead and copper results at customer taps.

<details>
<summary>All 2 water records</summary>

| Water system | Utility | Serves | Parameter | Period | Record |
| :-- | :-- | :-- | :-- | :-- | :-- |
| Willston Zone | Arlington County DES | Arlington County | Copper | 2022 | [record](water/measurements/arlington_des/copper_tap_2024.md) |
| Willston Zone | Arlington County DES | Arlington County | Lead | 2022 | [record](water/measurements/arlington_des/lead_tap_2024.md) |

</details>

### Zoning and land use

3 of the 9 Northern Virginia jurisdictions tracked here have records. Independent cities are jurisdictions in their own right.

<details>
<summary>All 9 jurisdictions</summary>

| Jurisdiction | Folder | Status |
| :-- | :-- | :-- |
| City of Alexandria | [`alexandria/`](alexandria/) | Records |
| Arlington County | [`arlington/`](arlington/) | Records |
| Fairfax County | [`fairfax_county/`](fairfax_county/) | Records |
| City of Fairfax | [`fairfax_city/`](fairfax_city/) | Not covered yet: code published only on Municode ([details](fairfax_city/SOURCING_BLOCKED.md)) |
| City of Falls Church | [`falls_church/`](falls_church/) | Not covered yet: code published only on Municode ([details](falls_church/SOURCING_BLOCKED.md)) |
| Loudoun County | [`loudoun/`](loudoun/) | Not covered yet: adopted amendments published on enCodePlus, code on Municode ([details](loudoun/SOURCING_BLOCKED.md)) |
| City of Manassas | [`manassas/`](manassas/) | Not covered yet: code published only on Municode ([details](manassas/SOURCING_BLOCKED.md)) |
| City of Manassas Park | [`manassas_park/`](manassas_park/) | Not covered yet: code published only on Municode ([details](manassas_park/SOURCING_BLOCKED.md)) |
| Prince William County | [`prince_william/`](prince_william/) | Not covered yet: code published only on Municode ([details](prince_william/SOURCING_BLOCKED.md)) |

</details>


Records are added and refreshed by deterministic fetch scripts run by hand. Scheduled fetch-and-check workflows are staged in `.github/workflows-pending/` and are not switched on yet, so a change at the source appears here only after the next run.

## How a record is made

- **From the government's own site.** Each record names its source (`primary_source_url`, and `source_pdf` where the instrument is a file), the SHA-256 of the exact file fetched (`source_sha256`) and when it was fetched, in UTC (`source_fetched_at`).
- **Quoted, not summarised.** Quoted text is copied mechanically from that file. Where a title or summary page differs from the enacted text, the enacted text controls and the difference is noted in the record.
- **No commercial code publishers.** Sites such as Municode, American Legal Publishing, eCode360 and enCodePlus are not used. When a jurisdiction's code is published only there, its folder holds a `SOURCING_BLOCKED.md` saying what was checked, and no records.
- **No news or commentary.** Articles and opinion pieces are never cited.
- **No AI models.** Nothing in fetching, building, comparing or sending these records calls a model.

## Checking the records yourself

```bash
pip install pyyaml pytest
python verify_corpus_integrity.py   # re-fetches cited sources, so it needs network access
python -m pytest tests
```

Every hash can also be checked by hand: download the file a record cites (or open the matching page in `.cache/`) and compare its SHA-256 with the record's `source_sha256`.

## Reuse and licences

- **Records: CC0 1.0** ([LICENSE-DATA](LICENSE-DATA)). The record files and their YAML metadata, the contact files, the feeds, `coverage.json` and the documentation are dedicated to the public domain. Use them for anything, with no permission or credit needed. If you do give credit, a link to the cited government source is the most useful kind.
- **Code: MIT** ([LICENSE](LICENSE)). `verify_corpus_integrity.py`, `scripts/`, `tests/` and the workflow files. Keep the copyright notice when you copy them.
- **Not covered by either.** Text quoted from a government document keeps whatever status it has at its source; the dedication covers what Swapp Technologies added (the selection, structure and metadata). The saved pages in `.cache/` belong to their publishers and are kept only so their hashes can be recomputed. The Enacted name, its mark and the images in `docs/readme/` are not licensed.

## Corrections

If you find an error (a wrong date, a broken source link, a quote that doesn't match its source), open an issue on this repository with the record's path and the source you checked. A correction goes through the same checks as every other record.

Enacted can't say whether or how a rule applies to a particular property, project or license. The office listed in that jurisdiction's `authority.yaml` can.

## About

Published by Swapp Technologies LLC (Maryland). The site that presents these records is [enacted.app](https://enacted.app).

Related repositories: [enacted-state-md](https://github.com/Swapp-Technologies-LLC/enacted-state-md) · [enacted-state-dc](https://github.com/Swapp-Technologies-LLC/enacted-state-dc) · [enacted-federal](https://github.com/Swapp-Technologies-LLC/enacted-federal)
