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

- **Atari Jaguar CD** (`atarijaguarcd`): `.cue`/`.cdi`/`.chd`, launched with BigPEmu or RetroArch's Virtual Jaguar core (for RetroAchievements).
- **Sony PlayStation 4** (`ps4`): `.ps4` serial files, launched with shadPS4.
- **Sony PlayStation 5** (`ps5`): `.ps5` files; no PC emulator yet.
- **Nintendo Switch 2** (`switch2`): `.xci`/`.nsp`/`.xcz`/`.nsz`, launched with Lichii (its only option).

Each launches RetroArch with a made-up core name (`bigpemu`, `shadps4`, `ps5`, `lichii`)
that the launch bridge turns into the PC emulator. iiSU replaces the device
copy when an iiSU update ships a newer console list (emuladores.version.txt),
so these entries then need adding again.

The border pack needs the same consoles, in
`/sdcard/Android/media/com.iisulauncher/iiSULauncher/assets/border_packs/default/border_pack.json`
(images go in the same folder):

```json
{ "console": "atarijaguarcd", "border": "atarijaguarcd.png" , "logo": "logo_atarijaguar.png" },
{ "console": "ps4", "border": "ps4.png" , "logo": "logo_ps4.png" },
{ "console": "ps5", "border": "ps5.png" , "logo": "logo_ps5.png" },
{ "console": "switch2", "border": "switch2.png" , "logo": "logo_switch2.png" },
```
