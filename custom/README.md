# Saved working config

`config.json` here is a copy of the working `bridge/config.json` from the
Windows install this fork is used on: ROM and emulator folders, display,
hotkeys, the emulator for each Android package (Eden for Switch, Gopher64
for N64, Supermodel in the NEO.emu slot, and so on) and the VM profile.

The real file lives at `bridge/config.json`, which git ignores and updates
never overwrite. This copy is only a backup: after a fresh install, copy it
to `bridge/config.json` to get the same setup back.

# Extra consoles for iiSU

`extra_consoles.json` holds the consoles added by hand to iiSU's console
list, which iiSU reads from the VM at
`/sdcard/Android/media/com.iisulauncher/iiSULauncher/Emuladores/emuladores.json`
(a copy of the APK's `emuladores_default.json`, made on first start):

- **Atari Jaguar CD** (`atarijaguarcd`): `.cue`/`.cdi`/`.chd`, launched with BigPEmu.
- **Sony PlayStation 4** (`ps4`): `.ps4` serial files, launched with shadPS4.
- **Sony PlayStation 5** (`ps5`): `.ps5` files; no PC emulator yet.

Each launches RetroArch with a made-up core name (`bigpemu`, `shadps4`, `ps5`)
that the launch bridge turns into the PC emulator. iiSU replaces the device
copy when an iiSU update ships a newer console list (emuladores.version.txt),
so these entries then need adding again.
