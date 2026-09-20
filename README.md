# Enacted: Virginia Legal Repository (`enacted-state-va`)

This repository contains verified primary-source records of enacted Virginia land-use, zoning, and building law across Northern Virginia jurisdictions, independent cities, and public water quality measurement records.

**Parent Entity:** Swapp Technologies LLC (Organized in Maryland)  
**Repository Scope:** Virginia Land-Use, Municipal Zoning Law & Public Water Quality (DMV Launch Region)  
**Posture:** This repository records the enacted law. It does not advise, approve, determine, or certify anything.  

---

## 1. What This Repository Is

Enacted records what changed, when it took effect, and who to ask:
1. **Verbatim Statutory & Measurement Records**: Quoted provisions mechanically extracted from primary enacted instruments and official agency reports.
2. **Deterministic Version History**: Structured changes and legislative chronologies with exact effective dates.
3. **Primary Municipal Contacts**: Official municipal planning, zoning, and utility departments responsible for administering each local jurisdiction.

### Standard Verification Line
*This repository records the enacted law. It does not advise, approve, determine, or certify anything.*  
All text quotes, reports, shows, and records primary government documents. It does not provide legal advice or apply law to specific facts.

---

## 2. Sourcing Standards & Aggregator Blocklist

All records in this repository are derived strictly from primary government portals (Arlington County Board portal, municipal Legistar/Granicus portals, and official agency reports). Virginia independent cities are treated as first-class jurisdictions.

### Aggregator Hard Blocklist
Under Swapp Technologies LLC record integrity rules, third-party code aggregators are strictly blocklisted:
* **Municode** (`municode.com` / CivicPlus)
* **American Legal Publishing** (`amlegal.com`)
* **eCode360** (`ecode360.com` / General Code)

Jurisdictions whose codes are accessible only through blocklisted third-party aggregators contain a `SOURCING_BLOCKED.md` file detailing the evaluation and remain unpopulated.

---

## 3. Ingestion Conventions & Multi-Topic Topology

* **Multi-Topic State Repository**: Repositories are organized per state (`enacted-state-va`) containing all topics for that state (Land-Use/Zoning, Public Water Quality).
* **Measurement Records**: Public water measurement records live under `water/measurements/<system_slug>/<parameter>_<year>.md` with explicit per-system attribution, governing limits, compliance dates, and basis.
* **Primary Document Hashes**: Every record file includes the full 64-character SHA-256 hash of the source document fetched at generation time (`source_sha256`).
* **Fetch Timestamps**: Every record file includes the actual wall-clock ISO 8601 UTC timestamp of retrieval (`source_fetched_at`).
