# Held-out test set

Three fictional TORs and one bid bulletin in `eval/test_set/`, written answer key first and frozen by hash in `manifest.json`. A test fails if any file changes. Karen skimmed them for realism on 2 Nov 2026 and accepted them.

| TOR | Format | Key features | Terms |
|---|---|---:|---|
| Alumni giving | PDF: Label: description bullets, no modal verbs | 15 | Healthy |
| Fleet maintenance | PDF: ruled matrix with bullets in its cells | 21 | Risky |
| Convention center, with a bid bulletin | Word: numbered clauses | 21 | Moderate |

## Limitations

- **Shorter than planned.** About 5 pages each, against the plan's ~6, ~8 and ~8. Kept at this length by decision. Requirements are denser than in a long real TOR, with less surrounding prose to tell apart from them.
- **Same author family.** Claude wrote the TORs and their keys, and the runtime model is a different one, but neither is a real client document.
- **Small.** Three TORs give an indication, not a statistic.
- **Extra use of the fleet RFP.** Beyond its baseline and one retry, its sections were sent once more to read Azure's error code, with the answers discarded unread. The cause then prompted a fix built on a dev TOR (I-18), and logged as such.
- **Stored guard findings for the fleet runs were read,** at Karen's request, without opening the file.
