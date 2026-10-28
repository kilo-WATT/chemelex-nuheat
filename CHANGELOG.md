# Changelog

## 0.1.4 - 2026-10-27

- Expose account-subject extraction and a typed invalid-identity error for OAuth
  integrations. Payload decoding does not authenticate the token, and errors
  omit token contents.

## 0.1.3 - 2026-10-24

- Separate permission denial from authentication expiry; optional v1 permission
  failures preserve conservative v2 reads.
- Honor Retry-After with runtime-client cooldown, without queued writes.
- Bound requests and nested operations, propagate cancellation and retain accepted
  PUT status on API readback failures. No new mode payloads or dependencies.

## 0.1.2

* Add an explicit, live-verified v1 Permanent Hold command with mandatory target.
* Optionally correlate v1 operating/schedule details with mode-3 v2 responses,
  distinguishing physical Manual from Auto with Permanent Hold.
* Preserve v2-only default behavior, authenticated request handling and identifiers.

## 0.1.1 - 2026-09-21

- Align derived thermostat states with corrected live OpenAPI v2 GET responses.
- Distinguish Auto, Hold, and Standby using the numeric mode and centi-Celsius
  target together.
- Preserve the unresolved overlap between physical Manual and permanent hold.

## 0.1.0 - 2026-07-31

- Add the initial typed asynchronous NuHeat API v2 client.
- Support account and thermostat reads, target-temperature writes, and Auto,
  Hold, and Manual modes.
- Convert API centi-Celsius values at the HTTP boundary.
- Add behavior derived from controlled live validation, including timed holds,
  physical Manual mode, and the frost-protected Standby command.
