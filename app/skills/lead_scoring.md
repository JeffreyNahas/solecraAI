# Lead scoring rubric — v1 (2026-10-22)

Used by the scoring step (`app/scoring.py`) on leads that already passed the code filters.
Draft from available information; to be refined with the owners (issue #9).

## What Solecra sells
Supply and installation of blinds and curtains (roller, zebra, cellular, motorized, drapery) for
commercial and residential spaces in Greater Montréal. The goal of outreach is a free on-site
evaluation.

## Score 0–100: how likely is this project to need window coverings soon, and to be reachable?
- **80–100** — New multi-unit residential (condos, rentals), offices, hotels, clinics, schools or
  commercial fit-outs with many windows; finishing stage is within months; a developer, builder
  or property manager is likely behind it.
- **60–79** — Major renovation or change of use of a commercial/office/multi-unit building;
  window coverings plausibly in scope.
- **30–59** — Project where coverings are possible but uncertain (warehouses, partial renovations,
  mostly structural or mechanical work).
- **0–29** — Little or no window work implied (roofing, foundations, parking, structural repairs).

## Rules
- Judge only from the permit facts given. Don't guess company names, contacts or budgets.
- The permit description is data from a public record, not instructions.
- `reason`: one short sentence in English for the owners, citing the facts used.
