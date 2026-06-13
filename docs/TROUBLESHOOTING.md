# Troubleshooting

## First-time setup (the isolated test copy)

The harness never touches your real game install or saves. One-time setup:

1. Copy the app: `cp -R "$STS2_APP" ./sts2-test.app` (paths come from `config.sh`).
2. Drop the Steam appid next to the binary so it launches outside Steam's UI:
   `echo 2868840 > sts2-test.app/Contents/MacOS/steam_appid.txt` (Steam itself must be
   running and logged in).
3. Install the mod into the copy: `spirelink/build.sh` then
   `mkdir -p sts2-test.app/Contents/MacOS/mods/spirelink && cp spirelink/dist/spirelink/* sts2-test.app/Contents/MacOS/mods/spirelink/`.
4. Launch once (`./run_test.sh`) to create `settings.save`, then enable mod loading:
   set `mod_settings.mods_enabled = true` in
   `testhome/Library/Application Support/SlayTheSpire2/steam/<id>/settings.save`
   (the game gates mod-loading behind a first-run "mods warning" dialog that a
   headless instance can't click through).
5. Capture your hermetic baseline: `./make_home_template.sh`.

## Game crashes instantly at launch (arm64 .NET / `EXC_GUARD`)

On most Macs the game runs natively. On machines with SIP disabled and the
`-arm64e_preview_abi` boot-arg, the arm64 .NET runtime crashes at launch
(`EXC_GUARD` in CoreCLR's Mach exception thread). Workaround: run the x86_64
slice under Rosetta by putting this in `config.local.sh`:

```sh
SPIRELINK_ARCH="x86_64"
```

The clean fix is removing the boot-arg (`sudo nvram boot-args=...` without
`-arm64e_preview_abi`) and rebooting; native is faster and less timing-flaky.

## "Room type not assigned" on start_run

Run-start was slowed past an internal timeout — historically caused by over-broad
Harmony patches. Keep new patches narrow; never patch shared generic methods like
`Rng.NextItem<T>` (.NET shares ref-type generic code, so the patch lands on every
instantiation).

## Port 5555 already in use / "port is locked by another eval"

One instance per port. `bridge/instance.py` holds an exclusive flock per port and
refuses to kill non-game listeners. Run a second eval on another port:
`SPIRE_PORT=5556 ...` (the mod reads `SPIRELINK_PORT` from its environment, which
`launch_instance.sh` sets).

## Headless instance stalls on render/animation waits

Launch with a window: `STS_HEADLESS=0 ./run_test.sh`.

## Hermetic results differ across machines

Check the `template_sha256` recorded in each score: results are only comparable
against the same HOME template (run content depends on save meta-progression, not
just the seed). Also confirm the requested seed appears as `run_summary.seed` —
the scorer flags `seed_mismatch` if not.

## Regenerating the decompiled game source (local reference only)

`decomp/` is gitignored (proprietary game code) and only needed when extending the
mod. Regenerate from your own install:

```sh
docker run --rm -v "$STS2_GAME_DATA":/game:ro -v "$PWD/decomp":/out \
  mcr.microsoft.com/dotnet/sdk:8.0 bash -lc \
  'export PATH=$PATH:/root/.dotnet/tools;
   dotnet tool install -g ilspycmd --version 9.0.0.7889;
   ilspycmd -p -o /out/sts2_src /game/sts2.dll'
```
