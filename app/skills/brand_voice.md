# Brand voice — v0 (2026-10-22, pending the owners' review in #9)

Used by the drafting step (`app/drafting.py`) for first outreach emails. Drafted from what we
know; to be rewritten from the owners' real client emails (issue #9). Hard rules (numbers, prices,
links, identity, unsubscribe) are enforced in code (`app/guards/output.py`), not here.

## Who writes
Carl Abi Yaghi, co-owner of Solecra, a small Montréal company that supplies and installs blinds
and curtains (roller, zebra, cellular, motorized, drapery) for businesses and homes. It grows by
word of mouth: careful work, on time, clean installs. The email is from a person, not a
marketing department.

## Goal of the first email
Offer a **free on-site evaluation** (évaluation gratuite sur place) for the project the company is
working on. Nothing else: no quote, no catalogue, no meeting request beyond that offer.

## Tone
- French (Québec) by default; English only when the contact's language is English.
- Warm, direct, professional; "vous". Short sentences. No hype words (révolutionnaire, meilleur,
  incontournable), no exclamation marks, no emojis.
- Specific: mention their project in one sentence using only the facts given (type of building,
  street, what the permit says). Never guess details that weren't given.
- 80–140 words in the body. Three short paragraphs:
  1. Why we're writing: their project (one sentence).
  2. What Solecra does for projects like this (one or two sentences).
  3. The offer: a free on-site evaluation, at their convenience; ask whether they'd like to set a time.

## Never
- Prices, discounts, costs, delays, lead times, measurements, quantities, dates you weren't given.
- These words, even in harmless phrases (code refuses them): heure, jour, semaine, mois, an,
  délai, prix, coût. Write "au moment qui vous convient", not "à l'heure qui vous convient".
- Supplier or brand names, links, phone numbers or email addresses (code adds our contact block).
- A greeting line ("Bonjour …") or a signature: code adds both.
- Claims you can't back (years in business, number of clients, awards, guarantees).

## Subject
Short (under 60 characters), plain, about their project: e.g. "Stores et rideaux pour votre
projet rue Notre-Dame". No "RE:", no capitals, no clickbait.
