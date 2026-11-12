"""
Applies the Community-iiSU-PC interposer patch to a copy of the iiSU APK: decompiles
it, injects the iiSU-PC bridge classes (smali_patch/), redirects its
ROM-launch startActivity call sites to LaunchBridge.launch(), rebuilds,
zipaligns, and signs the result with a freshly-generated debug keystore.

This never bundles or redistributes iiSU's own APK. It operates on a copy
supplied by whoever runs it, the same relationship any APK-patching /
modding tool has to the app it patches.

The patch is anchored on a literal log string ("ROM launch attempt
package=") rather than hardcoded register names or line numbers, since
those are compiler-chosen and can differ between iiSU builds. It only
touches startActivity(Intent) calls inside the single method that contains
that log string, not just anywhere in the file, since MainActivity has many
unrelated startActivity calls elsewhere, see find_enclosing_method().

As of iiSU Alpha 7.5-prerelease1, LOG_ANCHOR no longer exists at all, that
build moved the actual dispatch out of MainActivity entirely into its own
single-purpose "GameLaunchCoordinator" class (confirmed live via adb
logcat while launching a real game: MainActivity now only logs "Tracking
launched package display=..." *after* the real launch already happened
elsewhere). That class's own obfuscated name changes every rebuild (seen
as "rw2" in 7.5-prerelease1) same as je6/PrimaryHomeActions below, so
find_game_launch_coordinator_smali() locates it the same way: by its
content (the literal "GameLaunchCoordinator" log tag plus an actual
startActivity call, since a sibling class references that same tag in
log messages without ever dispatching anything itself). Once found, every
startActivity call in that file is fair game to redirect, unlike
MainActivity, this class exists for no other purpose than coordinating
one game launch. find_main_activity_smali()/LOG_ANCHOR is tried first for
older iiSU builds that still have it; find_game_launch_coordinator_smali()
is the fallback once it's gone.

7.5-prerelease2 needed no change to any of the above (the coordinator was
"ax2", the PrimaryHomeActions holder "jq6"); what did break were the
obfuscated iiSU class names hardcoded in MediaBridgeReceiver.smali, so
those are now discovered at patch time too (find_asset_helper_class()).
"""

import re
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

from jre_env import java_exe, java_subprocess_env

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.platform_compat import subprocess_creationflags

SCRIPT_DIR = Path(__file__).parent
SMALI_PATCH_DIR = SCRIPT_DIR / "smali_patch"
TOOLS_DIR = SCRIPT_DIR / "tools"
APKTOOL_JAR = TOOLS_DIR / "apktool.jar"

LOG_ANCHOR = "ROM launch attempt package="
GAME_LAUNCH_COORDINATOR_LOG_TAG = "GameLaunchCoordinator"
# Register operands can be either v (local) or p (parameter) registers, a
# method that receives its Context/Intent as its own parameters (confirmed
# live in the GameLaunchCoordinator fallback path: a no-arg method still
# uses "p0" there, register reuse once the original "this" value is no
# longer needed is legal Dalvik bytecode) can pass them straight through
# to startActivity without ever copying them into locals first.
STARTACTIVITY_RE = re.compile(
    r"^(\s*)invoke-virtual \{([vp]\d+), ([vp]\d+)(?:, ([vp]\d+))?\}, "
    r"Landroid/(?:content/Context|app/Activity);->startActivity\(Landroid/content/Intent;(?:Landroid/os/Bundle;)?\)V\s*$"
)
BRIDGE_PACKAGE_SMALI_DIR = "com/iisulauncher/pcbridge"
LAUNCH_BRIDGE_CLASS = "Lcom/iisulauncher/pcbridge/LaunchBridge;"


def redirect_start_activity(match: re.Match) -> str:
    """The LaunchBridge.launch() call that replaces one startActivity call.
    The options Bundle, when the original passed one, goes along too: the PC
    bridge can hand a launch back (an Android app such as Chrome or the Play
    Store), and LaunchBridge then makes the original call with it."""
    indent, v_ctx, v_intent, v_options = match.groups()
    if v_options is None:
        return (
            f"{indent}invoke-static {{{v_ctx}, {v_intent}}}, "
            f"{LAUNCH_BRIDGE_CLASS}->launch(Landroid/content/Context;Landroid/content/Intent;)V\n"
        )
    return (
        f"{indent}invoke-static {{{v_ctx}, {v_intent}, {v_options}}}, "
        f"{LAUNCH_BRIDGE_CLASS}->launch(Landroid/content/Context;Landroid/content/Intent;Landroid/os/Bundle;)V\n"
    )


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    # Every command this wraps (java -jar apktool, zipalign, apksigner.bat)
    # is console-subsystem; this runs from the GUI's Setup flow
    # (pythonw.exe, no console of its own), so without CREATE_NO_WINDOW
    # each one flashes its own window during patching.
    kwargs.setdefault("creationflags", subprocess_creationflags())
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({' '.join(args)}):\n{result.stdout}\n{result.stderr}")
    return result


def validate_iisu_apk(apk_path: Path) -> None:
    """Cheap pre-check that apk_path is actually iiSU, so a wrong file gets
    rejected in under a second instead of after a multi-GB SDK download and
    a full apktool decompile (the point where find_main_activity_smali()
    would otherwise be the first thing to notice). Searches the raw dex
    bytes for either of the two anchors the real patch can use (see
    module docstring), an ASCII string constant lands in the dex's string
    pool as contiguous UTF-8 bytes, so a plain byte search finds it
    without decompiling anything."""
    if not zipfile.is_zipfile(apk_path):
        raise RuntimeError(f"{apk_path} is not a valid APK (not a zip file).")

    anchor_candidates = [LOG_ANCHOR.encode("utf-8"), GAME_LAUNCH_COORDINATOR_LOG_TAG.encode("utf-8")]
    with zipfile.ZipFile(apk_path) as z:
        dex_names = [n for n in z.namelist() if re.fullmatch(r"classes\d*\.dex", n)]
        if not dex_names:
            raise RuntimeError(f"{apk_path.name} has no classes.dex, it doesn't look like a valid Android APK.")
        dex_contents = [z.read(name) for name in dex_names]
        found = any(anchor in content for content in dex_contents for anchor in anchor_candidates)

    if not found:
        raise RuntimeError(
            f"{apk_path.name} doesn't look like iiSU, couldn't find its ROM-launch code in it. "
            "Double check this is the right APK (this tool only patches iiSU itself)."
        )


def decompile(apk_path: Path, out_dir: Path) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    run([java_exe(), "-Xmx2g", "-jar", str(APKTOOL_JAR), "d", "-f", str(apk_path), "-o", str(out_dir)], env=java_subprocess_env())


def build(decompiled_dir: Path, out_apk: Path) -> None:
    run([java_exe(), "-Xmx2g", "-jar", str(APKTOOL_JAR), "b", str(decompiled_dir), "-o", str(out_apk)], env=java_subprocess_env())


def find_main_activity_smali(decompiled_dir: Path) -> Path:
    candidates = [p for p in decompiled_dir.rglob("MainActivity.smali") if LOG_ANCHOR in p.read_text(encoding="utf-8", errors="replace")]
    if not candidates:
        raise RuntimeError(
            "Could not find a MainActivity.smali containing the ROM-launch log anchor "
            f"({LOG_ANCHOR!r}). iiSU's code has likely changed since this patch was written "
            "and it needs to be updated by hand."
        )
    return candidates[0]


def find_enclosing_method(lines: list[str], line_index: int) -> tuple[int, int]:
    """Returns (start, end) line indices of the .method ... .end method
    block containing line_index, so the patch only touches startActivity
    calls within that one method, not anywhere else in this very large
    file, which has many unrelated startActivity calls."""
    start = line_index
    while start >= 0 and not lines[start].lstrip().startswith(".method"):
        start -= 1
    if start < 0:
        raise RuntimeError("Could not find the enclosing .method for the ROM-launch anchor.")
    end = line_index
    while end < len(lines) and not lines[end].lstrip().startswith(".end method"):
        end += 1
    if end >= len(lines):
        raise RuntimeError("Could not find the .end method closing the ROM-launch method.")
    return start, end


def patch_main_activity(path: Path) -> int:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)

    anchor_index = next((i for i, line in enumerate(lines) if LOG_ANCHOR in line), None)
    if anchor_index is None:
        raise RuntimeError(f"Anchor {LOG_ANCHOR!r} disappeared between the file-level check and patching, this shouldn't happen.")

    method_start, method_end = find_enclosing_method(lines, anchor_index)

    patched = 0
    for i in range(method_start, method_end):
        match = STARTACTIVITY_RE.match(lines[i])
        if not match:
            continue
        lines[i] = redirect_start_activity(match)
        patched += 1

    if patched == 0:
        raise RuntimeError(
            "Found the ROM-launch anchor and its enclosing method, but no startActivity(Intent) "
            "call inside it to redirect. iiSU's code has likely changed and this patch needs "
            "updating by hand."
        )

    path.write_text("".join(lines), encoding="utf-8")
    return patched


def find_game_launch_coordinator_smali(decompiled_dir: Path) -> Path:
    """Fallback for iiSU builds where LOG_ANCHOR is gone (see module
    docstring): locates the single-purpose class that actually fires the
    game-launch startActivity call, by content rather than its obfuscated
    name, which changes every rebuild. A sibling class references the same
    "GameLaunchCoordinator" log tag in its own log messages without ever
    dispatching anything itself (confirmed: it has zero startActivity
    calls), so the tag alone isn't a unique-enough anchor on its own,
    requiring an actual startActivity call alongside it is."""
    candidates = []
    for path in decompiled_dir.rglob("*.smali"):
        text = path.read_text(encoding="utf-8", errors="replace")
        # STARTACTIVITY_RE is anchored with ^/$ against a single line (it's
        # normally matched per-line via .match()), not re.MULTILINE, so it
        # must be checked line by line here too, .search()'ing the whole
        # file's text as one blob would only ever match at the file's
        # very start/end and silently find nothing.
        if GAME_LAUNCH_COORDINATOR_LOG_TAG in text and any(STARTACTIVITY_RE.match(line) for line in text.splitlines()):
            candidates.append(path)
    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected exactly one GameLaunchCoordinator-tagged class with a startActivity call, found {len(candidates)}. "
            "iiSU's code has likely changed and this patch needs updating by hand."
        )
    return candidates[0]


def patch_all_start_activity_calls(path: Path) -> int:
    """Redirects every startActivity(Intent[, Bundle]) call in path to
    LaunchBridge.launch(), with no method-scoping (unlike
    patch_main_activity()): this is only ever called on a class found by
    find_game_launch_coordinator_smali(), which exists for no purpose
    other than coordinating one game launch, so every startActivity call
    in it (the normal path and the StrictMode-relaxed cyou.joiplay.joiplay
    workaround path alike, confirmed live via a real 7.5-prerelease1
    launch) is fair game, unlike MainActivity's many unrelated ones."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)

    patched = 0
    for i, line in enumerate(lines):
        match = STARTACTIVITY_RE.match(line)
        if not match:
            continue
        lines[i] = redirect_start_activity(match)
        patched += 1

    if patched == 0:
        raise RuntimeError(
            f"Found the GameLaunchCoordinator class ({path.name}) but no startActivity(Intent) "
            "call inside it to redirect, this shouldn't happen (the file-level check above "
            "already confirmed one exists)."
        )

    path.write_text("".join(lines), encoding="utf-8")
    return patched


# A name no R8-obfuscated field can collide with (iiSU's own are 1-2 letters).
HOLDER_FIELD = "iisupcHolder"
PRIMARY_HOME_ACTIONS_METHOD_ANCHOR = ".method public final a()Lcom/iisulauncher/launcher/MainActivity;"
PRIMARY_HOME_ACTIONS_CLASS_RE = re.compile(r"^\.class public final L(\w+);$", re.MULTILINE)


def find_primary_home_actions_holder(decompiled_dir: Path) -> tuple[Path, str, str]:
    """Read-only lookup of the PrimaryHomeActions holder: (file, obfuscated
    class name, file text). Raises RuntimeError unless exactly one class
    matches. Shared by the patch and by patch_check's compatibility report."""
    candidates = []
    for path in decompiled_dir.rglob("*.smali"):
        text = path.read_text(encoding="utf-8", errors="replace")
        class_match = PRIMARY_HOME_ACTIONS_CLASS_RE.search(text)
        if class_match and PRIMARY_HOME_ACTIONS_METHOD_ANCHOR in text:
            candidates.append((path, class_match.group(1), text))

    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected exactly one iiSU PrimaryHomeActions holder class, found {len(candidates)}. "
            "iiSU's code has likely changed and this patch needs updating by hand."
        )
    return candidates[0]


def patch_primary_home_actions_holder(decompiled_dir: Path) -> tuple[Path, str]:
    """Expose iiSU's existing injected PrimaryHomeActions instance (called
    "je6" in the build this was first written against).

    This class already owns the synchronized WeakReference<MainActivity>.
    This patch exposes only the object itself through a static field so
    MediaBridge can call its a() method and reuse iiSU's existing activity
    lifecycle tracking.

    Anchored on class/method text rather than a hardcoded class name: R8
    reassigns these short obfuscated names on every rebuild (confirmed
    live, the same class is "dq6" as of iiSU Alpha 7.5-prerelease1), so
    the class name is read out of each candidate file instead of glob'd by
    filename, and reused for the field/sput text it injects. Idempotent.
    """
    path, class_name, text = find_primary_home_actions_holder(decompiled_dir)
    class_ref = f"L{class_name};"

    static_field = f".field public static volatile {HOLDER_FIELD}:{class_ref}"
    if static_field not in text:
        field_anchor = "# instance fields"
        if field_anchor not in text:
            raise RuntimeError(f"Found {path.name} but could not find its instance-fields anchor.")
        text = text.replace(
            field_anchor,
            "# iiSU-PC: expose this existing injected PrimaryHomeActions holder.\n"
            f"{static_field}\n\n"
            f"{field_anchor}",
            1,
        )

    sput = f"    sput-object p0, {class_ref}->{HOLDER_FIELD}:{class_ref}"
    if sput not in text:
        ctor_start = text.find(".method public constructor <init>()V")
        if ctor_start < 0:
            raise RuntimeError(f"Found {path.name} but could not find its constructor.")

        ctor_end = text.find(".end method", ctor_start)
        if ctor_end < 0:
            raise RuntimeError(f"Found {path.name}'s constructor but not its .end method.")

        ctor = text[ctor_start:ctor_end]
        super_call = "    invoke-direct {p0}, Ljava/lang/Object;-><init>()V"
        if super_call not in ctor:
            raise RuntimeError(f"Found {path.name}'s constructor but could not find its Object constructor call.")

        patched_ctor = ctor.replace(
            super_call,
            super_call
            + "\n\n"
            + "    # iiSU-PC: expose this same Hilt-created holder; MainActivity itself\n"
            + "    # remains referenced only by this class's existing WeakReference.\n"
            + sput,
            1,
        )
        text = text[:ctor_start] + patched_ctor + text[ctor_end:]

    path.write_text(text, encoding="utf-8")
    return path, class_name

# The asset-index helper MediaBridgeReceiver drives (iiSU's own static
# n/m/o/M/K/I utilities). R8 renames the class every rebuild ("wy6" in 7.4,
# "qa7" in 7.5-prerelease1, "xa7" in 7.5-prerelease2) but kept these method
# letters and descriptors through all three, so it's found by structure.
ASSET_HELPER_METHODS = (
    ".method public static n(Ljava/io/File;Ljava/lang/String;)Ljava/io/File;",
    ".method public static m(Ljava/io/File;Ljava/lang/String;)Ljava/util/List;",
    ".method public static o(Ljava/io/File;)Ljava/io/File;",
    ".method public static M(Landroid/content/Context;Ljava/lang/String;Ljava/lang/String;Ljava/io/File;)V",
    ".method public static K(Landroid/content/Context;Ljava/lang/String;Ljava/lang/String;Ljava/io/File;)V",
    ".method public static I(Landroid/content/Context;Ljava/lang/String;Ljava/lang/String;Ljava/io/File;"
    "Ljava/util/List;Ljava/util/List;Ljava/io/File;Ljava/io/File;Ljava/lang/Long;)V",
)
MAIN_ACTIVITY_REFRESH_METHOD = ".method public static final v(Lcom/iisulauncher/launcher/MainActivity;)V"
ASSET_INDEX_BLOCK_RE = re.compile(r"[ \t]*# IISUPC_ASSET_INDEX_BEGIN\n.*?[ \t]*# IISUPC_ASSET_INDEX_END\n", re.DOTALL)
CLASS_DECL_RE = re.compile(r"^\.class [^\n]*L(\w+);$", re.MULTILINE)


def find_asset_helper_class(decompiled_dir: Path) -> str | None:
    """Returns the obfuscated class name of iiSU's asset-index helper (see
    ASSET_HELPER_METHODS), or None if no class has the full signature set,
    in which case MediaBridge installs files without re-indexing them. More
    than one match is treated the same as none: guessing wrong would make
    the receiver call into an unrelated class."""
    matches = []
    for path in decompiled_dir.rglob("*.smali"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if all(sig in text for sig in ASSET_HELPER_METHODS):
            class_match = CLASS_DECL_RE.search(text)
            if class_match:
                matches.append(class_match.group(1))
    return matches[0] if len(matches) == 1 else None


def inject_launch_bridge(decompiled_dir: Path, primary_home_actions_class: str, asset_helper_class: str | None = None) -> None:
    """Copies smali_patch/*.smali into the decompiled tree, substituting the
    LIISUPC_HOLDER / LIISUPC_ASSETS class tokens MediaBridgeReceiver.smali is
    written against for whichever classes this iiSU build actually uses
    (those names change every rebuild, see patch_primary_home_actions_holder()
    and find_asset_helper_class()), so the receiver keeps pointing at classes
    that genuinely exist in the patched APK instead of silently referencing
    ones that don't. With no asset helper found, the asset-index block is
    stripped out of the receiver instead of leaving dangling references."""
    dest = decompiled_dir / "smali" / BRIDGE_PACKAGE_SMALI_DIR
    dest.mkdir(parents=True, exist_ok=True)
    for smali_file in SMALI_PATCH_DIR.glob("*.smali"):
        text = smali_file.read_text(encoding="utf-8")
        text = text.replace("LIISUPC_HOLDER;", f"L{primary_home_actions_class};")
        if asset_helper_class:
            text = text.replace("LIISUPC_ASSETS;", f"L{asset_helper_class};")
        else:
            text = ASSET_INDEX_BLOCK_RE.sub("", text)
        (dest / smali_file.name).write_text(text, encoding="utf-8")


def has_main_activity_refresh(decompiled_dir: Path) -> bool:
    return any(
        MAIN_ACTIVITY_REFRESH_METHOD in path.read_text(encoding="utf-8", errors="replace")
        for path in decompiled_dir.rglob("MainActivity.smali")
    )


def patch_manifest_for_media_bridge(decompiled_dir: Path) -> None:
    """Register the explicit ADB-invoked MediaBridge receiver.

    No intent filter is needed: Manager addresses the component explicitly.
    exported=true is required because `adb shell am broadcast` originates
    outside iiSU's app UID. Protect the exported receiver with Android's DUMP
    permission so adb shell can invoke it while ordinary third-party apps
    cannot explicitly address the component. Re-running the patch remains
    idempotent.
    """
    manifest = decompiled_dir / "AndroidManifest.xml"
    text = manifest.read_text(encoding="utf-8")
    receiver_name = "com.iisulauncher.pcbridge.MediaBridgeReceiver"
    receiver = (
        '        <receiver android:name="com.iisulauncher.pcbridge.MediaBridgeReceiver" '
        'android:enabled="true" android:exported="true" '
        'android:permission="android.permission.DUMP" />\n'
    )
    if receiver_name in text:
        if 'android:permission="android.permission.DUMP"' not in text:
            pattern = re.compile(
                r'[ \t]*<receiver\s+[^>]*android:name="com\.iisulauncher\.pcbridge\.MediaBridgeReceiver"[^>]*/>\n?'
            )
            text = pattern.sub(receiver, text, count=1)
            manifest.write_text(text, encoding="utf-8")
        return

    marker = "</application>"
    if marker not in text:
        raise RuntimeError("Could not find </application> in AndroidManifest.xml.")

    text = text.replace(marker, receiver + marker, 1)
    manifest.write_text(text, encoding="utf-8")


def fix_extract_native_libs(decompiled_dir: Path) -> None:
    """extractNativeLibs="false" causes INSTALL_FAILED_INVALID_APK once the
    APK is re-signed with a different key than the original, flip it."""
    manifest = decompiled_dir / "AndroidManifest.xml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text.replace('android:extractNativeLibs="false"', 'android:extractNativeLibs="true"'), encoding="utf-8")


def enable_large_heap(decompiled_dir: Path) -> bool:
    """Sets android:largeHeap="true" on iiSU's <application>.

    iiSU sizes Coil's in-memory image cache at 16% of the app's memory class,
    and Coil reads that as ActivityManager.getLargeMemoryClass() when the app
    has largeHeap set, getMemoryClass() otherwise (checked in Alpha 7.5's
    newImageLoader and Coil's percent-of-memory sizing). On the AVD that is
    576 MB instead of 192 MB, so the cache grows from about 31 MB to about
    92 MB: at a 4K VM resolution the smaller cache only held a few screens of
    cover art, and covers reloaded every time they scrolled back into view.
    Returns whether the manifest changed (False when it was already set)."""
    manifest = decompiled_dir / "AndroidManifest.xml"
    text = manifest.read_text(encoding="utf-8")
    match = re.search(r"<application\b[^>]*>", text)
    if match is None:
        raise RuntimeError("Could not find <application> in AndroidManifest.xml.")
    tag = match.group(0)
    if 'android:largeHeap="true"' in tag:
        return False
    if "android:largeHeap=" in tag:
        new_tag = re.sub(r'android:largeHeap="[^"]*"', 'android:largeHeap="true"', tag)
    else:
        new_tag = tag.replace("<application", '<application android:largeHeap="true"', 1)
    manifest.write_text(text[:match.start()] + new_tag + text[match.end():], encoding="utf-8")
    return True


# The home screen's cover loader logs its queue as "inFlight=N pending=N
# queued=N"; that string finds its (obfuscated) class in any iiSU build.
BROWSER_LOADER_LOG_MARKER = '"inFlight="'
# How many covers the home screen may decode at once. iiSU Alpha 7.5 allows 2.
BROWSER_DECODE_PARALLELISM = 6

# In the loader's dispatch method: the limit loaded into a register right
# before it sizes the batch list (new ArrayList(limit)), then compared with
# the in-flight set's size in the loop that starts loads.
def _dispatch_limit(value: int) -> re.Pattern:
    return re.compile(
        rf"const/4 (v\d+), {hex(value)}(\s*(?:\.line \d+\s*)*)"
        r"invoke-direct \{(v\d+), \1\}, Ljava/util/ArrayList;-><init>\(I\)V"
    )


# In the method that works out how many free slots are left: limit - in flight.
def _free_slots(value: int) -> re.Pattern:
    return re.compile(
        r"(invoke-interface \{(v\d+)\}, Ljava/util/Set;->size\(\)I\s*(?:\.line \d+\s*)*move-result \2\s*(?:\.line \d+\s*)*)"
        rf"rsub-int/lit8 \2, \2, {hex(value)}"
    )


def raise_browser_decode_parallelism(decompiled_dir: Path, limit: int = BROWSER_DECODE_PARALLELISM) -> bool:
    """Lets the home screen decode more cover art at once.

    iiSU's home-screen cover loader starts at most 2 decodes at a time. On a
    big library at 4K that queues hundreds of covers whenever the list moves
    (seen live: 300-580 pending, about 55 decoded per second, and the same
    speed with 6 CPU cores as with the default), so covers scrolled into
    view stay blank for seconds. Raising the limit lets the decodes use the
    VM's other cores. Only the two places the limit appears are changed: the
    dispatch loop and the free-slot count. If either can't be found exactly
    once (a different iiSU build), nothing is changed and False is returned,
    so an unfamiliar build is left working as iiSU shipped it."""
    if not 2 < limit <= 7:  # const/4 holds -8..7
        raise ValueError("limit must be 3 to 7")
    candidates = [
        path for path in decompiled_dir.rglob("*.smali")
        if BROWSER_LOADER_LOG_MARKER in path.read_text(encoding="utf-8", errors="replace")
    ]
    if len(candidates) != 1:
        print(f"[patch] WARNING: couldn't find iiSU's home-screen cover loader ({len(candidates)} matches), "
              "leaving its decode limit as is.")
        return False
    path = candidates[0]
    text = path.read_text(encoding="utf-8")
    if len(_dispatch_limit(limit).findall(text)) == 1 and len(_free_slots(limit).findall(text)) == 1:
        return False  # already raised (an APK that was patched before)
    dispatch = list(_dispatch_limit(2).finditer(text))
    free_slots = list(_free_slots(2).finditer(text))
    if len(dispatch) != 1 or len(free_slots) != 1:
        print(f"[patch] WARNING: {path.name}'s decode limit isn't where expected "
              f"({len(dispatch)} dispatch, {len(free_slots)} free-slot matches), leaving it as is.")
        return False
    text = _dispatch_limit(2).sub(
        lambda m: f"const/4 {m.group(1)}, {hex(limit)}{m.group(2)}invoke-direct {{{m.group(3)}, {m.group(1)}}}, "
                  "Ljava/util/ArrayList;-><init>(I)V",
        text,
    )
    text = _free_slots(2).sub(lambda m: f"{m.group(1)}rsub-int/lit8 {m.group(2)}, {m.group(2)}, {hex(limit)}", text)
    path.write_text(text, encoding="utf-8")
    return True


# How far beyond the screen the home screen's grids load covers ahead of time.
BROWSER_PREFETCH_ROWS = 6        # uniform grids: rows above and below the screen (iiSU: 1)
BROWSER_PREFETCH_SCREENS = 2.0   # free-form grids: screen lengths either side (iiSU: 1.0)

_GAP = r"(?:\s*\.line \d+)*\s*"  # whitespace and apktool's .line markers between instructions
# The tail of the uniform-grid branch of iiSU's window calculation (s13.W in
# Alpha 7.5): overscanStart = (firstRow - 1) * columns, overscanEnd =
# (lastRow + 2) * columns, i.e. one row of covers loaded beyond each edge.
def _grid_overscan(start: str, end: str) -> re.Pattern:
    """start: how the first overscan row is derived from the first visible row,
    as a regex whose group 1 is that register; end: the rows added to the
    last visible row for the end of the overscan (exclusive)."""
    return re.compile(
        rf"{start}{_GAP}mul-int/2addr \1, (v\d+){_GAP}if-gez \1, :(\w+){_GAP}goto :(\w+){_GAP}"
        rf":\3{_GAP}move v\d+, \1{_GAP}:\4{_GAP}add-int/lit8 (v\d+), \5, {end}{_GAP}mul-int/2addr \5, \2"
    )


_GRID_OVERSCAN = _grid_overscan(r"sub-int/2addr (v\d+), v\d+", "0x2")
# The free-form branch of the same method: margin = 1.0f * viewport length,
# where the viewport length was just read from the layout's "a" field.
_FREEFORM_MARGIN = re.compile(rf"(iget (v\d+), \2, L[\w/$]+;->a:F{_GAP})(mul-float/2addr (v\d+), \2)")


def _float_bits(value: float) -> str:
    """A float's bits as const/high16 wants them (only the top 16 bits may be set)."""
    bits = struct.unpack(">I", struct.pack(">f", value))[0]
    if bits & 0xFFFF:
        raise ValueError(f"{value} can't be loaded with const/high16")
    return hex(bits)


def widen_browser_prefetch(
    decompiled_dir: Path, rows: int = BROWSER_PREFETCH_ROWS, screens: float = BROWSER_PREFETCH_SCREENS
) -> bool:
    """Makes the home screen load covers further ahead of where you're looking.

    iiSU's grid only asks for covers one row beyond the visible area (one
    screen length for free-form layouts), so while scrolling, new covers are
    requested only as they arrive and show blank until decoded. Asking for
    `rows` rows (or `screens` screen lengths) beyond each edge starts them
    decoding well before they come into view. Covers already in the cache
    cost nothing, so this mainly moves work iiSU would do anyway earlier.

    The method is found by its shape (the uniform-grid tail, matched exactly
    once, in a method that also calls Math.floor and Math.ceil), since its
    obfuscated name changes between iiSU builds. If it isn't found, nothing
    changes and False is returned; an already-widened build is left alone
    quietly."""
    if not 1 < rows <= 7 or not 1.0 < screens <= 8.0:
        raise ValueError("rows must be 2-7 and screens more than 1 and at most 8")
    widened = _grid_overscan(rf"add-int/lit8 (v\d+), \1, -{hex(rows)}", hex(rows + 1))
    matches, already = [], 0
    for path in decompiled_dir.rglob("*.smali"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "Ljava/lang/Math;->floor(D)D" not in text:
            continue
        for method in re.finditer(r"^\.method [^\n]*\n.*?^\.end method", text, re.S | re.M):
            body = method.group(0)
            if "Ljava/lang/Math;->ceil(D)D" in body and "Ljava/lang/Math;->floor(D)D" in body:
                matches.extend((path, method.start(), method.end(), m) for m in _GRID_OVERSCAN.finditer(body))
                already += len(widened.findall(body))
    if not matches and already == 1:
        return False  # already widened (an APK that was patched before)
    if len(matches) != 1:
        print(f"[patch] WARNING: couldn't find iiSU's home-screen grid window ({len(matches)} matches), "
              "leaving how far ahead it loads covers as is.")
        return False
    path, start, end, m = matches[0]
    text = path.read_text(encoding="utf-8")
    body = text[start:end]
    start_reg, end_reg = m.group(1), m.group(5)
    tail = m.group(0)
    tail = re.sub(rf"^sub-int/2addr {start_reg}, v\d+",
                  f"add-int/lit8 {start_reg}, {start_reg}, -{hex(rows)}", tail, count=1)
    tail = tail.replace(f"add-int/lit8 {end_reg}, {end_reg}, 0x2", f"add-int/lit8 {end_reg}, {end_reg}, {hex(rows + 1)}", 1)
    body = body[: m.start()] + tail + body[m.end():]

    margins = list(_FREEFORM_MARGIN.finditer(body))
    if len(margins) == 1:
        mm = margins[0]
        body = (body[: mm.start()] + mm.group(1)
                + f"const/high16 {mm.group(4)}, {_float_bits(screens)}\n\n    "
                + mm.group(3) + body[mm.end():])
    else:
        print(f"[patch] WARNING: {path.name}: free-form grid margin not found ({len(margins)} matches), "
              "only uniform grids load further ahead.")
    path.write_text(text[:start] + body + text[end:], encoding="utf-8")
    return True


# Share of iiSU's memory class its Coil image cache may use. iiSU uses 0.16.
# Bitmap pixels live in native memory (Android 8+), so this doesn't eat into
# the Java heap that largeHeap governs; 0.5 of 576 MB is about 288 MB.
IMAGE_MEMORY_CACHE_SHARE = 0.5
_APPLICATION_SMALI = "com/iisulauncher/LairLauncherApplication.smali"
# newImageLoader: MemoryCache.Builder().maxSizePercent(share, context), the
# share loaded as a double right before the call.
_MEMORY_CACHE_SHARE = re.compile(
    r"const-wide (v\d+), (0x[0-9a-f]+)L(?:\s*# [0-9.]+)?" + _GAP
    + r"invoke-virtual \{(v\d+), \1, v\d+, (p\d+|v\d+)\}, L\w+;->\w+\(DLandroid/content/Context;\)V"
)


def _double_bits(value: float) -> str:
    return hex(struct.unpack(">Q", struct.pack(">d", value))[0])


def raise_image_memory_cache(decompiled_dir: Path, share: float = IMAGE_MEMORY_CACHE_SHARE) -> bool:
    """Lets iiSU keep more decoded artwork in memory.

    iiSU gives Coil, which draws the home screen's console pages, 16% of its
    memory class for decoded images. With largeHeap that is about 92 MB, and
    at 4K one console page's artwork alone is tens of MB, so paging back and
    forth through a few consoles pushed the earlier pages out (seen live:
    a page visited seconds before came back with blank tiles that popped in
    a few hundred ms later). `share` of 576 MB keeps several pages resident.
    At 0.35 the blank time halved (to 3-4 frames) but didn't go away.

    Only the one maxSizePercent(double, Context) call in iiSU's Application
    class is changed, and only while it still holds iiSU's 0.16; anything
    else is left as is."""
    if not 0.16 < share <= 0.75:
        raise ValueError("share must be above 0.16 and at most 0.75")
    candidates = [p for p in decompiled_dir.glob(f"smali*/{_APPLICATION_SMALI}") if p.is_file()]
    if len(candidates) != 1:
        print("[patch] WARNING: couldn't find iiSU's Application class, leaving its image cache size as is.")
        return False
    path = candidates[0]
    text = path.read_text(encoding="utf-8")
    calls = list(_MEMORY_CACHE_SHARE.finditer(text))
    original = [m for m in calls if m.group(2) == _double_bits(0.16)]
    if not original and any(m.group(2) == _double_bits(share) for m in calls):
        return False  # already raised
    if len(original) != 1:
        print(f"[patch] WARNING: iiSU's image cache size isn't where expected ({len(original)} matches), leaving it as is.")
        return False
    m = original[0]
    start, end = m.span(2)
    path.write_text(text[:start] + _double_bits(share) + text[end:], encoding="utf-8")
    return True


def zipalign(zipalign_exe: Path, unsigned_apk: Path, aligned_apk: Path) -> None:
    run([str(zipalign_exe), "-p", "-f", "4", str(unsigned_apk), str(aligned_apk)])


def sign(apksigner_exe: Path, keystore: Path, keystore_pass: str, key_alias: str, aligned_apk: Path, signed_apk: Path) -> None:
    # apksigner.bat resolves its own `java` via JAVA_HOME/PATH internally,
    # env= makes sure that resolves to a bundled JRE once one exists,
    # without ever touching the user's real system PATH/JAVA_HOME.
    run([
        str(apksigner_exe), "sign",
        "--ks", str(keystore),
        "--ks-key-alias", key_alias,
        "--ks-pass", f"pass:{keystore_pass}",
        "--key-pass", f"pass:{keystore_pass}",
        "--out", str(signed_apk),
        str(aligned_apk),
    ], env=java_subprocess_env())


def patch_apk(
    source_apk: Path,
    output_apk: Path,
    work_dir: Path,
    zipalign_exe: Path,
    apksigner_exe: Path,
    keystore: Path,
    keystore_pass: str,
    key_alias: str,
) -> str | None:
    """Patches source_apk into output_apk and returns iiSU's own versionName
    from its manifest (None if it couldn't be read)."""
    decompiled_dir = work_dir / "decompiled"
    unsigned_apk = work_dir / "unsigned.apk"
    aligned_apk = work_dir / "aligned.apk"

    print(f"[patch] decompiling {source_apk.name}...")
    decompile(source_apk, decompiled_dir)

    # The same read-only report `patch_check.py` prints, so a Setup log
    # alone says which build this was and which hooks were found.
    import patch_check

    version_name = patch_check.read_version_name(decompiled_dir)
    print(f"[patch] build: {patch_check.KNOWN_BUILDS.get(patch_check.sha256_file(source_apk), 'not one this project has seen before')}"
          f" (versionName {version_name or 'unknown'})")
    for finding in patch_check.inspect_decompiled(decompiled_dir):
        print(f"[patch]   {finding.status.upper():8} {finding.name}: {finding.detail}")

    print("[patch] locating the ROM-launch code...")
    try:
        main_activity = find_main_activity_smali(decompiled_dir)
        patched_count = patch_main_activity(main_activity)
    except RuntimeError:
        # LOG_ANCHOR is gone as of iiSU Alpha 7.5-prerelease1, which moved
        # the actual dispatch out of MainActivity into its own dedicated
        # class (see module docstring). Older builds still hit the try
        # above and never reach this fallback.
        main_activity = find_game_launch_coordinator_smali(decompiled_dir)
        patched_count = patch_all_start_activity_calls(main_activity)
    print(f"[patch] redirected {patched_count} startActivity call(s) to LaunchBridge in {main_activity.relative_to(decompiled_dir)}")

    print("[patch] exposing iiSU's existing PrimaryHomeActions holder...")
    primary_home_actions, primary_home_actions_class = patch_primary_home_actions_holder(decompiled_dir)
    print(f"[patch] patched PrimaryHomeActions holder ({primary_home_actions_class}) in {primary_home_actions.relative_to(decompiled_dir)}")

    print("[patch] injecting iiSU-PC bridge classes...")
    asset_helper_class = find_asset_helper_class(decompiled_dir)
    if asset_helper_class:
        print(f"[patch] found iiSU's asset-index helper ({asset_helper_class})")
    else:
        print("[patch] WARNING: couldn't find iiSU's asset-index helper, MediaBridge will install media without re-indexing it.")
    if not has_main_activity_refresh(decompiled_dir):
        print("[patch] WARNING: MainActivity's refresh entry point is gone, MediaBridge rescans will fail until the receiver is updated.")
    inject_launch_bridge(decompiled_dir, primary_home_actions_class, asset_helper_class)
    patch_manifest_for_media_bridge(decompiled_dir)
    fix_extract_native_libs(decompiled_dir)
    if enable_large_heap(decompiled_dir):
        print("[patch] enabled largeHeap so iiSU keeps more cover art in memory")
    if raise_browser_decode_parallelism(decompiled_dir):
        print(f"[patch] home screen now decodes up to {BROWSER_DECODE_PARALLELISM} covers at once (was 2)")
    if widen_browser_prefetch(decompiled_dir):
        print(f"[patch] home screen now loads covers {BROWSER_PREFETCH_ROWS} rows ahead (was 1)")
    if raise_image_memory_cache(decompiled_dir):
        print(f"[patch] iiSU's image memory cache raised to {IMAGE_MEMORY_CACHE_SHARE:.0%} of its memory (was 16%)")

    print("[patch] rebuilding...")
    build(decompiled_dir, unsigned_apk)

    print("[patch] aligning and signing...")
    zipalign(zipalign_exe, unsigned_apk, aligned_apk)
    sign(apksigner_exe, keystore, keystore_pass, key_alias, aligned_apk, output_apk)

    print(f"[patch] done: {output_apk}")
    return version_name
