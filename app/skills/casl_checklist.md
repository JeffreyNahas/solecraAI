# CASL checklist — v0 (2026-10-22, pending the owners' review in #9 and the legal review in #13)

Used by the drafting step as background. Every item marked (code) is enforced by code and cannot
be skipped by the model or an owner edit.

- Consent: implied by conspicuous publication. The address was published by or for the business
  on the page saved as evidence, with no "no solicitation" notice, and the message is relevant to
  their business role (research step, `app/guards/consent.py`). (code)
- The message must be relevant to that role: a building or fit-out project and window coverings.
- Identification: sender's name, "on behalf of Solecra", mailing address, and a phone, email or
  website, valid for at least 60 days. (code: footer added to every email)
- Unsubscribe: a link that works for at least 60 days, applied without delay (we apply it
  instantly). (code)
- No misleading subject or content; the subject describes the email honestly.
- One email per company; no follow-up after an unsubscribe or a reply. (code)
- French first (Bill 96); English only if the contact uses English. (code: language check)
