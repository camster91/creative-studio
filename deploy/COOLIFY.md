# PhotoGen Coolify adoption

This configuration prepares a private recovery candidate. It is not a public
cutover or a complete main auto-deployment implementation.

## Recorded source and recovery

The inspected main revision is `b382aa35282d2aa02238e03a21dd5a5041ba3979`.
The healthy VPS app reports revision `ea4560831e503c6ce34d18739476a71ac6e9a9de`
and image `sha256:d1a9dc359b8576e22f81f3884b5909408a0b8de5977ca9087dd1ab7a8d578991`.
Do not confuse recovery of that preceding image with qualification of main.

Recovery at `/opt/retired-deployments/photogen-migration-20261003T025615999576Z`
contains a private container inspection, a consistent snapshot and independent
restore. Four SQLite databases (15 tables/16 rows) and all 20 files verified.
The original output directory was empty and remains preserved. Both source
mounts, `/root/photogen-data` and `/root/photogen-outputs`, remain untouched.

An isolated preceding-image rehearsal served `/api/whoami`, `/` and `/app`
with HTTP 200, denied anonymous `/api/costs` with HTTP 401, and left all database
table hashes unchanged. It had no network or provider credentials. This does
not establish account, browser, paid-generation or current-main compatibility.

## Prepare and validate the private candidate

1. Recheck capacity, the exact original image, source mounts and current revision.
   Source builds currently require lower disk use or an approved capacity exception.
2. Create `/opt/photogen-coolify/data` and `/opt/photogen-coolify/outputs`, privately
   copy a freshly verified snapshot, and assign ownership to the selected image's
   runtime UID/GID. The preceding image uses 1000:1000; verify each new image.
   Never point the candidate at the live source folders.
3. Import `deploy/coolify.yml` as a raw Compose resource. Supply an immutable
   locally available recovery image or verified registry digest as `PHOTOGEN_IMAGE`.
   Store separate synthetic candidate session/admin secrets only in Coolify's
   secret configuration. Never commit, export or print their values.
4. The service uses private loopback port 15173, an internal network, a read-only
   application filesystem, writable copied storage and a temporary `/tmp`.
   Missing bind directories fail rather than creating root-owned empty stores.
5. No provider, mail, Stripe or Figma credentials are included. Daily spend is
   zero. Verify health, private-state authorization, account/data compatibility,
   all stored assets and browser layouts before any promotion.

## Remaining release gates

CI Build (273235128) and Deploy Creative Studio (283004459) were manually disabled
when inspected. Re-enabling them requires the existing explicit workflow gate.
The latter performs SSH staging/production deployment; do not enable it merely
to obtain an image. Replace its deployment operations with the reviewed Coolify
flow before activation, preserving build/smoke requirements and production approval.

Before each real deployment, attach and exercise a helper-compatible backup guard
that verifies SQLite snapshots, stored files and the preceding image. Preserve
the current session/signing secrets privately for an eventual accepted cutover.
Current-main migration and preceding-version rollback must both be qualified on
copies before changing the live database or domain.

Public routing is a separate shared-proxy approval. Do not stop the original
container, alter its routes or transfer its live data ownership during candidate
qualification. Keep every Docker volume and verified rollback image.

Completion requires passing source/container/browser checks, a verified backup
guard, an approved domain cutover, HTTPS/health/revision checks and an observed
signed main deployment event. This candidate configuration alone proves none
of those outstanding release gates.
